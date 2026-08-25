"""Validator (F2.3): prove generated tables satisfy the schema; report every violation class with examples.

The expander is *supposed* to produce clean data — the validator is the independent check (lecture 9: never
let the generator grade itself). It is also run after feedback edits and before loading into Postgres.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import pandas as pd
import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp
from sqlglot.errors import ParseError

from ..schema.models import CheckConstraint, Column, ColumnType, Schema, Table

Row = dict[str, Any]


class Issue(BaseModel):
    table: str
    column: str | None = None
    rule: str = Field(
        description="pk_duplicate | unique | fk_dangling | not_null | enum | check | varchar_length | "
        "decimal_scale | decimal_precision | type | date_invalid | columns_mismatch | missing_table | "
        "row_count"
    )
    count: int
    examples: list[str] = Field(default_factory=list)
    message: str

    def __str__(self) -> str:
        where = f"{self.table}.{self.column}" if self.column else self.table
        ex = f" e.g. {', '.join(self.examples)}" if self.examples else ""
        return f"[{self.rule}] {where}: {self.message} ({self.count}){ex}"


class ValidationReport(BaseModel):
    ok: bool
    issues: list[Issue] = Field(default_factory=list)
    row_counts: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list, description="Non-failing remarks, e.g. unsupported CHECKs")

    def by_table(self, table: str) -> list[Issue]:
        return [i for i in self.issues if i.table.lower() == table.lower()]

    def summary(self) -> str:
        total = sum(self.row_counts.values())
        if self.ok:
            return f"OK — {len(self.row_counts)} tables, {total} rows, no constraint violations"
        return (
            f"{len(self.issues)} issue(s) across {len({i.table for i in self.issues})} table(s): "
            + "; ".join(str(i) for i in self.issues[:8])
        )


def is_null(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    return isinstance(value, float) and math.isnan(value)


def validate(
    schema: Schema,
    tables: dict[str, pd.DataFrame],
    *,
    expected_rows: dict[str, int] | None = None,
    max_examples: int = 5,
) -> ValidationReport:
    issues: list[Issue] = []
    notes: list[str] = []
    counts: dict[str, int] = {}
    frames = {name.lower(): frame for name, frame in tables.items()}
    for table in schema.tables:
        frame = frames.get(table.name.lower())
        if frame is None:
            issues.append(
                Issue(table=table.name, rule="missing_table", count=1, message="table not generated")
            )
            continue
        counts[table.name] = len(frame)
        if expected_rows is not None:
            want = next((n for t, n in expected_rows.items() if t.lower() == table.name.lower()), None)
            if want is not None and want != len(frame):
                issues.append(
                    Issue(
                        table=table.name,
                        rule="row_count",
                        count=abs(want - len(frame)),
                        message=f"expected {want} rows, found {len(frame)}",
                    )
                )
        actual = [c.lower() for c in frame.columns]
        wanted = [c.lower() for c in table.column_names]
        if sorted(actual) != sorted(wanted):
            missing = sorted(set(wanted) - set(actual))
            extra = sorted(set(actual) - set(wanted))
            issues.append(
                Issue(
                    table=table.name,
                    rule="columns_mismatch",
                    count=len(missing) + len(extra),
                    message=f"missing columns {missing}, unexpected columns {extra}",
                )
            )
            continue
        colmap = {c.lower(): c for c in frame.columns}
        rows = [
            dict(zip(table.column_names, vals, strict=True))
            for vals in zip(*(frame[colmap[c.name.lower()]].tolist() for c in table.columns), strict=True)
        ]
        for col in table.columns:
            issues.extend(_column_issues(table, col, [r[col.name] for r in rows], max_examples))
        issues.extend(_unique_issues(table, rows, max_examples))
        issues.extend(_fk_issues(schema, table, rows, frames, max_examples))
        check_issues, check_notes = _check_issues(table, rows, max_examples)
        issues.extend(check_issues)
        notes.extend(check_notes)
    return ValidationReport(ok=not issues, issues=issues, row_counts=counts, notes=notes)


# --- per-column -----------------------------------------------------------------------------------
def _column_issues(table: Table, col: Column, values: list[Any], max_examples: int) -> list[Issue]:
    out: list[Issue] = []
    present = [v for v in values if not is_null(v)]
    nulls = len(values) - len(present)
    if nulls and not col.nullable:
        out.append(
            Issue(
                table=table.name,
                column=col.name,
                rule="not_null",
                count=nulls,
                message="NULL in NOT NULL column",
            )
        )
    bad_type = [v for v in present if not _type_ok(col, v)]
    if bad_type:
        rule = "date_invalid" if col.type.is_temporal else "type"
        out.append(
            Issue(
                table=table.name,
                column=col.name,
                rule=rule,
                count=len(bad_type),
                examples=_examples(bad_type, max_examples),
                message=f"value does not fit {col.raw_type}",
            )
        )
        present = [v for v in present if _type_ok(col, v)]
    if col.type is ColumnType.ENUM and col.enum_values:
        bad = [v for v in present if str(v) not in col.enum_values]
        if bad:
            out.append(
                Issue(
                    table=table.name,
                    column=col.name,
                    rule="enum",
                    count=len(bad),
                    examples=_examples(bad, max_examples),
                    message=f"not one of {col.enum_values}",
                )
            )
    elif col.type.is_textual and col.max_length is not None:
        bad = [v for v in present if len(str(v)) > col.max_length]
        if bad:
            out.append(
                Issue(
                    table=table.name,
                    column=col.name,
                    rule="varchar_length",
                    count=len(bad),
                    examples=_examples(bad, max_examples),
                    message=f"longer than {col.max_length} characters",
                )
            )
    if col.type is ColumnType.DECIMAL:
        scale, precision = col.scale or 0, col.precision or 10
        decimals = [_to_decimal(v) for v in present]
        bad_scale = [d for d in decimals if d is not None and -d.as_tuple().exponent > scale]  # type: ignore[operator]
        if bad_scale:
            out.append(
                Issue(
                    table=table.name,
                    column=col.name,
                    rule="decimal_scale",
                    count=len(bad_scale),
                    examples=_examples(bad_scale, max_examples),
                    message=f"more than {scale} fraction digits",
                )
            )
        cap = Decimal(10) ** (precision - scale)
        bad_prec = [d for d in decimals if d is not None and abs(d) >= cap]
        if bad_prec:
            out.append(
                Issue(
                    table=table.name,
                    column=col.name,
                    rule="decimal_precision",
                    count=len(bad_prec),
                    examples=_examples(bad_prec, max_examples),
                    message=f"exceeds NUMERIC({precision},{scale})",
                )
            )
    return out


def _type_ok(col: Column, v: Any) -> bool:
    t = col.type
    if t.is_integer:
        return isinstance(v, int) and not isinstance(v, bool)
    if t is ColumnType.DECIMAL or t is ColumnType.FLOAT:
        return _to_decimal(v) is not None
    if t is ColumnType.BOOLEAN:
        return isinstance(v, bool)
    if t is ColumnType.DATE:
        return type(v) is date or (isinstance(v, str) and _parses(date.fromisoformat, v))
    if t is ColumnType.DATETIME:
        return isinstance(v, date) or (isinstance(v, str) and _parses(datetime.fromisoformat, v))
    return True


def _parses(fn: Any, text: str) -> bool:
    try:
        fn(text)
        return True
    except ValueError:
        return False


def _to_decimal(v: Any) -> Decimal | None:
    if isinstance(v, bool):
        return None
    try:
        return v if isinstance(v, Decimal) else Decimal(str(v))
    except InvalidOperation, ValueError:
        return None


def _examples(values: list[Any], n: int) -> list[str]:
    return [repr(v)[:60] for v in values[:n]]


# --- uniqueness and FKs ---------------------------------------------------------------------------
def _unique_issues(table: Table, rows: list[Row], max_examples: int) -> list[Issue]:
    out: list[Issue] = []
    for cols in table.unique_column_sets:
        seen: set[tuple[Any, ...]] = set()
        dupes: list[tuple[Any, ...]] = []
        for r in rows:
            key = tuple(r[c] for c in cols)
            if any(is_null(k) for k in key):
                continue
            if key in seen:
                dupes.append(key)
            seen.add(key)
        if dupes:
            is_pk = cols == list(table.primary_key)
            out.append(
                Issue(
                    table=table.name,
                    column=cols[0] if len(cols) == 1 else None,
                    rule="pk_duplicate" if is_pk else "unique",
                    count=len(dupes),
                    examples=_examples([k[0] if len(k) == 1 else k for k in dupes], max_examples),
                    message=f"duplicate values in {'PRIMARY KEY' if is_pk else 'UNIQUE'} ({', '.join(cols)})",
                )
            )
    return out


def _fk_issues(
    schema: Schema, table: Table, rows: list[Row], frames: dict[str, pd.DataFrame], max_examples: int
) -> list[Issue]:
    out: list[Issue] = []
    for fk in table.foreign_keys:
        parent_frame = frames.get(fk.ref_table.lower())
        if parent_frame is None:
            continue  # reported as missing_table for the parent
        parent = schema.table(fk.ref_table)
        pcols = [next(c for c in parent_frame.columns if c.lower() == rc.lower()) for rc in fk.ref_columns]
        parent_keys = {
            tuple(vals)
            for vals in zip(*(parent_frame[c].tolist() for c in pcols), strict=True)
            if not any(is_null(v) for v in vals)
        }
        dangling = []
        for r in rows:
            key = tuple(r[c] for c in fk.columns)
            if any(is_null(k) for k in key):
                continue
            if key not in parent_keys:
                dangling.append(key[0] if len(key) == 1 else key)
        if dangling:
            out.append(
                Issue(
                    table=table.name,
                    column=fk.columns[0] if len(fk.columns) == 1 else None,
                    rule="fk_dangling",
                    count=len(dangling),
                    examples=_examples(dangling, max_examples),
                    message=f"no matching row in {parent.name}({', '.join(fk.ref_columns)})",
                )
            )
    return out


# --- CHECK constraints ----------------------------------------------------------------------------
def _check_issues(table: Table, rows: list[Row], max_examples: int) -> tuple[list[Issue], list[str]]:
    out: list[Issue] = []
    notes: list[str] = []
    for chk in table.checks:
        try:
            parsed = sqlglot.parse_one(chk.expression, read="postgres")
        except ParseError:
            notes.append(f"{table.name}: CHECK ({chk.expression}) could not be parsed — not validated")
            continue
        if not isinstance(parsed, exp.Expression):
            notes.append(f"{table.name}: CHECK ({chk.expression}) could not be parsed — not validated")
            continue
        tree: exp.Expression = parsed
        failures: list[Any] = []
        unsupported = False
        for r in rows:
            result = _eval(tree, r, table)
            if result is _UNSUPPORTED:
                unsupported = True
                break
            if result is False:
                failures.append(tuple(r[c] for c in chk.columns) if chk.columns else "row")
        if unsupported:
            notes.append(
                f"{table.name}: CHECK ({chk.expression}) uses unsupported constructs — not validated"
            )
            continue
        if failures:
            out.append(
                Issue(
                    table=table.name,
                    column=chk.columns[0] if len(chk.columns) == 1 else None,
                    rule="check",
                    count=len(failures),
                    examples=_examples(
                        [f[0] if isinstance(f, tuple) and len(f) == 1 else f for f in failures], max_examples
                    ),
                    message=f"violates CHECK ({chk.expression})",
                )
            )
    return out, notes


_UNSUPPORTED = object()
_COMPARE = {
    exp.EQ: lambda a, b: a == b,
    exp.NEQ: lambda a, b: a != b,
    exp.GT: lambda a, b: a > b,
    exp.GTE: lambda a, b: a >= b,
    exp.LT: lambda a, b: a < b,
    exp.LTE: lambda a, b: a <= b,
}


def _eval(node: exp.Expression, row: Row, table: Table) -> Any:
    """Three-valued SQL evaluation of a CHECK expression; returns True/False/None or _UNSUPPORTED."""
    if isinstance(node, exp.Paren):
        return _eval(cast(exp.Expression, node.this), row, table)
    if isinstance(node, exp.And | exp.Or):
        left, right = (
            _eval(cast(exp.Expression, node.left), row, table),
            _eval(cast(exp.Expression, node.right), row, table),
        )
        if left is _UNSUPPORTED or right is _UNSUPPORTED:
            return _UNSUPPORTED
        if isinstance(node, exp.And):
            return False if False in (left, right) else (None if None in (left, right) else True)
        return True if True in (left, right) else (None if None in (left, right) else False)
    if isinstance(node, exp.Not):
        if not isinstance(node.this, exp.Expression):
            return _UNSUPPORTED
        inner = _eval(node.this, row, table)
        return inner if inner in (None, _UNSUPPORTED) else not inner
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
        return is_null(_value(node.this, row, table))
    if isinstance(node, exp.Between):
        v, lo, hi = (_value(x, row, table) for x in (node.this, node.args["low"], node.args["high"]))
        if _UNSUPPORTED in (v, lo, hi):
            return _UNSUPPORTED
        return None if any(is_null(x) for x in (v, lo, hi)) else lo <= v <= hi
    if isinstance(node, exp.In):
        v = _value(node.this, row, table)
        options = [_value(e, row, table) for e in node.expressions]
        if v is _UNSUPPORTED or _UNSUPPORTED in options:
            return _UNSUPPORTED
        return None if is_null(v) else any(_coerce_pair(v, o)[0] == _coerce_pair(v, o)[1] for o in options)
    for cls, fn in _COMPARE.items():
        if isinstance(node, cls):
            a, b = _value(node.this, row, table), _value(node.expression, row, table)
            if a is _UNSUPPORTED or b is _UNSUPPORTED:
                return _UNSUPPORTED
            if is_null(a) or is_null(b):
                return None
            try:
                return fn(*_coerce_pair(a, b))
            except TypeError:
                return _UNSUPPORTED
    return _UNSUPPORTED


def _value(node: exp.Expression, row: Row, table: Table) -> Any:
    if isinstance(node, exp.Column):
        name = node.name
        if not table.has_column(name):
            return _UNSUPPORTED
        return row[table.column(name).name]
    if isinstance(node, exp.Literal):
        return node.this if node.is_string else Decimal(node.this)
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return -Decimal(node.this.this)
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Paren):
        return _value(node.this, row, table)
    return _UNSUPPORTED


def _coerce_pair(a: Any, b: Any) -> tuple[Any, Any]:
    if (
        isinstance(a, int | float | Decimal)
        and isinstance(b, int | float | Decimal)
        and not isinstance(a, bool)
    ):
        return Decimal(str(a)), Decimal(str(b))
    if isinstance(a, datetime) and type(b) is date:
        return a.date(), b
    if type(a) is date and isinstance(b, datetime):
        return a, b.date()
    if isinstance(a, date) and isinstance(b, str):
        return str(a), b
    if isinstance(a, str) and isinstance(b, date):
        return a, str(b)
    return a, b


__all__ = ["CheckConstraint", "Issue", "ValidationReport", "is_null", "validate"]
