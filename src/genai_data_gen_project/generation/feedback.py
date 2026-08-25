"""Feedback edits (F4.1): a typed `EditPlan` applied deterministically to one table, then revalidated.

Ops: set_values (constant or recipe on filtered rows), regenerate_column, add_rows (PKs continue, FKs sample
existing parents, aggregates refreshed), delete_rows (cascade to NOT NULL children, SET NULL for nullable
FKs), update_pool (new text-pool values). Filters are expressions in `generation/expressions.py` syntax
(`rating < 3`, `city == 'Kraków'`, `return_date is None`). Primary keys can never be edited. Every op is
checked against the schema before anything changes; the result is validated (constraints + consistency).
"""

from __future__ import annotations

import json
import random
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from ..llm.client import LLMBackend
from ..schema.models import Schema, Table
from ..schema.summary import table_summary
from ..storage import csvio
from ..storage.dataset import Dataset
from . import expander, planner, pools
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
def compile_filter(where: str | None, table: Table, schema: Schema) -> Compiled | None:
    if where is None or not where.strip():
        return None
    try:
        compiled = compile_expression(where)
    except ExpressionError as e:
        raise EditError(f"invalid filter {where!r}: {e}") from e
    for name in compiled.column_refs:
        if not table.has_column(name):
            raise EditError(f"filter {where!r} references unknown column {name!r} of {table.name}")
    for fk_col, pcol in compiled.parent_refs:
        fk = table.fk_for_column(fk_col)
        if fk is None:
            raise EditError(f"filter {where!r}: parent({fk_col}) is not a foreign key column of {table.name}")
        parent = schema.table(fk.ref_table)
        if not parent.has_column(pcol):
            raise EditError(
                f"filter {where!r}: {parent.name} has no column {pcol!r} "
                f"(columns: {', '.join(parent.column_names)})"
            )
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
        except (KeyError, TypeError, ValueError) as e:
            raise EditError(f"filter {compiled.source!r} cannot be evaluated on {table.name}: {e}") from e
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
    _check_ops(plan, table, gplan, schema)
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


def _check_ops(plan: EditPlan, table: Table, gplan: GenerationPlan, schema: Schema) -> None:
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
            compile_filter(op.where, table, schema)
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
            if compile_filter(op.where, table, schema) is None:
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
    positions = matching_positions(schema, tables, table, compile_filter(op.where, table, schema))
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
    positions = matching_positions(schema, tables, table, compile_filter(op.where, table, schema))
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


# --- natural-language feedback → EditPlan with Gemini (F4.2) --------------------------------------
FEEDBACK_SYSTEM = """You translate a user's feedback about ONE table of a synthetic SQL dataset into a precise
edit plan. Return JSON with `table`, `ops` (list) and a one-sentence `summary`. Operations:
- set_values: store the constant `value` in `column` for rows matching `where` (omit `where` = all rows).
  With `kind` + parameters instead of `value`, matching rows get new random values from that recipe.
- regenerate_column: new values for all rows of `column` from a recipe (`kind` required): faker (+ `provider`,
  optional `locale` like pl_PL), pattern (`template`, tokens: {col:first_name|slug}, {col:last_name|slug},
  {seq}, # digit, ? letter, % uppercase), enum_weighted (`values`, `weights`), int_range/decimal_range (`min`,
  `max`), date_window/datetime_window (`start`, `end` ISO), text_pool (`brief`, `examples`).
- add_rows: append `count` rows; `overrides` = [{column, value}] constants for the new rows.
- delete_rows: remove rows matching `where`; dependent child rows are handled automatically.
- update_pool: replace a free-text column (names, titles, descriptions, addresses) with new realistic values
  described by `brief` (the values are generated afterwards); optional `where`.
Filters (`where`) are Python-like expressions over this table's columns: `rating < 3`, `city == 'Kraków'`,
`status == 'A' or status == 'B'` (no `in`), `return_date is None`, `parent(customer_id).city == 'Kraków'`.
Rules: never touch primary key columns; ENUM columns only take the listed values; write numbers and dates as
strings ("3", "49.99", "2025-01-31"); keep the plan minimal and faithful to the feedback; if the feedback
cannot be mapped to these operations, return an empty `ops` list and explain in `summary`.
Any condition in the feedback (below, above, under, over, before, after, only, where, older, cheaper ...) MUST
become a `where` filter — never apply a conditional change to all rows.
Examples:
- "set all ratings below 3 to 3" → {"op": "set_values", "column": "rating", "where": "rating < 3",
  "value": "3"}
- "make all orders from 2024 delivered" → {"op": "set_values", "column": "order_status",
  "where": "order_date >= '2024-01-01' and order_date < '2025-01-01'", "value": "Delivered"}
- "regenerate emails as firstname.lastname@example.org" → {"op": "regenerate_column", "column": "email",
  "kind": "pattern", "template": "{col:first_name|slug}.{col:last_name|slug}@example.org", "unique": true}
- "add 15 cancelled orders" → {"op": "add_rows", "count": 15, "overrides": [{"column": "order_status",
  "value": "Cancelled"}]}
- "salaries between 50000 and 90000" → {"op": "set_values", "column": "salary", "kind": "decimal_range",
  "min": 50000, "max": 90000}
- "delete customers who never ordered" → {"op": "delete_rows", "where": "...", ...} only if expressible; else
  explain in `summary`."""


class DraftOverride(BaseModel):
    column: str
    value: str | None = None


class DraftOp(BaseModel):
    """Flat, LLM-friendly operation; converted to a typed EditOp by `draft_to_plan`."""

    op: Literal["set_values", "regenerate_column", "add_rows", "delete_rows", "update_pool"]
    column: str | None = None
    where: str | None = None
    value: str | None = None
    kind: planner.OverrideKind | None = None
    min: float | None = None
    max: float | None = None
    values: list[str] | None = None
    weights: list[float] | None = None
    true_ratio: float | None = None
    provider: str | None = None
    locale: str | None = None
    template: str | None = None
    unique: bool | None = None
    start: str | None = None
    end: str | None = None
    brief: str | None = None
    examples: list[str] | None = None
    count: int | None = None
    overrides: list[DraftOverride] = Field(default_factory=list)
    cascade: bool | None = None
    rationale: str | None = None


class EditPlanDraft(BaseModel):
    table: str
    ops: list[DraftOp] = Field(default_factory=list)
    summary: str | None = None


def build_feedback_prompt(dataset: Dataset, table: Table, feedback: str, *, sample_rows: int = 5) -> str:
    frame = dataset.tables[table.name]
    gplan = GenerationPlan.model_validate(dataset.plan) if dataset.plan else None
    recipes = ""
    if gplan and (tplan := gplan.table(table.name)):
        recipes = "\n".join(f"  {cp.column}: {cp.recipe.kind}" for cp in tplan.columns)
    rows = [
        {c: csvio.format_value(v) for c, v in zip(frame.columns, vals, strict=True)}
        for vals in frame.head(sample_rows).itertuples(index=False, name=None)
    ]
    parents = []
    for fk in table.foreign_keys:
        if len(fk.columns) == 1 and dataset.schema.has_table(fk.ref_table):
            parent = dataset.schema.table(fk.ref_table)
            parents.append(f"  parent({fk.columns[0]}) → {parent.name}({', '.join(parent.column_names)})")
    parent_block = "\n".join(parents) or "  (none)"
    return (
        f"TABLE ({len(frame)} rows):\n{table_summary(table)}\n\n"
        "PARENT ROWS usable in filters as parent(fk_column).column — use these exact column names:\n"
        f"{parent_block}\n\n"
        f"CURRENT RECIPES:\n{recipes}\n\n"
        f"SAMPLE ROWS:\n{json.dumps(rows, ensure_ascii=False, default=str)}\n\n"
        f"USER FEEDBACK:\n{feedback.strip()}\n\n"
        "Return the edit plan."
    )


def draft_to_plan(draft: EditPlanDraft, dataset: Dataset) -> EditPlan:
    schema = dataset.schema
    if not schema.has_table(draft.table):
        raise EditError(f"unknown table {draft.table!r} (tables: {', '.join(schema.table_names)})")
    table = schema.table(draft.table)
    if not draft.ops:
        raise EditError(f"no operations: {draft.summary or 'the feedback could not be mapped to edits'}")
    ops: list[EditOp] = []
    for d in draft.ops:
        if d.op in {"set_values", "regenerate_column", "update_pool"}:
            if not d.column or not table.has_column(d.column):
                raise EditError(f"{d.op}: unknown column {d.column!r} of {table.name}")
            column = table.column(d.column)
        if d.op == "set_values":
            recipe = _draft_recipe(d, table, column) if d.kind else None
            ops.append(SetValuesOp(column=column.name, where=d.where, value=d.value, recipe=recipe))
        elif d.op == "regenerate_column":
            if not d.kind:
                raise EditError(f"regenerate_column {column.name}: a recipe kind is required")
            ops.append(
                RegenerateColumnOp(column=column.name, recipe=_draft_recipe(d, table, column), where=d.where)
            )
        elif d.op == "add_rows":
            overrides: dict[str, Scalar] = {o.column: o.value for o in d.overrides}
            ops.append(AddRowsOp(count=d.count or 10, overrides=overrides))
        elif d.op == "delete_rows":
            if not d.where:
                raise EditError("delete_rows needs a `where` filter")
            ops.append(DeleteRowsOp(where=d.where, cascade=True if d.cascade is None else d.cascade))
        else:
            ops.append(
                UpdatePoolOp(
                    column=column.name, brief=d.brief or f"realistic values for {column.name}", where=d.where
                )
            )
    return EditPlan(table=table.name, ops=ops, summary=draft.summary)


def _draft_recipe(d: DraftOp, table: Table, column: Any) -> ColumnRecipe:
    override = planner.ColumnOverride(
        table=table.name,
        column=column.name,
        kind=d.kind or "constant",
        min=d.min,
        max=d.max,
        values=d.values,
        weights=d.weights,
        value=d.value,
        true_ratio=d.true_ratio,
        provider=d.provider,
        locale=d.locale,
        template=d.template,
        unique=d.unique,
        start=d.start,
        end=d.end,
        brief=d.brief,
        examples=d.examples,
    )
    try:
        return planner.to_recipe(override, column)
    except (ValueError, TypeError) as e:
        raise EditError(
            f"{d.op} {column.name}: invalid recipe parameters — {str(e).splitlines()[0][:160]}"
        ) from e


def plan_edit(
    dataset: Dataset,
    table: str,
    feedback: str,
    llm: LLMBackend,
    *,
    temperature: float = 0.2,
    fill_pools: bool = True,
) -> EditPlan:
    """Ask Gemini for an EditPlan (structured output), validate it, retry once with the error, fill pools."""
    schema = dataset.schema
    if not schema.has_table(table):
        raise EditError(f"unknown table {table!r} (tables: {', '.join(schema.table_names)})")
    table_obj = schema.table(table)
    gplan = GenerationPlan.model_validate(dataset.plan) if dataset.plan else None
    if gplan is None:
        raise EditError("dataset has no generation plan; regenerate it before editing")
    prompt = build_feedback_prompt(dataset, table_obj, feedback)
    draft = llm.generate_structured(EditPlanDraft, prompt, system=FEEDBACK_SYSTEM, temperature=temperature)
    try:
        plan = draft_to_plan(draft, dataset)
        _check_ops(plan, table_obj, gplan, schema)
        _check_conditions(plan, feedback)
    except EditError as first_error:
        retry_prompt = f"{prompt}\n\nYour previous plan was invalid: {first_error}. Return a corrected plan."
        draft = llm.generate_structured(
            EditPlanDraft, retry_prompt, system=FEEDBACK_SYSTEM, temperature=temperature
        )
        try:
            plan = draft_to_plan(draft, dataset)
            _check_ops(plan, table_obj, gplan, schema)
        except EditError as e:
            raise EditError(f"Gemini produced an invalid edit plan twice: {e}") from e
    if fill_pools:
        _fill_pool_values(plan, dataset, table_obj, llm)
    return plan


CONDITION_WORDS = re.compile(
    r"\b(below|above|under|over|less than|more than|greater|smaller|before|after|older|younger|cheaper|"
    r"more expensive|only|where|whose|which|that are|that have|with|without|between|except|but not)\b",
    re.IGNORECASE,
)


def _check_conditions(plan: EditPlan, feedback: str) -> None:
    """Feedback that states a condition must not be applied unconditionally (the model dropped the filter)."""
    if not CONDITION_WORDS.search(feedback):
        return
    unconditional = [
        op
        for op in plan.ops
        if isinstance(op, SetValuesOp | DeleteRowsOp | UpdatePoolOp) and not getattr(op, "where", None)
    ]
    if unconditional and not any(isinstance(op, AddRowsOp) for op in plan.ops):
        names = ", ".join(type(op).__name__ for op in unconditional)
        raise EditError(
            f"the feedback contains a condition but {names} has no `where` filter — "
            "express the condition as a filter"
        )


def _fill_pool_values(plan: EditPlan, dataset: Dataset, table: Table, llm: LLMBackend) -> None:
    frame = dataset.tables[table.name]
    for op in plan.ops:
        if not isinstance(op, UpdatePoolOp) or op.values:
            continue
        col = table.column(op.column)
        n = len(
            matching_positions(
                dataset.schema, dataset.tables, table, compile_filter(op.where, table, dataset.schema)
            )
        ) or len(frame)
        gplan = GenerationPlan.model_validate(dataset.plan) if dataset.plan else None
        current = gplan.table(table.name).column(col.name) if gplan and gplan.table(table.name) else None  # type: ignore[union-attr]
        unique = col.unique or (
            current is not None and isinstance(current.recipe, TextPoolRecipe) and current.recipe.unique
        )
        request = pools.PoolRequest(
            table=table.name,
            column=col.name,
            brief=op.brief,
            unique=bool(unique),
            size=n,
            max_length=col.max_length,
            table_context=table_summary(table),
        )
        op.values = pools.fetch_pool(llm, request)


def apply_feedback(
    dataset: Dataset, table: str, feedback: str, llm: LLMBackend, *, seed: int = 0, temperature: float = 0.2
) -> tuple[Dataset, EditResult, EditPlan]:
    """One call for the UI: feedback text → Gemini EditPlan → applied dataset + result."""
    plan = plan_edit(dataset, table, feedback, llm, temperature=temperature)
    new_dataset, result = apply(plan, dataset, seed=seed)
    return new_dataset, result, plan
