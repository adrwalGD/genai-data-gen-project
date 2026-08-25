"""PostgreSQL loader for datasets (F2.5): schema `ds_<id>` per dataset, FKs added after bulk load.

One transaction per load: drop + recreate the schema, CREATE TABLEs (no FKs), COPY every table from the
CSV codec, ADD CONSTRAINT foreign keys (the database re-proves integrity), reset identity sequences, count
rows. A failing load rolls back completely, so a previously loaded version stays intact and queryable.
The read-only query executor for talk-to-data is added in F6.1 (`sql_guard.py`, `run_readonly`).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql

from ..config import Settings, get_settings
from ..schema.postgres_ddl import emit_foreign_keys, emit_sequence_resets, emit_tables, ident
from . import csvio
from .dataset import DATASET_ID_RE, Dataset
from .sql_guard import GuardedSql, SqlRejected, guard

_log = logging.getLogger(__name__)
SCHEMA_PREFIX = "ds_"


class LoadError(RuntimeError):
    """Loading failed (connection, DDL, COPY or constraint violation); message includes the fix hint."""


@dataclass
class LoadResult:
    schema_name: str
    row_counts: dict[str, int]
    fk_constraints: int
    elapsed_s: float
    warnings: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(self.row_counts.values())


def connect(settings: Settings | None = None) -> psycopg.Connection:
    settings = settings or get_settings()
    try:
        return psycopg.connect(settings.database_url, connect_timeout=10)
    except psycopg.OperationalError as e:
        raise LoadError(
            f"cannot connect to PostgreSQL at {_redact(settings.database_url)}: {_first_line(e)} — "
            "run `make db-up` (inside docker the host is `postgres`)"
        ) from e


def load_dataset(dataset: Dataset, settings: Settings | None = None) -> LoadResult:
    """Replace schema ds_<id> with the dataset's tables and data. All-or-nothing."""
    schema_name = dataset.pg_schema
    started = time.perf_counter()
    with connect(settings) as conn:
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name)))
                cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
                for stmt in emit_tables(dataset.schema, schema_name):
                    cur.execute(stmt)
                for table in dataset.schema.tables:
                    frame = dataset.tables.get(table.name)
                    if frame is None or frame.empty:
                        continue
                    columns = ", ".join(ident(c) for c in table.column_names)
                    copy_sql = (
                        f"COPY {ident(schema_name)}.{ident(table.name)} ({columns}) "
                        "FROM STDIN WITH (FORMAT csv, NULL '', HEADER true)"
                    )
                    with cur.copy(copy_sql) as copy:
                        copy.write(csvio.to_csv_bytes(frame, table))
                fk_statements = emit_foreign_keys(dataset.schema, schema_name)
                for stmt in fk_statements:
                    cur.execute(stmt)
                for stmt in emit_sequence_resets(dataset.schema, schema_name):
                    cur.execute(stmt)
                comment = json.dumps(
                    {
                        "dataset_id": dataset.id,
                        "name": dataset.name,
                        "created_at": dataset.created_at.isoformat(),
                    }
                )
                cur.execute(
                    sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
                        sql.Identifier(schema_name), sql.Literal(comment)
                    )
                )
                counts = _row_counts(cur, schema_name, dataset.schema.table_names)
        except psycopg.errors.ForeignKeyViolation as e:
            raise LoadError(
                f"dataset violates a foreign key while adding constraints: {_first_line(e)} — "
                "run the validator; the previous version of the dataset (if any) is unchanged"
            ) from e
        except psycopg.errors.IntegrityError as e:
            raise LoadError(
                f"dataset violates a constraint: {_first_line(e)} — run the validator; "
                "previous version unchanged"
            ) from e
        except psycopg.Error as e:
            raise LoadError(
                f"loading {schema_name} failed: {_first_line(e)} — previous version unchanged"
            ) from e
    elapsed = time.perf_counter() - started
    _log.info(
        "loaded dataset %s into %s: %s rows in %.1fs", dataset.id, schema_name, sum(counts.values()), elapsed
    )
    return LoadResult(
        schema_name=schema_name, row_counts=counts, fk_constraints=len(fk_statements), elapsed_s=elapsed
    )


def _row_counts(cur: psycopg.Cursor, schema_name: str, tables: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in tables:
        cur.execute(
            sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(schema_name), sql.Identifier(name.lower())
            )
        )
        row = cur.fetchone()
        counts[name] = int(row[0]) if row else 0
    return counts


def drop_dataset(dataset_id: str, settings: Settings | None = None) -> bool:
    """Drop schema ds_<id>; returns True if it existed."""
    if not DATASET_ID_RE.match(dataset_id):
        raise LoadError(f"invalid dataset id {dataset_id!r}")
    schema_name = f"{SCHEMA_PREFIX}{dataset_id}"
    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema_name,))
        existed = cur.fetchone() is not None
        cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name)))
        conn.commit()
    return existed


def list_loaded(settings: Settings | None = None) -> list[str]:
    """Dataset ids that currently have a schema in PostgreSQL."""
    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE %s "
            "ORDER BY schema_name",
            (f"{SCHEMA_PREFIX}%",),
        )
        return [row[0][len(SCHEMA_PREFIX) :] for row in cur.fetchall()]


def is_loaded(dataset_id: str, settings: Settings | None = None) -> bool:
    return dataset_id in list_loaded(settings)


def loaded_row_counts(dataset: Dataset, settings: Settings | None = None) -> dict[str, int]:
    with connect(settings) as conn, conn.cursor() as cur:
        return _row_counts(cur, dataset.pg_schema, dataset.schema.table_names)


def _first_line(e: BaseException) -> str:
    text = str(e).strip()
    return text.splitlines()[0][:300] if text else type(e).__name__


def _redact(url: str) -> str:
    if "@" in url and "://" in url:
        head, tail = url.split("://", 1)
        creds, host = tail.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        return f"{head}://{user}:***@{host}"
    return url


# --- read-only querying for Talk to your data (F6.1) ----------------------------------------------
class SqlError(RuntimeError):
    """PostgreSQL rejected or aborted the query; `hint` says what to try (goes back to the agent)."""

    def __init__(self, message: str, *, hint: str = "", sqlstate: str | None = None) -> None:
        super().__init__(f"{message} — {hint}" if hint else message)
        self.message = message
        self.hint = hint
        self.sqlstate = sqlstate


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int
    sql: str
    tables: list[str] = field(default_factory=list)

    def to_records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]


def run_readonly(
    dataset_id: str,
    sql_text: str,
    settings: Settings | None = None,
    *,
    limit: int | None = None,
    timeout_ms: int | None = None,
) -> QueryResult:
    """Guard the SQL, then run it READ ONLY with a statement timeout inside schema ds_<id>."""
    settings = settings or get_settings()
    if not DATASET_ID_RE.match(dataset_id):
        raise SqlError(f"invalid dataset id {dataset_id!r}", hint="pick a saved dataset")
    cap = limit or settings.sql_row_limit
    guarded: GuardedSql = guard(sql_text, limit=cap)  # SqlRejected propagates with its reason
    schema_name = f"{SCHEMA_PREFIX}{dataset_id}"
    started = time.perf_counter()
    with connect(settings) as conn:
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(
                    sql.SQL("SET LOCAL statement_timeout = {}").format(
                        sql.Literal(int(timeout_ms or settings.sql_timeout_ms))
                    )
                )
                cur.execute(sql.SQL("SET LOCAL search_path = {}").format(sql.Identifier(schema_name)))
                cur.execute(guarded.sql)
                columns = [d.name for d in cur.description or []]
                fetched = cur.fetchall() if cur.description else []
                raise _Rollback(columns, [list(row) for row in fetched])
        except _Rollback as done:  # the transaction block rolled back; nothing was written
            columns, rows = done.columns, done.rows
        except psycopg.errors.QueryCanceled as e:
            raise SqlError(
                f"query exceeded {timeout_ms or settings.sql_timeout_ms} ms",
                hint="add WHERE filters or aggregate less data",
                sqlstate=e.sqlstate,
            ) from e
        except psycopg.errors.ReadOnlySqlTransaction as e:
            raise SqlError(
                "write attempted inside a read-only query", hint="only SELECT is allowed", sqlstate=e.sqlstate
            ) from e
        except psycopg.Error as e:
            primary = (
                e.diag.message_primary if e.diag and e.diag.message_primary else _first_line(e)
            ) or str(e)
            hint = (
                e.diag.message_hint
                if e.diag and e.diag.message_hint
                else "check table and column names against the schema"
            )
            raise SqlError(primary, hint=hint, sqlstate=getattr(e, "sqlstate", None)) from e
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    truncated = len(rows) > guarded.limit
    return QueryResult(
        columns=columns,
        rows=rows[: guarded.limit],
        row_count=min(len(rows), guarded.limit),
        truncated=truncated,
        elapsed_ms=elapsed_ms,
        sql=guarded.sql,
        tables=guarded.tables,
    )


class _Rollback(Exception):
    """Internal: carries the fetched rows out of `conn.transaction()` so the block always rolls back."""

    def __init__(self, columns: list[str], rows: list[list[Any]]) -> None:
        super().__init__("rollback")
        self.columns = columns
        self.rows = rows


__all__ = [
    "LoadError",
    "LoadResult",
    "QueryResult",
    "SqlError",
    "SqlRejected",
    "connect",
    "drop_dataset",
    "is_loaded",
    "list_loaded",
    "load_dataset",
    "loaded_row_counts",
    "run_readonly",
]
