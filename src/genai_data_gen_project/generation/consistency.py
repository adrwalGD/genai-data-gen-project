"""Consistency checks (F2.7): re-evaluate the relations a plan declares (derived, aggregate, after_column).

The validator proves constraints from the DDL; this module proves the *semantic* relations the plan promised:
subtotal = quantity * price, totals = sum of children, event dates after registration, status <-> dates.
Expressions using randomness are skipped (they cannot be re-derived).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from ..schema.models import Schema, Table
from .expressions import NullResult, compile_expression, parse_parent_ref
from .recipes import AggregateRecipe, DateTimeWindowRecipe, DateWindowRecipe, DerivedRecipe, GenerationPlan
from .validator import Issue, is_null

TOLERANCE = Decimal("0.011")
Frames = dict[str, pd.DataFrame]
Row = dict[str, Any]


def check_consistency(
    schema: Schema, plan: GenerationPlan, tables: Frames, *, max_examples: int = 5
) -> list[Issue]:
    issues: list[Issue] = []
    frames = {name.lower(): frame for name, frame in tables.items()}
    for tplan in plan.tables:
        if not schema.has_table(tplan.table) or tplan.table.lower() not in frames:
            continue
        table = schema.table(tplan.table)
        frame = frames[tplan.table.lower()]
        for cplan in tplan.columns:
            if not table.has_column(cplan.column):
                continue
            column = table.column(cplan.column).name
            r = cplan.recipe
            if isinstance(r, DerivedRecipe):
                issues.extend(
                    _derived_issues(schema, table, column, r.expression, frame, frames, max_examples)
                )
            elif isinstance(r, AggregateRecipe):
                issues.extend(_aggregate_issues(schema, table, column, r, frame, frames, max_examples))
            elif isinstance(r, DateWindowRecipe | DateTimeWindowRecipe) and r.after_column:
                issues.extend(_after_issues(schema, table, column, r, frame, frames, max_examples))
    return issues


def _parent_lookup(schema: Schema, table: Table, frames: Frames, row: Row) -> Callable[[str, str], Any]:
    def lookup(fk_col: str, pcol: str) -> Any:
        fk = table.fk_for_column(fk_col)
        if fk is None:
            return None
        value = row.get(table.column(fk_col).name)
        if is_null(value):
            return None
        parent = schema.table(fk.ref_table)
        pframe = frames.get(parent.name.lower())
        if pframe is None:
            return None
        ref_col = parent.column(fk.ref_columns[fk.columns.index(table.column(fk_col).name)]).name
        matches = pframe.index[pframe[ref_col] == value].tolist()
        return None if not matches else pframe.at[matches[0], parent.column(pcol).name]

    return lookup


def _rows(frame: pd.DataFrame) -> list[Row]:
    return [dict(zip(frame.columns, vals, strict=True)) for vals in frame.itertuples(index=False, name=None)]


def _equal(actual: Any, expected: Any) -> bool:
    if is_null(actual) and is_null(expected):
        return True
    if is_null(actual) or is_null(expected):
        return False
    numeric = (int, float, Decimal)
    if isinstance(actual, numeric) and isinstance(expected, numeric) and not isinstance(actual, bool):
        return abs(Decimal(str(actual)) - Decimal(str(expected))) <= TOLERANCE
    if isinstance(actual, datetime) and isinstance(expected, date):
        return actual.date() == (expected.date() if isinstance(expected, datetime) else expected)
    return bool(actual == expected)


def _issue(
    table: Table, column: str, bad: list[tuple[Any, Any]], message: str, sep: str, n: int
) -> list[Issue]:
    if not bad:
        return []
    examples = [f"{a!r} {sep} {e!r}" for a, e in bad[:n]]
    return [
        Issue(
            table=table.name,
            column=column,
            rule="consistency",
            count=len(bad),
            examples=examples,
            message=message,
        )
    ]


def _derived_issues(
    schema: Schema, table: Table, column: str, expression: str, frame: pd.DataFrame, frames: Frames, n: int
) -> list[Issue]:
    compiled = compile_expression(expression)
    if compiled.uses_random:
        return []
    rng = random.Random(0)
    bad: list[tuple[Any, Any]] = []
    for row in _rows(frame):
        try:
            expected = compiled.evaluate(row, _parent_lookup(schema, table, frames, row), rng)
        except NullResult:
            expected = None
        if not _equal(row[column], expected):
            bad.append((row[column], expected))
    return _issue(table, column, bad, f"does not equal {expression}", "!=", n)


def _aggregate_issues(
    schema: Schema, table: Table, column: str, r: AggregateRecipe, frame: pd.DataFrame, frames: Frames, n: int
) -> list[Issue]:
    child = schema.table(r.child_table)
    cframe = frames.get(child.name.lower())
    fk = child.fk_for_column(r.child_fk_column)
    if cframe is None or fk is None:
        return []
    ref_col = table.column(fk.ref_columns[fk.columns.index(child.column(r.child_fk_column).name)]).name
    groups: dict[Any, list[float]] = {}
    keys = cframe[child.column(r.child_fk_column).name].tolist()
    values = cframe[child.column(r.child_column).name].tolist() if r.child_column else [1] * len(keys)
    for key, value in zip(keys, values, strict=True):
        if is_null(key) or (is_null(value) and r.agg != "count"):
            continue
        groups.setdefault(key, []).append(1.0 if r.agg == "count" else float(value))
    bad: list[tuple[Any, Any]] = []
    for key, actual in zip(frame[ref_col].tolist(), frame[column].tolist(), strict=True):
        items = groups.get(key)
        if not items:
            expected: float = float(r.default)
        else:
            aggregates = {
                "sum": sum(items),
                "count": float(len(items)),
                "min": min(items),
                "max": max(items),
                "avg": sum(items) / len(items),
            }
            expected = aggregates[r.agg]
        if not _equal(actual, expected):
            bad.append((actual, round(expected, 2)))
    return _issue(
        table, column, bad, f"does not equal {r.agg}({child.name}.{r.child_column or '*'})", "!=", n
    )


def _after_issues(
    schema: Schema,
    table: Table,
    column: str,
    r: DateWindowRecipe | DateTimeWindowRecipe,
    frame: pd.DataFrame,
    frames: Frames,
    n: int,
) -> list[Issue]:
    ref = r.after_column or ""
    parent_ref = parse_parent_ref(ref)
    if isinstance(r, DateWindowRecipe):
        min_gap = timedelta(days=r.min_days_after)
    else:
        min_gap = timedelta(hours=r.min_hours_after)
    bad: list[tuple[Any, Any]] = []
    for row in _rows(frame):
        actual = row[column]
        if parent_ref:
            base = _parent_lookup(schema, table, frames, row)(*parent_ref)
        else:
            base = row.get(table.column(ref).name)
        if is_null(actual) or is_null(base):
            continue
        a = actual if isinstance(actual, datetime) else datetime.combine(actual, datetime.min.time())
        b = base if isinstance(base, datetime) else datetime.combine(base, datetime.min.time())
        if isinstance(r, DateWindowRecipe):
            a, b = a.replace(hour=0, minute=0, second=0), b.replace(hour=0, minute=0, second=0)
        window_end = r.end if isinstance(r.end, datetime) else datetime.combine(r.end, datetime.min.time())
        if a < b + min_gap and a < window_end:  # NOT NULL values clamped to the window end are accepted
            bad.append((actual, base))
    return _issue(table, column, bad, f"must follow {ref}", "before", n)
