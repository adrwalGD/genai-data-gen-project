"""Restricted expression language for derived columns and consistency checks (F2.7).

Grammar (a subset of Python expressions, evaluated by walking the AST — never `eval`):
  columns          quantity, return_date            values of the same row (may be None)
  parent lookups   parent(menu_id).price            column of the parent row referenced by a FK column
  constants        42, 1.5, 'Returned', None, True, TODAY (the generation anchor date)
  arithmetic       + - * / // %  (None operand → NullResult, i.e. the derived value is NULL)
  comparisons      == != < <= > >=  (None operand → None), `x is None`, `x is not None`
  logic            and, or, not, `a if cond else b`
  functions        min, max, abs, round, randint(a, b), random(), coalesce(a, b, ...), days_between(a, b)
"""

from __future__ import annotations

import ast
import operator
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

ANCHOR_DATE = date(2026, 8, 25)
ParentLookup = Callable[[str, str], Any]  # (fk_column, parent_column) -> value for the current row

_BINOPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_COMPARE: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
FUNCTIONS = {"min", "max", "abs", "round", "randint", "random", "coalesce", "days_between"}
CONSTANTS: dict[str, Any] = {"TODAY": ANCHOR_DATE}
RANDOM_FUNCTIONS = {"randint", "random"}


class ExpressionError(ValueError):
    """Syntax or vocabulary error in a derived expression (reported at plan time)."""


class NullResult(Exception):
    """Arithmetic touched a NULL — the derived value is NULL."""


@dataclass
class Compiled:
    source: str
    tree: ast.Expression
    column_refs: list[str] = field(default_factory=list)
    parent_refs: list[tuple[str, str]] = field(default_factory=list)  # (fk_column, parent_column)
    uses_random: bool = False

    @property
    def dependencies(self) -> list[str]:
        """Same-table columns this expression needs generated first (incl. FK columns of parent lookups)."""
        deps = list(self.column_refs)
        for fk_col, _ in self.parent_refs:
            if fk_col not in deps:
                deps.append(fk_col)
        return deps

    def evaluate(self, row: Mapping[str, Any], parent: ParentLookup, rng: random.Random) -> Any:
        """Value for one row; raises NullResult when arithmetic meets NULL."""
        return _eval(self.tree.body, row, parent, rng)


def compile_expression(source: str) -> Compiled:
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"invalid expression {source!r}: {e.msg}") from e
    compiled = Compiled(source=source, tree=tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExpressionError(f"{source!r}: only plain function calls are allowed")
            if node.func.id == "parent":
                if len(node.args) != 1 or not isinstance(node.args[0], ast.Name):
                    raise ExpressionError(f"{source!r}: parent(...) takes exactly one column name")
            elif node.func.id not in FUNCTIONS:
                raise ExpressionError(
                    f"{source!r}: unknown function {node.func.id!r} (allowed: {sorted(FUNCTIONS)})"
                )
            elif node.func.id in RANDOM_FUNCTIONS:
                compiled.uses_random = True
        elif isinstance(node, ast.Attribute):
            if not (
                isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "parent"
            ):
                raise ExpressionError(f"{source!r}: attributes are only allowed on parent(fk_column)")
            fk_col = node.value.args[0].id  # type: ignore[attr-defined]
            ref = (fk_col, node.attr)
            if ref not in compiled.parent_refs:
                compiled.parent_refs.append(ref)
        elif (
            isinstance(node, ast.Name)
            and node.id not in FUNCTIONS
            and node.id not in CONSTANTS
            and node.id != "parent"
        ):
            if node.id not in compiled.column_refs and not _is_parent_arg(tree, node):
                compiled.column_refs.append(node.id)
        elif isinstance(node, ast.Constant) and not isinstance(
            node.value, int | float | str | bool | type(None)
        ):
            raise ExpressionError(f"{source!r}: unsupported constant {node.value!r}")
        elif isinstance(
            node, ast.Subscript | ast.Lambda | ast.ListComp | ast.Dict | ast.List | ast.Tuple | ast.Set
        ):
            raise ExpressionError(f"{source!r}: unsupported construct {type(node).__name__}")
    return compiled


def _is_parent_arg(tree: ast.AST, name: ast.Name) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "parent"
            and node.args
            and node.args[0] is name
        ):
            return True
    return False


def _num(value: Any) -> Any:
    return float(value) if isinstance(value, Decimal) else value


def _eval(node: ast.AST, row: Mapping[str, Any], parent: ParentLookup, rng: random.Random) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in CONSTANTS:
            return CONSTANTS[node.id]
        if node.id not in row:
            raise ExpressionError(f"unknown column {node.id!r}")
        return row[node.id]
    if isinstance(node, ast.Attribute):
        fk_col = node.value.args[0].id  # type: ignore[attr-defined]
        return parent(fk_col, node.attr)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval(node.left, row, parent, rng), _eval(node.right, row, parent, rng)
        if left is None or right is None:
            raise NullResult
        try:
            return _BINOPS[type(node.op)](_num(left), _num(right))
        except ZeroDivisionError as e:
            raise NullResult from e
    if isinstance(node, ast.UnaryOp):
        value = _eval(node.operand, row, parent, rng)
        if isinstance(node.op, ast.Not):
            return not value
        if isinstance(node.op, ast.USub):
            if value is None:
                raise NullResult
            return -_num(value)
    if isinstance(node, ast.BoolOp):
        values = (_eval(v, row, parent, rng) for v in node.values)
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in values:
                result = v
                if not v:
                    return v
            return result
        for v in values:
            if v:
                return v
        return False
    if isinstance(node, ast.IfExp):
        return _eval(node.body if _eval(node.test, row, parent, rng) else node.orelse, row, parent, rng)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, row, parent, rng)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval(comparator, row, parent, rng)
            if isinstance(op, ast.Is):
                ok = left is right
            elif isinstance(op, ast.IsNot):
                ok = left is not right
            elif type(op) in _COMPARE:
                if left is None or right is None:
                    return None
                try:
                    ok = _COMPARE[type(op)](_comparable(left), _comparable(right))
                except TypeError:
                    return None  # e.g. number vs string literal from a sloppy filter: unknown, not a crash
            else:
                raise ExpressionError(f"unsupported comparison {type(op).__name__}")
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        args = [_eval(a, row, parent, rng) for a in node.args]
        return _call(node.func.id, args, rng)
    raise ExpressionError(f"unsupported construct {type(node).__name__}")


def _comparable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.date()
    return value


def _call(name: str, args: list[Any], rng: random.Random) -> Any:
    if name == "coalesce":
        return next((a for a in args if a is not None), None)
    if name == "random":
        return rng.random()
    if name == "days_between":
        a, b = (_comparable(x) for x in args[:2])
        if a is None or b is None:
            raise NullResult
        return (b - a).days
    if any(a is None for a in args):
        raise NullResult
    nums = [_num(a) for a in args]
    if name == "randint":
        lo, hi = int(nums[0]), int(nums[1])
        return rng.randint(lo, hi) if hi >= lo else lo
    if name == "round":
        return round(nums[0], int(nums[1])) if len(nums) > 1 else round(nums[0])
    if name == "min":
        return min(nums)
    if name == "max":
        return max(nums)
    return abs(nums[0])


def parse_parent_ref(ref: str) -> tuple[str, str] | None:
    """`parent(fk_col).col` → (fk_col, col); plain column names → None."""
    text = ref.strip()
    if text.startswith("parent(") and ")." in text:
        fk_col, _, col = text[len("parent(") :].partition(").")
        if fk_col and col:
            return fk_col.strip(), col.strip()
    return None
