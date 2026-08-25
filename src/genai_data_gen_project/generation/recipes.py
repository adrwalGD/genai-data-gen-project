"""Column recipes — the typed contract between planners (heuristics / Gemini) and the deterministic expander.

A `GenerationPlan` says, per table, how many rows to make and, per column, which recipe produces values and
how often the column is NULL. The same pydantic models are the structured-output schema for the LLM planner
(F3.2), so they stay JSON-friendly (discriminated union on `kind`, simple field types).

Pattern templates (`PatternRecipe.template`), implemented by the expander (F2.2):
  {col:name}            value of another column of the same row (must be generated earlier in column order)
  {col:name|slug}       modifiers: slug (ascii lowercase alnum), lower, upper, initial (first letter)
  {faker:provider}      a Faker provider call, e.g. {faker:free_email_domain}
  {seq}                 1-based row index
  #  ?  %               random digit / lowercase letter / uppercase letter
Derived expressions (`DerivedRecipe.expression`) are arithmetic over same-row columns with the functions
min, max, abs, round, randint(a, b) — evaluated by a restricted AST evaluator, never `eval`.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from ..schema.models import Column, ColumnType, Schema, Table


class Distribution(StrEnum):
    UNIFORM = "uniform"
    NORMAL = "normal"
    SKEWED_LOW = "skewed_low"  # most values near the minimum (e.g. fines, quantities)
    SKEWED_HIGH = "skewed_high"  # most values near the maximum (e.g. ratings, availability)


class IntRangeRecipe(BaseModel):
    kind: Literal["int_range"] = "int_range"
    min: int
    max: int
    distribution: Distribution = Distribution.UNIFORM

    @model_validator(mode="after")
    def _ordered(self) -> IntRangeRecipe:
        if self.max < self.min:
            raise ValueError(f"int_range max {self.max} < min {self.min}")
        return self


class DecimalRangeRecipe(BaseModel):
    kind: Literal["decimal_range"] = "decimal_range"
    min: float
    max: float
    scale: int | None = Field(default=None, description="Fraction digits; defaults to the column scale")
    distribution: Distribution = Distribution.UNIFORM

    @model_validator(mode="after")
    def _ordered(self) -> DecimalRangeRecipe:
        if self.max < self.min:
            raise ValueError(f"decimal_range max {self.max} < min {self.min}")
        return self


class SequenceRecipe(BaseModel):
    kind: Literal["sequence"] = "sequence"
    start: int = 1
    step: int = 1


class EnumWeightedRecipe(BaseModel):
    kind: Literal["enum_weighted"] = "enum_weighted"
    values: list[str] = Field(min_length=1)
    weights: list[float] | None = Field(default=None, description="Relative weights; None = uniform")

    @model_validator(mode="after")
    def _weights(self) -> EnumWeightedRecipe:
        if self.weights is not None:
            if len(self.weights) != len(self.values):
                raise ValueError("enum_weighted weights must match values length")
            if any(w < 0 for w in self.weights) or sum(self.weights) <= 0:
                raise ValueError("enum_weighted weights must be non-negative with a positive sum")
        return self


class BooleanRecipe(BaseModel):
    kind: Literal["boolean"] = "boolean"
    true_ratio: float = Field(default=0.5, ge=0.0, le=1.0)


class FakerRecipe(BaseModel):
    kind: Literal["faker"] = "faker"
    provider: str = Field(description="Faker provider method name, e.g. first_name, city, isbn13")
    kwargs: dict[str, str | int | float | bool] = Field(default_factory=dict)
    unique: bool = False


class PatternRecipe(BaseModel):
    kind: Literal["pattern"] = "pattern"
    template: str
    unique: bool = False


class DateWindowRecipe(BaseModel):
    kind: Literal["date_window"] = "date_window"
    start: date
    end: date
    after_column: str | None = Field(default=None, description="Same-row temporal column this must follow")
    min_days_after: int = Field(default=0, ge=0)
    max_days_after: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> DateWindowRecipe:
        if self.end < self.start:
            raise ValueError("date_window end < start")
        return self


class DateTimeWindowRecipe(BaseModel):
    kind: Literal["datetime_window"] = "datetime_window"
    start: datetime
    end: datetime
    after_column: str | None = None
    min_hours_after: int = Field(default=0, ge=0)
    max_hours_after: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> DateTimeWindowRecipe:
        if self.end < self.start:
            raise ValueError("datetime_window end < start")
        return self


class TextPoolRecipe(BaseModel):
    """Values come from a pool of realistic strings (LLM-generated in M3; Faker fallback offline)."""

    kind: Literal["text_pool"] = "text_pool"
    brief: str = Field(
        description="What the values are, e.g. 'realistic fiction and non-fiction book titles'"
    )
    unique: bool = False
    fallback_provider: str = Field(
        default="sentence", description="Faker provider used when no LLM is available"
    )
    fallback_kwargs: dict[str, str | int | float | bool] = Field(default_factory=dict)
    examples: list[str] = Field(default_factory=list)


class ForeignKeyRecipe(BaseModel):
    kind: Literal["fk"] = "fk"
    ref_table: str
    ref_column: str
    distribution: Distribution = Field(
        default=Distribution.UNIFORM, description="skewed_low = a few parents get most children"
    )


class DerivedRecipe(BaseModel):
    kind: Literal["derived"] = "derived"
    expression: str = Field(description="Arithmetic over same-row columns, e.g. 'quantity * unit_price'")


class ConstantRecipe(BaseModel):
    kind: Literal["constant"] = "constant"
    value: str | int | float | bool | None = None


class AggregateRecipe(BaseModel):
    """Parent column computed from its children after generation (total_amount = sum of subtotals)."""

    kind: Literal["aggregate"] = "aggregate"
    child_table: str
    child_fk_column: str = Field(description="FK column in the child table that points at this table")
    child_column: str | None = Field(
        default=None, description="Child column to aggregate (not needed for count)"
    )
    agg: Literal["sum", "count", "min", "max", "avg"] = "sum"
    default: float = Field(default=0, description="Value for parents without children")


ColumnRecipe = Annotated[
    IntRangeRecipe
    | DecimalRangeRecipe
    | SequenceRecipe
    | EnumWeightedRecipe
    | BooleanRecipe
    | FakerRecipe
    | PatternRecipe
    | DateWindowRecipe
    | DateTimeWindowRecipe
    | TextPoolRecipe
    | ForeignKeyRecipe
    | DerivedRecipe
    | ConstantRecipe
    | AggregateRecipe,
    Field(discriminator="kind"),
]


class ColumnPlan(BaseModel):
    column: str
    recipe: ColumnRecipe
    null_ratio: float = Field(default=0.0, ge=0.0, le=1.0, description="Share of NULLs; ignored for NOT NULL")
    rationale: str | None = None


class TablePlan(BaseModel):
    table: str
    rows: int = Field(ge=0)
    columns: list[ColumnPlan]
    description: str | None = None

    def column(self, name: str) -> ColumnPlan | None:
        return next((c for c in self.columns if c.column.lower() == name.lower()), None)


class GenerationPlan(BaseModel):
    tables: list[TablePlan]
    notes: list[str] = Field(
        default_factory=list, description="Planner remarks, e.g. which instructions applied"
    )

    def table(self, name: str) -> TablePlan | None:
        return next((t for t in self.tables if t.table.lower() == name.lower()), None)


# --- compatibility rules ------------------------------------------------------------------------------------
_ALLOWED: dict[ColumnType, set[str]] = {
    ColumnType.INTEGER: {"int_range", "sequence", "fk", "derived", "constant", "aggregate"},
    ColumnType.BIGINT: {"int_range", "sequence", "fk", "derived", "constant", "aggregate"},
    ColumnType.SMALLINT: {"int_range", "sequence", "fk", "derived", "constant", "aggregate"},
    ColumnType.DECIMAL: {"decimal_range", "int_range", "derived", "constant", "aggregate"},
    ColumnType.FLOAT: {"decimal_range", "int_range", "derived", "constant", "aggregate"},
    ColumnType.BOOLEAN: {"boolean", "constant", "derived"},
    ColumnType.VARCHAR: {"faker", "pattern", "text_pool", "enum_weighted", "constant", "fk", "derived"},
    ColumnType.CHAR: {"faker", "pattern", "text_pool", "enum_weighted", "constant", "fk", "derived"},
    ColumnType.TEXT: {"faker", "pattern", "text_pool", "enum_weighted", "constant", "derived"},
    ColumnType.ENUM: {"enum_weighted", "constant", "derived"},
    ColumnType.DATE: {"date_window", "constant", "derived"},
    ColumnType.DATETIME: {"datetime_window", "date_window", "constant", "derived"},
    ColumnType.TIME: {"pattern", "faker", "constant"},
    ColumnType.JSON: {"faker", "pattern", "constant"},
    ColumnType.UUID: {"faker", "pattern", "constant"},
    ColumnType.UNKNOWN: {"faker", "pattern", "text_pool", "constant"},
}


def _faker_has(provider: str) -> bool:
    from faker import Faker

    return callable(getattr(Faker(), provider, None))


def validate_plan(plan: GenerationPlan, schema: Schema) -> list[str]:
    """Return human-readable problems (empty list = plan is usable). Used to reject bad LLM suggestions."""
    problems: list[str] = []
    seen_tables: set[str] = set()
    for tp in plan.tables:
        if not schema.has_table(tp.table):
            problems.append(f"plan references unknown table {tp.table!r}")
            continue
        table = schema.table(tp.table)
        seen_tables.add(table.name.lower())
        seen_cols: set[str] = set()
        for cp in tp.columns:
            if not table.has_column(cp.column):
                problems.append(f"{table.name}: plan references unknown column {cp.column!r}")
                continue
            col = table.column(cp.column)
            seen_cols.add(col.name.lower())
            problems.extend(f"{table.name}.{col.name}: {p}" for p in _column_problems(table, col, cp, schema))
        for col in table.columns:
            if col.name.lower() not in seen_cols:
                problems.append(f"{table.name}.{col.name}: no recipe in plan")
    for table in schema.tables:
        if table.name.lower() not in seen_tables:
            problems.append(f"table {table.name!r} missing from plan")
    return problems


def _column_problems(table: Table, col: Column, cp: ColumnPlan, schema: Schema) -> list[str]:
    out: list[str] = []
    r = cp.recipe
    if r.kind not in _ALLOWED[col.type]:
        out.append(f"recipe {r.kind!r} is not allowed for type {col.type.value}")
        return out
    if cp.null_ratio > 0 and not col.nullable:
        out.append("null_ratio > 0 on a NOT NULL column")
    if isinstance(r, EnumWeightedRecipe) and col.type is ColumnType.ENUM:
        bad = [v for v in r.values if v not in col.enum_values]
        if bad:
            out.append(f"enum values {bad} are not in the column ENUM {col.enum_values}")
    if isinstance(r, EnumWeightedRecipe | ConstantRecipe) and col.max_length:
        values = (
            r.values if isinstance(r, EnumWeightedRecipe) else [str(r.value)] if r.value is not None else []
        )
        if any(len(v) > col.max_length for v in values):
            out.append(f"value longer than {col.max_length} characters")
    if isinstance(r, ConstantRecipe) and col.type is ColumnType.ENUM and r.value not in col.enum_values:
        out.append(f"constant {r.value!r} is not in the column ENUM")
    if isinstance(r, ForeignKeyRecipe):
        if not schema.has_table(r.ref_table) or not schema.table(r.ref_table).has_column(r.ref_column):
            out.append(f"fk target {r.ref_table}.{r.ref_column} does not exist")
        fk = table.fk_for_column(col.name)
        if fk is not None and fk.ref_table.lower() != r.ref_table.lower():
            out.append(f"fk target {r.ref_table} contradicts the schema FK → {fk.ref_table}")
    if (
        isinstance(r, IntRangeRecipe | DecimalRangeRecipe)
        and col.type is ColumnType.DECIMAL
        and col.precision
    ):
        limit = 10 ** (col.precision - (col.scale or 0))
        if abs(r.min) >= limit or abs(r.max) >= limit:
            out.append(f"range exceeds NUMERIC({col.precision},{col.scale}) capacity (< {limit})")
    if isinstance(r, DateWindowRecipe | DateTimeWindowRecipe) and r.after_column:
        out.extend(_temporal_ref_problems(table, r.after_column, schema))
    if isinstance(r, DerivedRecipe):
        out.extend(_derived_problems(table, r.expression, schema))
    if isinstance(r, AggregateRecipe):
        out.extend(_aggregate_problems(table, r, schema))
    if isinstance(r, FakerRecipe) and not _faker_has(r.provider):
        out.append(f"unknown Faker provider {r.provider!r}")
    if isinstance(r, TextPoolRecipe) and not _faker_has(r.fallback_provider):
        out.append(f"unknown Faker fallback provider {r.fallback_provider!r}")
    return out


def _parent_target(table: Table, fk_column: str, schema: Schema) -> Table | None:
    fk = table.fk_for_column(fk_column) if table.has_column(fk_column) else None
    return schema.table(fk.ref_table) if fk is not None and schema.has_table(fk.ref_table) else None


def _temporal_ref_problems(table: Table, ref: str, schema: Schema) -> list[str]:
    from .expressions import parse_parent_ref

    parent_ref = parse_parent_ref(ref)
    if parent_ref is None:
        if not table.has_column(ref):
            return [f"after_column {ref!r} does not exist"]
        if not table.column(ref).type.is_temporal:
            return [f"after_column {ref!r} is not a date/time column"]
        return []
    fk_col, col = parent_ref
    parent = _parent_target(table, fk_col, schema)
    if parent is None:
        return [f"after_column {ref!r}: {fk_col!r} is not a foreign key column"]
    if not parent.has_column(col) or not parent.column(col).type.is_temporal:
        return [f"after_column {ref!r}: {parent.name} has no date/time column {col!r}"]
    return []


def _derived_problems(table: Table, expression: str, schema: Schema) -> list[str]:
    from .expressions import ExpressionError, compile_expression

    try:
        compiled = compile_expression(expression)
    except ExpressionError as e:
        return [str(e)]
    out: list[str] = []
    for name in compiled.column_refs:
        if not table.has_column(name):
            out.append(f"derived expression references unknown column {name!r}")
    for fk_col, col in compiled.parent_refs:
        parent = _parent_target(table, fk_col, schema)
        if parent is None:
            out.append(f"parent({fk_col}) is not a foreign key column")
        elif not parent.has_column(col):
            out.append(f"parent({fk_col}).{col}: {parent.name} has no column {col!r}")
    return out


def _aggregate_problems(table: Table, r: AggregateRecipe, schema: Schema) -> list[str]:
    if not schema.has_table(r.child_table):
        return [f"aggregate child table {r.child_table!r} does not exist"]
    child = schema.table(r.child_table)
    fk = child.fk_for_column(r.child_fk_column) if child.has_column(r.child_fk_column) else None
    if fk is None or fk.ref_table.lower() != table.name.lower():
        return [f"aggregate: {child.name}.{r.child_fk_column} is not a foreign key to {table.name}"]
    if r.agg != "count":
        if r.child_column is None:
            return [f"aggregate {r.agg} needs child_column"]
        if not child.has_column(r.child_column) or not child.column(r.child_column).type.is_numeric:
            return [f"aggregate: {child.name} has no numeric column {r.child_column!r}"]
    return []
