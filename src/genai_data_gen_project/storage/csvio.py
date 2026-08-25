"""CSV codec shared by downloads, dataset persistence and the Postgres COPY loader (F2.4).

Format: UTF-8, header = column names in DDL spelling, every value double-quoted, NULL = unquoted empty field
(PostgreSQL `COPY ... WITH (FORMAT csv, NULL '')` semantics: a quoted empty string stays a string). Dates are
ISO-8601 (`YYYY-MM-DD`, `YYYY-MM-DD HH:MM:SS`), decimals fixed-point (no exponent), booleans `true`/`false`.
Reading is typed from the schema so a save → load round trip is lossless (except `""` → NULL, see `read_csv`).
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

from ..schema.models import Column, ColumnType, Schema, Table

NULL_TOKEN = ""


class CsvFormatError(ValueError):
    """A CSV does not match the table schema (header mismatch or value that does not parse)."""


def format_value(value: Any) -> str | None:
    """Text form of a value for CSV/COPY; None stays None (→ unquoted empty field)."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime().replace(microsecond=0).isoformat(sep=" ")
    return str(value)


def _quote(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def to_csv_text(frame: pd.DataFrame, table: Table) -> str:
    columns = table.column_names
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise CsvFormatError(f"{table.name}: frame lacks columns {missing}")
    lines = [",".join(_quote(c) for c in columns)]
    for row in frame[columns].itertuples(index=False, name=None):
        cells = []
        for value in row:
            text = format_value(value)
            cells.append(NULL_TOKEN if text is None else _quote(text))
        lines.append(",".join(cells))
    return "\n".join(lines) + "\n"


def to_csv_bytes(frame: pd.DataFrame, table: Table) -> bytes:
    return to_csv_text(frame, table).encode("utf-8")


def parse_value(col: Column, text: str | None) -> Any:
    if text is None or text == NULL_TOKEN:
        return None
    t = col.type
    try:
        if t.is_integer:
            return int(text)
        if t is ColumnType.DECIMAL:
            return Decimal(text)
        if t is ColumnType.FLOAT:
            return float(text)
        if t is ColumnType.BOOLEAN:
            lowered = text.strip().lower()
            if lowered in {"true", "t", "1", "yes", "y"}:
                return True
            if lowered in {"false", "f", "0", "no", "n"}:
                return False
            raise ValueError(f"not a boolean: {text!r}")
        if t is ColumnType.DATE:
            return date.fromisoformat(text)
        if t is ColumnType.DATETIME:
            return datetime.fromisoformat(text)
    except (ValueError, InvalidOperation) as e:
        raise CsvFormatError(f"{col.name}: cannot parse {text!r} as {col.raw_type}: {e}") from e
    return text


def read_csv(data: bytes | str, table: Table) -> pd.DataFrame:
    """Typed read. Empty fields become NULL (the csv module cannot tell `""` from an unquoted empty field)."""
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as e:
        raise CsvFormatError(f"{table.name}: empty CSV") from e
    expected = table.column_names
    if [h.lower() for h in header] != [c.lower() for c in expected]:
        raise CsvFormatError(f"{table.name}: header {header} does not match columns {expected}")
    columns: dict[str, list[Any]] = {c: [] for c in expected}
    for line_no, row in enumerate(reader, start=2):
        if not row:
            continue
        if len(row) != len(expected):
            raise CsvFormatError(
                f"{table.name}: line {line_no} has {len(row)} fields, expected {len(expected)}"
            )
        for col, cell in zip(table.columns, row, strict=True):
            columns[col.name].append(parse_value(col, cell))
    return pd.DataFrame({c: pd.Series(v, dtype=object) for c, v in columns.items()})


def to_zip_bytes(
    tables: dict[str, pd.DataFrame],
    schema: Schema,
    *,
    ddl: str | None = None,
    manifest: dict[str, Any] | None = None,
) -> bytes:
    """One CSV per table (`<Table>.csv`), plus `schema.ddl` and `manifest.json` when given."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for table in schema.tables:
            if table.name in tables:
                zf.writestr(f"{table.name}.csv", to_csv_bytes(tables[table.name], table))
        if ddl is not None:
            zf.writestr("schema.ddl", ddl)
        if manifest is not None:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
    return buffer.getvalue()
