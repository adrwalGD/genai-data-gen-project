"""Download helpers for the UI (F2.4): CSV per table, ZIP of everything. Thin facade over `storage.csvio`."""

from __future__ import annotations

import pandas as pd

from ..schema.models import Schema
from ..storage import csvio
from ..storage.dataset import Dataset


def to_csv_bytes(
    dataset_or_tables: Dataset | dict[str, pd.DataFrame], table_name: str, schema: Schema | None = None
) -> bytes:
    if isinstance(dataset_or_tables, Dataset):
        return csvio.to_csv_bytes(
            dataset_or_tables.tables[table_name], dataset_or_tables.schema.table(table_name)
        )
    if schema is None:
        raise ValueError("schema is required when exporting raw tables")
    return csvio.to_csv_bytes(dataset_or_tables[table_name], schema.table(table_name))


def to_zip_bytes(dataset: Dataset) -> bytes:
    manifest = {
        "id": dataset.id,
        "name": dataset.name,
        "created_at": dataset.created_at.isoformat(),
        "tables": dataset.row_counts(),
        "instructions": dataset.instructions,
        "params": dataset.params,
        "validation": dataset.report,
    }
    return csvio.to_zip_bytes(dataset.tables, dataset.schema, ddl=dataset.ddl, manifest=manifest)


def csv_filename(dataset: Dataset, table_name: str) -> str:
    return f"{dataset.name}_{table_name}.csv"


def zip_filename(dataset: Dataset) -> str:
    return f"{dataset.name}_{dataset.id}.zip"
