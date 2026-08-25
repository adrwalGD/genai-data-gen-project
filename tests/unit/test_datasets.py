"""F2.4 — filesystem dataset registry: save/load/list/delete round trip."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from genai_data_gen_project.generation import heuristics
from genai_data_gen_project.generation.expander import expand
from genai_data_gen_project.generation.validator import validate
from genai_data_gen_project.schema.parser import parse_ddl
from genai_data_gen_project.storage import datasets
from genai_data_gen_project.storage.dataset import (
    DATASET_ID_RE,
    Dataset,
    default_dataset_name,
    new_dataset_id,
)


def make_dataset(ddl: str, rows: int, seed: int, created: datetime) -> Dataset:
    schema = parse_ddl(ddl)
    plan = heuristics.plan(schema, rows)
    tables = expand(schema, plan, seed=seed)
    report = validate(schema, tables)
    return Dataset(
        id=new_dataset_id(),
        name=default_dataset_name(schema),
        ddl=ddl,
        schema=schema,
        tables=tables,
        plan=plan.model_dump(mode="json"),
        report=report.model_dump(mode="json"),
        instructions="realistic Polish restaurants",
        params={"rows_per_table": rows, "seed": seed, "temperature": 0.7, "llm": False},
        created_at=created,
    )


def test_ids_and_names() -> None:
    ids = {new_dataset_id() for _ in range(200)}
    assert len(ids) == 200 and all(DATASET_ID_RE.match(i) for i in ids)
    schema = parse_ddl("CREATE TABLE A (id INT PRIMARY KEY); CREATE TABLE B (id INT PRIMARY KEY);")
    assert default_dataset_name(schema) == "a-b"


def test_save_load_round_trip(tmp_path: Path, sample_ddl: dict[str, str]) -> None:
    ds = make_dataset(sample_ddl["restaurants"], 30, 5, datetime(2026, 8, 25, 12, 0, tzinfo=UTC))
    path = datasets.save(ds, tmp_path)
    assert path == tmp_path / ds.id
    assert (path / "manifest.json").exists() and (path / "schema.ddl").exists()
    assert sorted(p.name for p in (path / "tables").iterdir()) == sorted(
        f"{t}.csv" for t in ds.schema.table_names
    )
    assert not list(tmp_path.glob(".*.tmp"))
    loaded = datasets.load(ds.id, tmp_path)
    assert loaded.id == ds.id and loaded.name == "restaurants-customers-orders"
    assert loaded.schema.table_names == ds.schema.table_names and loaded.ddl == ds.ddl
    for name in ds.schema.table_names:
        pd.testing.assert_frame_equal(loaded.tables[name], ds.tables[name])
    assert loaded.plan == ds.plan and loaded.report == ds.report and loaded.report_ok is True
    assert loaded.params == ds.params and loaded.instructions == ds.instructions
    assert loaded.created_at == ds.created_at and loaded.pg_schema == f"ds_{ds.id}"
    assert loaded.total_rows == 30 * 7


def test_list_orders_newest_first_and_skips_garbage(tmp_path: Path, sample_ddl: dict[str, str]) -> None:
    older = make_dataset(sample_ddl["company"], 3, 1, datetime(2026, 1, 1, tzinfo=UTC))
    newer = make_dataset(sample_ddl["library"], 4, 2, datetime(2026, 6, 1, tzinfo=UTC))
    datasets.save(older, tmp_path)
    datasets.save(newer, tmp_path)
    (tmp_path / "garbage").mkdir()
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "manifest.json").write_text("{not json")
    infos = datasets.list_datasets(tmp_path)
    assert [i.id for i in infos] == [newer.id, older.id]
    assert infos[0].tables == dict.fromkeys(newer.schema.table_names, 4) and infos[0].total_rows == 36
    assert infos[0].label.startswith("authors-publishers-books · 9 tables · 36 rows · 2026-06-01")
    assert infos[1].report_ok is True and infos[1].params["seed"] == 1
    assert datasets.info(older.id, tmp_path) == infos[1]
    assert datasets.list_datasets(tmp_path / "missing") == []


def test_save_replaces_and_delete_removes(tmp_path: Path, sample_ddl: dict[str, str]) -> None:
    ds = make_dataset(sample_ddl["company"], 2, 1, datetime(2026, 1, 1, tzinfo=UTC))
    datasets.save(ds, tmp_path)
    ds.tables["Companies"] = ds.tables["Companies"].head(1)
    datasets.save(ds, tmp_path)
    assert json.loads((tmp_path / ds.id / "manifest.json").read_text())["tables"]["Companies"] == 1
    datasets.delete(ds.id, tmp_path)
    assert not (tmp_path / ds.id).exists()
    with pytest.raises(datasets.DatasetNotFound):
        datasets.load(ds.id, tmp_path)
    with pytest.raises(datasets.DatasetNotFound):
        datasets.delete(ds.id, tmp_path)
    with pytest.raises(datasets.DatasetNotFound):
        datasets.load("../etc", tmp_path)
