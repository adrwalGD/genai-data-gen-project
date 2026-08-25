"""The `Dataset` record: schema + generated tables + provenance (F2.4).

Lives in `storage/` (not `generation/`) so persistence and the loader can use it without importing service
layers (docs/architecture.md, arch-check R6). `plan` and `report` are kept as JSON-able dicts here;
the generation layer re-hydrates them with `GenerationPlan.model_validate(...)` and
`ValidationReport.model_validate(...)`.
"""

from __future__ import annotations

import base64
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from ..schema.models import Schema

DATASET_ID_RE = re.compile(r"^[a-z2-7]{12}$")


def new_dataset_id() -> str:
    """12 lowercase base32 characters — safe inside a Postgres identifier (`ds_<id>`)."""
    raw = base64.b32encode(secrets.token_bytes(8)).decode().lower().rstrip("=")
    return raw[:12]


def default_dataset_name(schema: Schema) -> str:
    return "-".join(t.lower() for t in schema.table_names[:3])


@dataclass
class Dataset:
    id: str
    name: str
    ddl: str
    schema: Schema
    tables: dict[str, pd.DataFrame]
    plan: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    instructions: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0))

    @property
    def pg_schema(self) -> str:
        return f"ds_{self.id}"

    def row_counts(self) -> dict[str, int]:
        return {t.name: len(self.tables[t.name]) for t in self.schema.tables if t.name in self.tables}

    @property
    def total_rows(self) -> int:
        return sum(self.row_counts().values())

    @property
    def report_ok(self) -> bool | None:
        return None if self.report is None else bool(self.report.get("ok"))
