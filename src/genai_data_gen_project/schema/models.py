"""Dialect-neutral schema IR produced by `schema.parser`; consumed by generation, validation, DDL emission.

Design notes (DECISIONS.md#2026-08-25-ddl-parsing-with-sqlglot):
- Identifiers keep their original case; lookups are case-insensitive (MySQL semantics, as in the samples).
- SQL fragments (defaults, check expressions) are stored as Postgres-dialect SQL text for `postgres_ddl`.
- The IR is plain data (pydantic) with a few convenience queries; nothing here depends on sqlglot.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ColumnType(StrEnum):
    """Normalised logical types. `raw_type` on the column keeps the original spelling."""

    INTEGER = "integer"
    BIGINT = "bigint"
    SMALLINT = "smallint"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOLEAN = "boolean"
    VARCHAR = "varchar"
    CHAR = "char"
    TEXT = "text"
    DATE = "date"
    DATETIME = "datetime"
    TIME = "time"
    ENUM = "enum"
    JSON = "json"
    UUID = "uuid"
    UNKNOWN = "unknown"

    @property
    def is_numeric(self) -> bool:
        return self.is_integer or self in {ColumnType.DECIMAL, ColumnType.FLOAT}

    @property
    def is_integer(self) -> bool:
        return self in {ColumnType.INTEGER, ColumnType.BIGINT, ColumnType.SMALLINT}

    @property
    def is_textual(self) -> bool:
        return self in {ColumnType.VARCHAR, ColumnType.CHAR, ColumnType.TEXT, ColumnType.ENUM}

    @property
    def is_temporal(self) -> bool:
        return self in {ColumnType.DATE, ColumnType.DATETIME, ColumnType.TIME}


class Column(BaseModel):
    name: str
    type: ColumnType
    raw_type: str = Field(description="Original type spelling, e.g. VARCHAR(255) or ENUM('a','b')")
    length: int | None = Field(default=None, description="VARCHAR/CHAR length")
    precision: int | None = Field(default=None, description="DECIMAL precision (total digits)")
    scale: int | None = Field(default=None, description="DECIMAL scale (fraction digits)")
    enum_values: list[str] = Field(default_factory=list)
    nullable: bool = True
    default: str | None = Field(default=None, description="Postgres SQL text: 0, 'Active', CURRENT_TIMESTAMP")
    auto_increment: bool = False
    primary_key: bool = False
    unique: bool = False
    check: str | None = Field(default=None, description="Inline CHECK expression as Postgres SQL text")
    comment: str | None = None

    @property
    def max_length(self) -> int | None:
        """Effective max text length: VARCHAR/CHAR length, or the longest ENUM literal."""
        if self.type is ColumnType.ENUM and self.enum_values:
            return max(len(v) for v in self.enum_values)
        return self.length


class ForeignKey(BaseModel):
    name: str | None = None
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    on_delete: str | None = None

    @property
    def is_composite(self) -> bool:
        return len(self.columns) > 1


class UniqueConstraint(BaseModel):
    name: str | None = None
    columns: list[str]


class CheckConstraint(BaseModel):
    name: str | None = None
    expression: str = Field(description="Postgres SQL text of the boolean expression")
    columns: list[str] = Field(default_factory=list, description="Column names referenced by the expression")


class Table(BaseModel):
    name: str
    columns: list[Column]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    unique_constraints: list[UniqueConstraint] = Field(default_factory=list)
    checks: list[CheckConstraint] = Field(default_factory=list)
    comment: str | None = None

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name.lower() == name.lower():
                return c
        raise KeyError(
            f"table {self.name!r} has no column {name!r} (columns: {', '.join(self.column_names)})"[:400]
        )

    def has_column(self, name: str) -> bool:
        return any(c.name.lower() == name.lower() for c in self.columns)

    def fk_for_column(self, name: str) -> ForeignKey | None:
        for fk in self.foreign_keys:
            if any(c.lower() == name.lower() for c in fk.columns):
                return fk
        return None

    def fk_is_nullable(self, fk: ForeignKey) -> bool:
        """A FK can be left NULL (and filled later) only if every participating column is nullable."""
        return all(self.column(c).nullable for c in fk.columns)

    @property
    def unique_column_sets(self) -> list[list[str]]:
        """All column sets that must be unique: PK, single-column UNIQUE, table-level UNIQUE."""
        sets: list[list[str]] = []
        if self.primary_key:
            sets.append(list(self.primary_key))
        sets.extend([c.name] for c in self.columns if c.unique and [c.name] not in sets)
        sets.extend(list(u.columns) for u in self.unique_constraints if list(u.columns) not in sets)
        return sets


class Schema(BaseModel):
    tables: list[Table]
    source_dialect: str = Field(description="Dialect the DDL was parsed with (mysql or postgres)")
    ignored_statements: list[str] = Field(
        default_factory=list, description="Skipped non-table statements (e.g. CREATE INDEX, USE, SET)"
    )
    notes: list[str] = Field(
        default_factory=list, description="Warnings worth showing: dialect fallback, unknown types"
    )

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]

    def table(self, name: str) -> Table:
        for t in self.tables:
            if t.name.lower() == name.lower():
                return t
        raise KeyError(f"schema has no table {name!r} (tables: {', '.join(self.table_names)})")

    def has_table(self, name: str) -> bool:
        return any(t.name.lower() == name.lower() for t in self.tables)

    def references_of(self, table_name: str) -> list[tuple[Table, ForeignKey]]:
        """(child table, fk) pairs whose FK points at `table_name`."""
        wanted = table_name.lower()
        return [(t, fk) for t in self.tables for fk in t.foreign_keys if fk.ref_table.lower() == wanted]

    def dependencies_of(self, table_name: str) -> list[str]:
        """Names of parent tables this table references (excluding itself)."""
        seen: list[str] = []
        for fk in self.table(table_name).foreign_keys:
            if fk.ref_table.lower() != table_name.lower() and fk.ref_table not in seen:
                seen.append(fk.ref_table)
        return seen
