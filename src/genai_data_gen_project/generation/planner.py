"""LLM planner (F3.2): Gemini refines the heuristic plan from the schema summary + user instructions.

Design (DECISIONS.md#2026-08-25-hybrid-data-generation): the model does not emit a full plan. It returns
`PlannerOutput` — per-table row counts plus a list of column *overrides* with flat optional fields — which
`merge()` converts into typed recipes and validates against the schema. Anything invalid is dropped with a
reason in `GenerationPlan.notes`, so the LLM can improve realism but can never break integrity.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..llm.client import LLMBackend
from ..schema.models import Column, Schema
from ..schema.summary import schema_summary
from . import heuristics
from .recipes import (
    BooleanRecipe,
    ColumnPlan,
    ColumnRecipe,
    ConstantRecipe,
    DateTimeWindowRecipe,
    DateWindowRecipe,
    DecimalRangeRecipe,
    Distribution,
    EnumWeightedRecipe,
    FakerRecipe,
    ForeignKeyRecipe,
    GenerationPlan,
    IntRangeRecipe,
    PatternRecipe,
    SequenceRecipe,
    TextPoolRecipe,
    constant_problem,
    validate_plan,
)

_log = logging.getLogger(__name__)
MAX_ROW_MULTIPLIER = 5  # planner may grow a table to at most 5x the requested rows

OverrideKind = Literal[
    "int_range",
    "decimal_range",
    "enum_weighted",
    "boolean",
    "faker",
    "pattern",
    "date_window",
    "datetime_window",
    "text_pool",
    "constant",
    "null_ratio_only",
]


class ColumnOverride(BaseModel):
    """One column the model wants to change. Only the fields relevant to `kind` need to be set."""

    table: str
    column: str
    kind: OverrideKind
    rationale: str | None = None
    null_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    # numeric ranges
    min: float | None = None
    max: float | None = None
    distribution: Distribution | None = None
    # enum_weighted / constant
    values: list[str] | None = None
    weights: list[float] | None = None
    value: str | None = None
    # boolean
    true_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    # faker / pattern
    provider: str | None = None
    locale: str | None = Field(default=None, description="Faker locale for faker kind, e.g. pl_PL")
    template: str | None = None
    unique: bool | None = None
    # date / datetime windows (ISO strings)
    start: str | None = None
    end: str | None = None
    after_column: str | None = None
    min_days_after: int | None = Field(default=None, ge=0)
    max_days_after: int | None = Field(default=None, ge=0)
    # text pools
    brief: str | None = None
    examples: list[str] | None = None


class TableRows(BaseModel):
    table: str
    rows: int = Field(ge=0)


class PlannerOutput(BaseModel):
    table_rows: list[TableRows] = Field(
        default_factory=list, description="Only tables whose row count should change"
    )
    overrides: list[ColumnOverride] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list, description="How the instructions were interpreted")


PLANNER_SYSTEM = """You are a senior data architect preparing a synthetic-data generation plan for a SQL
schema. You receive the schema, the current heuristic plan (one line per column: recipe kind and key
parameters) and the user's instructions. Return ONLY the changes needed so the data is realistic and follows
the instructions.

Rules:
- Never touch primary keys or foreign keys (they come from sequences / parent tables); you may only set
  null_ratio for nullable foreign keys via kind "null_ratio_only".
- Respect column types: int_range for integers, decimal_range for decimals, enum_weighted only with values
  that belong to the column's ENUM (or short strings for VARCHAR columns), boolean for booleans, date_window
  for dates, datetime_window for timestamps, text_pool/faker/pattern for text.
- Respect NOT NULL (null_ratio must stay 0 there) and VARCHAR lengths.
- date_window/datetime_window may set after_column to a date column of this table or to
  parent(fk_column).column of the parent row (e.g. parent(customer_id).registration_date).
- Use text_pool (with a precise brief and 3-5 examples) for names, titles, descriptions, addresses that
  should look real; use faker providers (first_name, last_name, email, city, street_address, company, job,
  phone_number, postcode, country, url, isbn13, sentence, paragraph, catch_phrase, word) for generic values;
  use pattern for formatted codes ("#"=digit, "?"=lowercase letter, "%"=uppercase letter, {col:other|slug},
  {seq}).
- Row counts: keep the requested default unless the instructions ask for more or realism demands it (e.g.
  several order items per order); never more than 5x the default — larger requests are clamped.
- Localization: Faker providers are English/US unless you set `locale` (e.g. pl_PL, de_DE, fr_FR) on a faker
  override — use it for names, addresses and cities of a given country. For culture-specific titles, dishes or
  descriptions use text_pool with a brief and 3-5 examples in the requested language.
- A text_pool override keeps the column's uniqueness; you cannot make a unique column non-unique.
- constant values must be valid literals for the column type (numbers for DECIMAL/INT, ISO dates for DATE).
- Prefer few, high-impact overrides. Explain each in one short rationale. Put general remarks in notes."""


def plan_with_llm(
    schema: Schema,
    instructions: str | None,
    rows_per_table: int | dict[str, int],
    llm: LLMBackend,
    *,
    temperature: float = 0.4,
    base: GenerationPlan | None = None,
    max_rows: int | None = None,
) -> tuple[GenerationPlan, PlannerOutput]:
    """Heuristic plan refined by Gemini. Returns (merged plan, raw planner output)."""
    base = base or heuristics.plan(schema, rows_per_table)
    prompt = build_prompt(schema, base, instructions, rows_per_table)
    output = llm.generate_structured(PlannerOutput, prompt, system=PLANNER_SYSTEM, temperature=temperature)
    merged = merge(base, output, schema, max_rows=max_rows)
    return merged, output


def build_prompt(
    schema: Schema, base: GenerationPlan, instructions: str | None, rows_per_table: int | dict[str, int]
) -> str:
    default_rows = rows_per_table if isinstance(rows_per_table, int) else "per table as listed"
    text = (
        instructions.strip()
        if instructions and instructions.strip()
        else "(none - just make the data realistic)"
    )
    return (
        f"SCHEMA:\n{schema_summary(schema)}\n\n"
        f"CURRENT PLAN (default {default_rows} rows per table):\n{plan_summary(base)}\n\n"
        f"USER INSTRUCTIONS:\n{text}\n\n"
        "Return the overrides and row-count changes."
    )


def plan_summary(plan: GenerationPlan) -> str:
    lines: list[str] = []
    for tp in plan.tables:
        lines.append(f"TABLE {tp.table} rows={tp.rows}")
        for cp in tp.columns:
            r = cp.recipe
            params = {
                k: v
                for k, v in r.model_dump(mode="json").items()
                if k != "kind" and v not in (None, [], {}, False)
            }
            short = ", ".join(f"{k}={_short(v)}" for k, v in list(params.items())[:4])
            null = f" null_ratio={cp.null_ratio:.2f}" if cp.null_ratio else ""
            lines.append(f"  {cp.column}: {r.kind}({short}){null}")
    return "\n".join(lines)


def _short(value: object) -> str:
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "..."


def merge(
    base: GenerationPlan, output: PlannerOutput, schema: Schema, *, max_rows: int | None = None
) -> GenerationPlan:
    """Apply valid overrides/row counts onto a copy of `base`; record every rejection in notes."""
    plan = base.model_copy(deep=True)
    notes = list(plan.notes) + [f"llm: {n}" for n in output.notes]
    for tr in output.table_rows:
        tp = plan.table(tr.table)
        if tp is None:
            notes.append(f"dropped row count for unknown table {tr.table!r}")
            continue
        rows = tr.rows
        relative_cap = tp.rows * MAX_ROW_MULTIPLIER
        if rows > relative_cap:
            notes.append(
                f"row count for {tp.table} clamped to {relative_cap} "
                f"({MAX_ROW_MULTIPLIER}x the requested {tp.rows}; planner asked for {rows})"
            )
            rows = relative_cap
        if max_rows is not None and rows > max_rows:
            notes.append(f"row count for {tp.table} clamped to {max_rows} (planner asked for {rows})")
            rows = max_rows
        tp.rows = rows
    applied = 0
    for ov in output.overrides:
        problem = _apply_override(plan, ov, schema)
        if problem:
            notes.append(f"dropped override {ov.table}.{ov.column} ({ov.kind}): {problem}")
            _log.info("planner override rejected: %s.%s — %s", ov.table, ov.column, problem)
        else:
            applied += 1
    notes.append(f"llm overrides applied: {applied}/{len(output.overrides)}")
    plan.notes = notes
    return plan


def _apply_override(plan: GenerationPlan, ov: ColumnOverride, schema: Schema) -> str | None:
    tp = plan.table(ov.table)
    if tp is None or not schema.has_table(ov.table):
        return "unknown table"
    cp = tp.column(ov.column)
    table = schema.table(ov.table)
    if cp is None or not table.has_column(ov.column):
        return "unknown column"
    col = table.column(ov.column)
    current = cp.recipe
    if isinstance(current, SequenceRecipe | ForeignKeyRecipe) and ov.kind != "null_ratio_only":
        return "primary/foreign key recipes cannot be overridden"
    trial = cp.model_copy(deep=True)
    if ov.kind != "null_ratio_only":
        try:
            trial.recipe = _to_recipe(_inherit(ov, current), col)
        except (ValueError, TypeError) as e:  # pydantic validation or date parsing
            return f"invalid parameters: {str(e).splitlines()[0][:120]}"
    if ov.null_ratio is not None:
        if ov.null_ratio > 0 and not col.nullable:
            return "null_ratio > 0 on a NOT NULL column"
        trial.null_ratio = ov.null_ratio
    if ov.rationale:
        trial.rationale = ov.rationale
    # validate in isolation: swap in, validate the whole plan, revert on problems
    original = cp.model_copy(deep=True)
    cp.recipe, cp.null_ratio, cp.rationale = trial.recipe, trial.null_ratio, trial.rationale
    problems = [p for p in validate_plan(plan, schema) if p.startswith(f"{table.name}.{col.name}:")]
    if problems:
        cp.recipe, cp.null_ratio, cp.rationale = original.recipe, original.null_ratio, original.rationale
        return "; ".join(p.split(": ", 1)[1] for p in problems)
    return None


def _inherit(ov: ColumnOverride, current: ColumnRecipe) -> ColumnOverride:
    """Fill fields the model omitted from the recipe it is replacing (dates, text pools, faker)."""
    ov = _inherit_dates(ov, current)
    if ov.kind == "text_pool" and isinstance(current, TextPoolRecipe):
        updates: dict[str, object] = {}
        if ov.unique is None or (current.unique and not ov.unique):
            updates["unique"] = current.unique  # never make a unique column non-unique
        if ov.provider is None:
            updates["provider"] = current.fallback_provider
        if not ov.examples and current.examples:
            updates["examples"] = list(current.examples)
        ov = ov.model_copy(update=updates)
    if (
        ov.kind == "faker"
        and isinstance(current, FakerRecipe | TextPoolRecipe | PatternRecipe)
        and ov.unique is None
    ):
        ov = ov.model_copy(update={"unique": getattr(current, "unique", False)})
    return ov


def _inherit_dates(ov: ColumnOverride, current: ColumnRecipe) -> ColumnOverride:
    """A date/datetime override that only tweaks nullability/gaps keeps the current window and anchor."""
    if ov.kind not in {"date_window", "datetime_window"}:
        return ov
    if not isinstance(current, DateWindowRecipe | DateTimeWindowRecipe):
        return ov
    updates: dict[str, object] = {}
    if ov.start is None:
        updates["start"] = current.start.isoformat()
    if ov.end is None:
        updates["end"] = current.end.isoformat()
    if ov.after_column is None and current.after_column:
        updates["after_column"] = current.after_column
    if ov.min_days_after is None:
        gap = (
            current.min_days_after if isinstance(current, DateWindowRecipe) else current.min_hours_after // 24
        )
        updates["min_days_after"] = gap
    if ov.max_days_after is None:
        gap_max = (
            current.max_days_after
            if isinstance(current, DateWindowRecipe)
            else (None if current.max_hours_after is None else current.max_hours_after // 24)
        )
        updates["max_days_after"] = gap_max
    return ov.model_copy(update=updates)


def _to_recipe(ov: ColumnOverride, col: Column) -> ColumnRecipe:
    column_scale = col.scale
    dist = ov.distribution or Distribution.UNIFORM
    match ov.kind:
        case "int_range":
            _need(ov, "min", "max")
            assert ov.min is not None and ov.max is not None
            return IntRangeRecipe(min=int(ov.min), max=int(ov.max), distribution=dist)
        case "decimal_range":
            _need(ov, "min", "max")
            assert ov.min is not None and ov.max is not None
            return DecimalRangeRecipe(
                min=float(ov.min), max=float(ov.max), scale=column_scale, distribution=dist
            )
        case "enum_weighted":
            _need(ov, "values")
            return EnumWeightedRecipe(values=list(ov.values or []), weights=ov.weights)
        case "boolean":
            return BooleanRecipe(true_ratio=0.5 if ov.true_ratio is None else ov.true_ratio)
        case "faker":
            _need(ov, "provider")
            return FakerRecipe(provider=str(ov.provider), unique=bool(ov.unique), locale=ov.locale)
        case "pattern":
            _need(ov, "template")
            return PatternRecipe(template=str(ov.template), unique=bool(ov.unique))
        case "date_window":
            _need(ov, "start", "end")
            return DateWindowRecipe(
                start=date.fromisoformat(str(ov.start)[:10]),
                end=date.fromisoformat(str(ov.end)[:10]),
                after_column=ov.after_column,
                min_days_after=ov.min_days_after or 0,
                max_days_after=ov.max_days_after,
            )
        case "datetime_window":
            _need(ov, "start", "end")
            return DateTimeWindowRecipe(
                start=_parse_dt(str(ov.start)),
                end=_parse_dt(str(ov.end)),
                after_column=ov.after_column,
                min_hours_after=(ov.min_days_after or 0) * 24,
                max_hours_after=None if ov.max_days_after is None else ov.max_days_after * 24,
            )
        case "text_pool":
            _need(ov, "brief")
            return TextPoolRecipe(
                brief=str(ov.brief),
                unique=bool(ov.unique),
                fallback_provider=ov.provider or "sentence",
                examples=list(ov.examples or [])[:8],
            )
        case "constant":
            reason = constant_problem(col, ov.value)
            if reason:
                raise ValueError(f"constant {ov.value!r} does not fit {col.raw_type}: {reason}")
            return ConstantRecipe(value=ov.value)
    raise ValueError(f"unsupported override kind {ov.kind!r}")


def _need(ov: ColumnOverride, *fields: str) -> None:
    missing = [f for f in fields if getattr(ov, f) is None]
    if missing:
        raise ValueError(f"{ov.kind} requires {', '.join(missing)}")


def _parse_dt(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())


__all__ = [
    "ColumnOverride",
    "PlannerOutput",
    "TableRows",
    "build_prompt",
    "merge",
    "plan_summary",
    "plan_with_llm",
]
_ = ColumnPlan  # re-exported type for callers building plans by hand


def to_recipe(override: ColumnOverride, column: Column) -> ColumnRecipe:
    """Public entry for other modules (feedback edits): override → typed recipe; ValueError when invalid."""
    return _to_recipe(override, column)
