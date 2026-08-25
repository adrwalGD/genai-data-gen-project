"""Dataset registry on the filesystem (F2.4): `data/datasets/<id>/{manifest.json, schema.ddl, tables/*.csv}`.

The filesystem is the source of truth; the Postgres schema `ds_<id>` (F2.5) is a queryable copy that
can always be rebuilt from these files. Writes go to a temporary directory and are renamed into place,
so a crash never leaves a half-written dataset visible.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..schema.parser import parse_ddl
from . import csvio
from .dataset import DATASET_ID_RE, Dataset

MANIFEST = "manifest.json"
DDL_FILE = "schema.ddl"
TABLES_DIR = "tables"
FORMAT_VERSION = 1


class DatasetNotFound(KeyError):
    pass


@dataclass(frozen=True)
class DatasetInfo:
    id: str
    name: str
    created_at: datetime
    tables: dict[str, int]
    instructions: str | None
    params: dict[str, Any]
    report_ok: bool | None
    path: Path

    @property
    def total_rows(self) -> int:
        return sum(self.tables.values())

    @property
    def label(self) -> str:
        when = f"{self.created_at:%Y-%m-%d %H:%M}"
        return f"{self.name} · {len(self.tables)} tables · {self.total_rows} rows · {when} · {self.id}"


def datasets_root(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).datasets_dir


def _dir(root: Path, dataset_id: str) -> Path:
    if not DATASET_ID_RE.match(dataset_id):
        raise DatasetNotFound(f"invalid dataset id {dataset_id!r}")
    return root / dataset_id


def save(dataset: Dataset, root: Path | None = None) -> Path:
    """Persist (or replace) a dataset atomically; returns its directory."""
    root = root or datasets_root()
    target = _dir(root, dataset.id)
    tmp = root / f".{dataset.id}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / TABLES_DIR).mkdir(parents=True)
    (tmp / DDL_FILE).write_text(dataset.ddl, encoding="utf-8")
    for table in dataset.schema.tables:
        frame = dataset.tables.get(table.name)
        if frame is None:
            continue
        (tmp / TABLES_DIR / f"{table.name}.csv").write_bytes(csvio.to_csv_bytes(frame, table))
    manifest = {
        "format_version": FORMAT_VERSION,
        "id": dataset.id,
        "name": dataset.name,
        "created_at": dataset.created_at.isoformat(),
        "source_dialect": dataset.schema.source_dialect,
        "tables": dataset.row_counts(),
        "instructions": dataset.instructions,
        "params": dataset.params,
        "plan": dataset.plan,
        "report": dataset.report,
    }
    (tmp / MANIFEST).write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    if target.exists():
        shutil.rmtree(target)
    os.replace(tmp, target)
    return target


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise DatasetNotFound(f"no dataset at {path}") from e
    return data


def info(dataset_id: str, root: Path | None = None) -> DatasetInfo:
    path = _dir(root or datasets_root(), dataset_id)
    return _info_from(path, _read_manifest(path))


def _info_from(path: Path, m: dict[str, Any]) -> DatasetInfo:
    report = m.get("report")
    return DatasetInfo(
        id=m["id"],
        name=m.get("name") or m["id"],
        created_at=datetime.fromisoformat(m["created_at"]),
        tables=dict(m.get("tables") or {}),
        instructions=m.get("instructions"),
        params=dict(m.get("params") or {}),
        report_ok=None if report is None else bool(report.get("ok")),
        path=path,
    )


def list_datasets(root: Path | None = None) -> list[DatasetInfo]:
    """All saved datasets, newest first. Unreadable directories are skipped."""
    root = root or datasets_root()
    if not root.exists():
        return []
    out: list[DatasetInfo] = []
    for path in root.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        try:
            out.append(_info_from(path, _read_manifest(path)))
        except DatasetNotFound, KeyError, ValueError, json.JSONDecodeError:
            continue
    return sorted(out, key=lambda i: (i.created_at, i.id), reverse=True)


def load(dataset_id: str, root: Path | None = None) -> Dataset:
    path = _dir(root or datasets_root(), dataset_id)
    m = _read_manifest(path)
    ddl = (path / DDL_FILE).read_text(encoding="utf-8")
    schema = parse_ddl(ddl, dialect=m.get("source_dialect"))
    tables = {}
    for table in schema.tables:
        csv_path = path / TABLES_DIR / f"{table.name}.csv"
        if csv_path.exists():
            tables[table.name] = csvio.read_csv(csv_path.read_bytes(), table)
    return Dataset(
        id=m["id"],
        name=m.get("name") or m["id"],
        ddl=ddl,
        schema=schema,
        tables=tables,
        plan=m.get("plan"),
        report=m.get("report"),
        instructions=m.get("instructions"),
        params=dict(m.get("params") or {}),
        created_at=datetime.fromisoformat(m["created_at"]),
    )


def delete(dataset_id: str, root: Path | None = None) -> None:
    path = _dir(root or datasets_root(), dataset_id)
    if not path.exists():
        raise DatasetNotFound(f"dataset {dataset_id!r} does not exist")
    shutil.rmtree(path)
