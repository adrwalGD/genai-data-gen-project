"""Feedback edits (F4.1): a typed `EditPlan` applied deterministically to one table, then revalidated.

Ops: set_values (constant or recipe on filtered rows), regenerate_column, add_rows (PKs continue, FKs sample
existing parents, aggregates refreshed), delete_rows (cascade to NOT NULL children, SET NULL for nullable
FKs), update_pool (new text-pool values). Filters are expressions in `generation/expressions.py` syntax
(`rating < 3`, `city == 'Kraków'`, `return_date is None`). Primary keys can never be edited. Every op is
checked against the schema before anything changes; the result is validated (constraints + consistency).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from ..schema.models import Schema, Table
from ..storage.dataset import Dataset
from . import expander
from .consistency import check_consistency
from .expressions import Compiled, ExpressionError, NullResult, compile_expression
from .recipes import ColumnRecipe, GenerationPlan, SequenceRecipe, TextPoolRecipe
from .validator import ValidationReport, validate


class EditError(ValueError):
    """The plan cannot be applied (unknown table/column, bad filter, PK edit, children without cascade)."""


Scalar = str | int | float | bool | None


class SetValuesOp(BaseModel):
    op: Literal["set_values"] = "set_values"
    column: str
    where: str | None = Field(default=None, description="Row filter expression; None = all rows")
    value: Scalar = Field(default=None, description="Constant to store (used when recipe is None)")
    recipe: ColumnRecipe | None = Field(default=None, description="Regenerate matching rows with this recipe")


class RegenerateColumnOp(BaseModel):
    op: Literal["regenerate_column"] = "regenerate_column"
    column: str
    recipe: ColumnRecipe
    where: str | None = None


class AddRowsOp(BaseModel):
    op: Literal["add_rows"] = "add_rows"
    count: int = Field(ge=1, le=5000)
    overrides: dict[str, Scalar] = Field(default_factory=dict, description="Constant values for some columns")


class DeleteRowsOp(BaseModel):
    op: Literal["delete_rows"] = "delete_rows"
    where: str
    cascade: bool = Field(
        default=True, description="Delete dependent children (NOT NULL FKs); SET NULL otherwise"
    )


class UpdatePoolOp(BaseModel):
    op: Literal["update_pool"] = "update_pool"
    column: str
    brief: str
    values: list[str] = Field(default_factory=list, description="Pool values (filled by Gemini in F4.2)")
    where: str | None = None


EditOp = Annotated[
    SetValuesOp | RegenerateColumnOp | AddRowsOp | DeleteRowsOp | UpdatePoolOp, Field(discriminator="op")
]


class EditPlan(BaseModel):
    table: str
    ops: list[EditOp] = Field(min_length=1)
    summary: str | None = Field(default=None, description="One sentence for the UI history")


class EditResult(BaseModel):
    dataset_id: str
    table: str
    rows_before: int
    rows_after: int
    affected_rows: int
    report: ValidationReport
    summary: str
    cascaded: dict[str, int] = Field(default_factory=dict, description="child table → rows deleted or nulled")


# --- filters --------------------------------------------------------------------------------------
def compile_filter(where: str | None, table: Table) -> Compiled | None:
    if where is None or not where.strip():
        return None
    try:
        compiled = compile_expression(where)
    except ExpressionError as e:
        raise EditError(f"invalid filter {where!r}: {e}") from e
    for name in compiled.column_refs:
        if not table.has_column(name):
            raise EditError(f"filter {where!r} references unknown column {name!r} of {table.name}")
    for fk_col, _ in compiled.parent_refs:
        if table.fk_for_column(fk_col) is None:
            raise EditError(f"filter {where!r}: parent({fk_col}) is not a foreign key column")
    return compiled


def matching_positions(
    schema: Schema, tables: dict[str, pd.DataFrame], table: Table, compiled: Compiled | None
) -> list[int]:
    frame = tables[table.name]
    if compiled is None:
        return list(range(len(frame)))
    rng = random.Random(0)
    positions: list[int] = []
    rows = [dict(zip(frame.columns, vals, strict=True)) for vals in frame.itertuples(index=False, name=None)]
    for i, row in enumerate(rows):
        parent = _parent_lookup(schema, tables, table, row)
        try:
            result = compiled.evaluate(row, parent, rng)
        except NullResult:
            result = None
        if result:
            positions.append(i)
    return positions


def _parent_lookup(schema: Schema, tables: dict[str, pd.DataFrame], table: Table, row: dict[str, Any]) -> Any:
    def lookup(fk_col: str, pcol: str) -> Any:
        fk = table.fk_for_column(fk_col)
        if fk is None:
            return None
        value = row.get(table.column(fk_col).name)
        if value is None:
            return None
        parent = schema.table(fk.ref_table)
        ref_col = parent.column(fk.ref_columns[fk.columns.index(table.column(fk_col).name)]).name
        pframe = tables[parent.name]
        matches = pframe.index[pframe[ref_col] == value].tolist()
        return None if not matches else pframe.at[matches[0], parent.column(pcol).name]

    return lookup


# --- apply ----------------------------------------------------------------------------------------
def apply(plan: EditPlan, dataset: Dataset, *, seed: int = 0) -> tuple[Dataset, EditResult]:
    """Apply the plan to a copy of the dataset; returns (new dataset, result with validation report)."""
    schema = dataset.schema
    if not schema.has_table(plan.table):
        raise EditError(f"unknown table {plan.table!r} (tables: {', '.join(schema.table_names)})")
    table = schema.table(plan.table)
    gplan = GenerationPlan.model_validate(dataset.plan) if dataset.plan else None
    if gplan is None:
        raise EditError("dataset has no generation plan; regenerate it before editing")
    tables = {name: frame.copy() for name, frame in dataset.tables.items()}
    _check_ops(plan, table, gplan)
    rows_before = len(tables[table.name])
    affected = 0
    cascaded: dict[str, int] = {}
    for index, op in enumerate(plan.ops):
        op_seed = seed * 1000 + index
        if isinstance(op, SetValuesOp | RegenerateColumnOp | UpdatePoolOp):
            affected += _apply_column_op(schema, gplan, tables, table, op, op_seed)
        elif isinstance(op, AddRowsOp):
            affected += _apply_add_rows(schema, gplan, tables, table, op, op_seed)
        elif isinstance(op, DeleteRowsOp):
            removed, child_counts = _apply_delete(schema, tables, table, op)
            affected += removed
            for name, count in child_counts.items():
                cascaded[name] = cascaded.get(name, 0) + count
    expander.recompute_derived(schema, gplan, tables)  # e.g. subtotals follow an edited price
    expander.recompute_aggregates(schema, gplan, tables)
    report = validate(schema, tables)
    report.issues.extend(check_consistency(schema, gplan, tables))
    report.ok = not report.issues
    summary = plan.summary or _describe(plan)
    history = list(dataset.params.get("edits", []))
    history.append({"at": datetime.now(UTC).replace(microsecond=0).isoformat(), "table": table.name,
                    "summary": summary, "ops": [op.model_dump(mode="json") for op in plan.ops],
                    "affected_rows": affected, "ok": report.ok})  # fmt: skip
    new_dataset = Dataset(
        id=dataset.id,
        name=dataset.name,
        ddl=dataset.ddl,
        schema=schema,
        tables=tables,
        plan=dataset.plan,
        report=report.model_dump(mode="json"),
        instructions=dataset.instructions,
        params={**dataset.params, "edits": history},
        created_at=dataset.created_at,
    )
    result = EditResult(
        dataset_id=dataset.id,
        table=table.name,
        rows_before=rows_before,
        rows_after=len(tables[table.name]),
        affected_rows=affected,
        report=report,
        summary=summary,
        cascaded=cascaded,
    )
    return new_dataset, result


def _check_ops(plan: EditPlan, table: Table, gplan: GenerationPlan) -> None:
    pk = {c.lower() for c in table.primary_key}
    for op in plan.ops:
        if isinstance(op, SetValuesOp | RegenerateColumnOp | UpdatePoolOp):
            if not table.has_column(op.column):
                raise EditError(
                    f"{table.name} has no column {op.column!r} (columns: {', '.join(table.column_names)})"
                )
            if op.column.lower() in pk:
                raise EditError(f"{table.name}.{op.column} is a primary key and cannot be edited")
            if table.fk_for_column(op.column) is not None and not isinstance(op, SetValuesOp):
                raise EditError(
                    f"{table.name}.{op.column} is a foreign key; only set_values with a constant is allowed"
                )
            compile_filter(op.where, table)
            if (
                isinstance(op, SetValuesOp)
                and op.recipe is None
                and op.value is None
                and not table.column(op.column).nullable
            ):
                raise EditError(f"{table.name}.{op.column} is NOT NULL; provide a value or a recipe")
        elif isinstance(op, AddRowsOp):
            for name in op.overrides:
                if not table.has_column(name):
                    raise EditError(f"{table.name} has no column {name!r}")
                if name.lower() in pk:
                    raise EditError(f"{table.name}.{name} is a primary key; ids are assigned automatically")
        elif isinstance(op, DeleteRowsOp):
            if compile_filter(op.where, table) is None:
                raise EditError(
                    "delete_rows needs a filter (use where='True' to delete everything explicitly)"
                )
    if gplan.table(table.name) is None:
        raise EditError(f"generation plan has no entry for {table.name}")


def _apply_column_op(
    schema: Schema,
    gplan: GenerationPlan,
    tables: dict[str, pd.DataFrame],
    table: Table,
    op: SetValuesOp | RegenerateColumnOp | UpdatePoolOp,
    seed: int,
) -> int:
    col = table.column(op.column)
    positions = matching_positions(schema, tables, table, compile_filter(op.where, table))
    if not positions:
        return 0
    frame = tables[table.name]
    if isinstance(op, SetValuesOp) and op.recipe is None:
        value = expander.conform_value(col, op.value)
        if col.type.value == "enum" and value is not None and str(op.value) not in col.enum_values:
            raise EditError(f"{table.name}.{col.name}: {op.value!r} is not one of {col.enum_values}")
        if value is None and not col.nullable:
            raise EditError(f"{table.name}.{col.name} is NOT NULL")
        values: list[Any] = [value] * len(positions)
    else:
        pools = None
        if isinstance(op, UpdatePoolOp):
            if not op.values:
                raise EditError(
                    f"update_pool for {table.name}.{col.name} has no values (Gemini fills them in F4.2)"
                )
            planned = gplan.table(table.name)
            planned_col = planned.column(col.name) if planned else None
            current = planned_col.recipe if planned_col else None
            pool_unique = col.unique or (isinstance(current, TextPoolRecipe) and current.unique)
            fallback = current.fallback_provider if isinstance(current, TextPoolRecipe) else "sentence"
            recipe: ColumnRecipe = TextPoolRecipe(
                brief=op.brief, unique=bool(pool_unique), fallback_provider=fallback
            )
            pools = {(table.name, col.name): list(op.values)}
        else:
            recipe = op.recipe  # type: ignore[assignment]
        try:
            values = expander.regenerate_values(
                schema, tables, table.name, col.name, recipe, row_positions=positions, seed=seed, pools=pools
            )
        except expander.ExpansionError as e:
            raise EditError(str(e)) from e
    column_values = frame[col.name].tolist()
    for pos, value in zip(positions, values, strict=True):
        column_values[pos] = value
    frame[col.name] = pd.Series(column_values, dtype=object)
    expander.enforce_unique(schema, gplan, tables, table.name, seed=seed)
    return len(positions)


def _apply_add_rows(
    schema: Schema,
    gplan: GenerationPlan,
    tables: dict[str, pd.DataFrame],
    table: Table,
    op: AddRowsOp,
    seed: int,
) -> int:
    try:
        new_rows = expander.generate_rows(schema, gplan, tables, table.name, op.count, seed=seed)
    except expander.ExpansionError as e:
        raise EditError(str(e)) from e
    for name, value in op.overrides.items():
        col = table.column(name)
        conformed = expander.conform_value(col, value)
        if col.type.value == "enum" and conformed is not None and str(value) not in col.enum_values:
            raise EditError(f"{table.name}.{col.name}: {value!r} is not one of {col.enum_values}")
        new_rows[col.name] = pd.Series([conformed] * len(new_rows), dtype=object)
    tables[table.name] = pd.concat([tables[table.name], new_rows], ignore_index=True)
    expander.enforce_unique(schema, gplan, tables, table.name, seed=seed)
    return op.count


def _apply_delete(
    schema: Schema, tables: dict[str, pd.DataFrame], table: Table, op: DeleteRowsOp
) -> tuple[int, dict[str, int]]:
    positions = matching_positions(schema, tables, table, compile_filter(op.where, table))
    if not positions:
        return 0, {}
    frame = tables[table.name]
    removed_keys = (
        {tuple(frame.iloc[i][c] for c in table.primary_key) for i in positions}
        if table.primary_key
        else set()
    )
    child_counts: dict[str, int] = {}
    _cascade(schema, tables, table, removed_keys, op.cascade, child_counts)
    tables[table.name] = frame.drop(index=frame.index[positions]).reset_index(drop=True)
    return len(positions), child_counts


def _cascade(
    schema: Schema,
    tables: dict[str, pd.DataFrame],
    parent: Table,
    removed_keys: set[tuple[Any, ...]],
    cascade: bool,
    counts: dict[str, int],
) -> None:
    if not removed_keys:
        return
    for child, fk in schema.references_of(parent.name):
        cframe = tables[child.name]
        ref_index = [parent.column(c).name for c in fk.ref_columns]
        if ref_index != list(parent.primary_key):
            continue
        keys = list(zip(*(cframe[child.column(c).name].tolist() for c in fk.columns), strict=True))
        hits = [i for i, key in enumerate(keys) if key in removed_keys]
        if not hits:
            continue
        if child.fk_is_nullable(fk):
            for c in fk.columns:
                values = cframe[child.column(c).name].tolist()
                for i in hits:
                    values[i] = None
                cframe[child.column(c).name] = pd.Series(values, dtype=object)
            counts[child.name] = counts.get(child.name, 0) + len(hits)
            continue
        if not cascade:
            raise EditError(
                f"{len(hits)} row(s) in {child.name} reference the rows to delete; "
                "enable cascade or delete them first"
            )
        child_keys = (
            {tuple(cframe.iloc[i][c] for c in child.primary_key) for i in hits}
            if child.primary_key
            else set()
        )
        if child.name != parent.name:
            _cascade(schema, tables, child, child_keys, cascade, counts)
        tables[child.name] = cframe.drop(index=cframe.index[hits]).reset_index(drop=True)
        counts[child.name] = counts.get(child.name, 0) + len(hits)


def _describe(plan: EditPlan) -> str:
    parts = []
    for op in plan.ops:
        if isinstance(op, SetValuesOp):
            parts.append(f"set {op.column}" + (f" where {op.where}" if op.where else ""))
        elif isinstance(op, RegenerateColumnOp):
            parts.append(f"regenerate {op.column}")
        elif isinstance(op, AddRowsOp):
            parts.append(f"add {op.count} rows")
        elif isinstance(op, DeleteRowsOp):
            parts.append(f"delete rows where {op.where}")
        else:
            parts.append(f"new values for {op.column}")
    return f"{plan.table}: " + "; ".join(parts)


__all__ = [
    "AddRowsOp",
    "DeleteRowsOp",
    "EditError",
    "EditOp",
    "EditPlan",
    "EditResult",
    "RegenerateColumnOp",
    "SequenceRecipe",
    "SetValuesOp",
    "UpdatePoolOp",
    "apply",
    "compile_filter",
    "matching_positions",
]
