"""Read-only SQL guard (F6.1) — every user/LLM query passes here before PostgreSQL (CLAUDE.md rule 4).

Accepts exactly one SELECT / UNION / INTERSECT / EXCEPT (CTEs allowed); rejects DML/DDL, multiple statements,
`SELECT ... INTO`, `FOR UPDATE/SHARE`, and dangerous functions (pg_sleep, dblink, file access, ...). Quoted
identifiers are lowercased because dataset tables are created lowercase (DECISIONS.md). The result carries
`LIMIT cap + 1` so the executor can tell the caller the result was truncated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

DENIED_FUNCTIONS = {
    "nextval",
    "setval",
    "lastval",
    "currval",
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "lo_unlink",
    "dblink",
    "dblink_exec",
    "set_config",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "query_to_xml",
    "pg_advisory_lock",
    "txid_current",
}


class SqlRejected(ValueError):
    """The query is not a single read-only SELECT; the message says why and what to change."""


@dataclass
class GuardedSql:
    sql: str
    limit: int
    tables: list[str] = field(default_factory=list)
    original: str = ""


def guard(sql: str, *, limit: int) -> GuardedSql:
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise SqlRejected("empty query — write a SELECT statement")
    try:
        statements = [s for s in sqlglot.parse(text, read="postgres") if s is not None]
    except SqlglotError as e:
        raise SqlRejected(f"not valid SQL: {str(e).splitlines()[0][:200]}") from e
    if len(statements) != 1:
        raise SqlRejected(
            f"exactly one statement is allowed, got {len(statements)} — send one SELECT at a time"
        )
    root = statements[0]
    if not isinstance(root, exp.Select | exp.SetOperation):
        raise SqlRejected(
            f"only read-only SELECT queries are allowed (got {type(root).__name__.upper()}) — "
            "this assistant never modifies data"
        )
    writer = root.find(exp.Insert, exp.Update, exp.Delete, exp.Merge)
    if writer is not None:
        raise SqlRejected(
            f"{type(writer).__name__.upper()} inside the query (e.g. in a WITH clause) is not allowed — "
            "this assistant never modifies data"
        )
    for select in root.find_all(exp.Select):
        if select.args.get("into"):
            raise SqlRejected("SELECT ... INTO creates a table — remove INTO")
        if select.args.get("locks"):
            raise SqlRejected("FOR UPDATE/SHARE locks are not allowed in a read-only query")
    for func in root.find_all(exp.Func):
        name = (func.sql_name() if not isinstance(func, exp.Anonymous) else str(func.this)).lower()
        if name in DENIED_FUNCTIONS:
            raise SqlRejected(f"function {name}() is not allowed in a read-only query")
    for ident in root.find_all(exp.Identifier):
        if ident.quoted and ident.name != ident.name.lower():
            ident.set("this", ident.name.lower())  # tables/columns are created lowercase (docs/db-rules.md)
    tables = sorted({t.name.lower() for t in root.find_all(exp.Table) if t.name})
    capped = _apply_limit(root, limit + 1)
    return GuardedSql(sql=capped.sql(dialect="postgres"), limit=limit, tables=tables, original=sql)


def _apply_limit(root: exp.Select | exp.SetOperation, cap: int) -> exp.Expression:
    """Keep a smaller user LIMIT, lower a larger one, add one when missing (wrapping set operations)."""
    existing = root.args.get("limit")
    if isinstance(existing, exp.Limit) and isinstance(existing.expression, exp.Literal):
        try:
            current = int(existing.expression.this)
        except ValueError:
            current = cap
        if current <= cap:
            return root
    if isinstance(root, exp.Select):
        return root.limit(cap)
    return exp.select("*").from_(root.subquery("q")).limit(cap)
