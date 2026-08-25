"""F2.5 — load datasets into PostgreSQL: schema per dataset, COPY, FKs after load, identity continuation."""

from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest

from genai_data_gen_project.config import Settings
from genai_data_gen_project.generation import heuristics
from genai_data_gen_project.generation.expander import expand
from genai_data_gen_project.generation.validator import validate
from genai_data_gen_project.schema.parser import parse_ddl
from genai_data_gen_project.storage import postgres
from genai_data_gen_project.storage.dataset import Dataset, new_dataset_id

pytestmark = pytest.mark.integration


def build(ddl: str, rows: int, seed: int = 1) -> Dataset:
    schema = parse_ddl(ddl)
    tables = expand(schema, heuristics.plan(schema, rows), seed=seed)
    report = validate(schema, tables)
    assert report.ok, report.summary()
    return Dataset(
        id=new_dataset_id(), name="t", ddl=ddl, schema=schema, tables=tables,
        report=report.model_dump(mode="json"), created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )  # fmt: skip


@pytest.fixture
def cleanup(settings: Settings) -> Iterator[list[str]]:
    ids: list[str] = []
    yield ids
    for dataset_id in ids:
        postgres.drop_dataset(dataset_id, settings)


@pytest.mark.parametrize("name", ["library", "restaurants", "company"])
def test_loads_1000_rows_per_table_with_fks(
    name: str, sample_ddl: dict[str, str], settings: Settings, pg_conn: psycopg.Connection, cleanup: list[str]
) -> None:
    ds = build(sample_ddl[name], 1000)
    cleanup.append(ds.id)
    result = postgres.load_dataset(ds, settings)
    assert result.schema_name == f"ds_{ds.id}"
    assert result.row_counts == dict.fromkeys(ds.schema.table_names, 1000)
    assert result.fk_constraints == sum(len(t.foreign_keys) for t in ds.schema.tables)
    assert result.elapsed_s < 60
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.table_constraints "
            "WHERE table_schema = %s AND constraint_type = 'FOREIGN KEY'",
            (result.schema_name,),
        )
        assert cur.fetchone() == (result.fk_constraints,)
        cur.execute(
            "SELECT obj_description(oid, 'pg_namespace') FROM pg_namespace WHERE nspname = %s",
            (result.schema_name,),
        )
        comment = cur.fetchone()
        assert comment is not None and ds.id in comment[0]
    pg_conn.rollback()
    assert postgres.is_loaded(ds.id, settings)
    assert postgres.loaded_row_counts(ds, settings) == result.row_counts


def test_identity_continues_after_explicit_ids(
    sample_ddl: dict[str, str], settings: Settings, cleanup: list[str]
) -> None:
    ds = build(sample_ddl["restaurants"], 50)
    cleanup.append(ds.id)
    postgres.load_dataset(ds, settings)
    with postgres.connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO "ds_{ds.id}"."customers" (first_name, last_name, email) '
            "VALUES ('New', 'Person', 'new.person@example.org') RETURNING customer_id"
        )
        assert cur.fetchone() == (51,)
        conn.rollback()


def test_reload_replaces_and_failed_reload_keeps_previous(
    sample_ddl: dict[str, str], settings: Settings, cleanup: list[str]
) -> None:
    ds = build(sample_ddl["company"], 20)
    cleanup.append(ds.id)
    first = postgres.load_dataset(ds, settings)
    ds.tables["Companies"] = ds.tables["Companies"].head(10)
    ds.tables["Departments"] = ds.tables["Departments"][ds.tables["Departments"]["company_id"] <= 10]
    ds.tables["Projects"] = ds.tables["Projects"][ds.tables["Projects"]["company_id"] <= 10]
    # keep integrity for the reload: drop dependents of removed projects
    kept_projects = set(ds.tables["Projects"]["project_id"])
    ds.tables["Employee_Projects"] = ds.tables["Employee_Projects"][
        ds.tables["Employee_Projects"]["project_id"].isin(kept_projects)
    ]
    kept_depts = set(ds.tables["Departments"]["department_id"])
    ds.tables["Employees"] = ds.tables["Employees"][ds.tables["Employees"]["department_id"].isin(kept_depts)]
    kept_emps = set(ds.tables["Employees"]["employee_id"])
    for child in ("Employee_Projects", "Employee_Benefits", "Performance_Reviews"):
        frame = ds.tables[child]
        mask = frame["employee_id"].isin(kept_emps)
        if child == "Performance_Reviews":
            mask &= frame["reviewer_id"].isin(kept_emps)
        ds.tables[child] = frame[mask]
    second = postgres.load_dataset(ds, settings)
    assert second.row_counts["Companies"] == 10 and second.row_counts != first.row_counts
    # a dataset with a dangling FK fails atomically; the second version stays queryable
    ds.tables["Departments"].iloc[0, ds.tables["Departments"].columns.get_loc("company_id")] = 999_999
    with pytest.raises(postgres.LoadError, match="foreign key"):
        postgres.load_dataset(ds, settings)
    assert postgres.loaded_row_counts(ds, settings) == second.row_counts


def test_drop_and_list(sample_ddl: dict[str, str], settings: Settings) -> None:
    ds = build(sample_ddl["restaurants"], 5)
    postgres.load_dataset(ds, settings)
    assert ds.id in postgres.list_loaded(settings)
    assert postgres.drop_dataset(ds.id, settings) is True
    assert postgres.drop_dataset(ds.id, settings) is False
    assert ds.id not in postgres.list_loaded(settings)
    with pytest.raises(postgres.LoadError, match="invalid dataset id"):
        postgres.drop_dataset("not-an-id", settings)


def test_connection_error_is_actionable() -> None:
    bad = Settings(_env_file=None, database_url="postgresql://x:secret@localhost:1/x")
    with pytest.raises(postgres.LoadError) as exc_info:
        postgres.connect(bad)
    message = str(exc_info.value)
    assert "make db-up" in message and "secret" not in message and "x:***@localhost:1" in message
