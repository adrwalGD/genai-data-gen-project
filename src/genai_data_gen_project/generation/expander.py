"""Deterministic row expander (F2.2): Schema + GenerationPlan + seed → {table: DataFrame}.

Integrity is enforced here, in code (CLAUDE.md rule 10): sequential PKs, FK values sampled from generated
parents (deferred/cyclic FKs filled in a second pass), NOT NULL, null ratios, ENUM membership, CHECK bounds,
VARCHAR length, NUMERIC scale/precision, uniqueness (retry then suffix), temporal order via `after_column`.
Same schema + plan + seed + pools → identical output.
"""

from __future__ import annotations

import random
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

import pandas as pd
from faker import Faker

from ..schema.models import Column, ColumnType, Schema, Table
from ..schema.order import GenerationOrder, generation_order
from .expressions import Compiled, NullResult, compile_expression, parse_parent_ref
from .heuristics import check_bounds
from .recipes import (
    AggregateRecipe,
    BooleanRecipe,
    ColumnPlan,
    ConstantRecipe,
    DateTimeWindowRecipe,
    DateWindowRecipe,
    DecimalRangeRecipe,
    DerivedRecipe,
    Distribution,
    EnumWeightedRecipe,
    FakerRecipe,
    ForeignKeyRecipe,
    GenerationPlan,
    IntRangeRecipe,
    PatternRecipe,
    SequenceRecipe,
    TablePlan,
    TextPoolRecipe,
)

Value = int | float | Decimal | bool | str | date | datetime | None
PoolKey = tuple[str, str]  # (table, column), case-insensitive via _key()
UNIQUE_ATTEMPTS = 40


class ExpansionError(ValueError):
    """The plan cannot be realised (missing plan, empty NOT NULL parent, unsatisfiable uniqueness, ...)."""


def _key(table: str, column: str) -> PoolKey:
    return table.lower(), column.lower()


@dataclass
class _Context:
    rng: random.Random
    faker: Faker
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    pools: dict[PoolKey, list[str]] = field(default_factory=dict)
    indexes: dict[tuple[str, str], dict[Value, int]] = field(default_factory=dict)
    fakers: dict[str, Faker] = field(default_factory=dict)
    seed: int = 0

    def faker_for(self, locale: str | None) -> Faker:
        """Seeded Faker for a locale (pl_PL, de_DE, ...); the default instance for None."""
        if not locale:
            return self.faker
        if locale not in self.fakers:
            try:
                instance = Faker(locale)
            except (AttributeError, ValueError) as e:
                raise ExpansionError(f"unknown Faker locale {locale!r}") from e
            instance.seed_instance(self.seed)
            self.fakers[locale] = instance
        return self.fakers[locale]

    def parent_index(self, table: str, column: str) -> dict[Value, int]:
        key = (table.lower(), column.lower())
        if key not in self.indexes:
            frame = self.tables[table]
            col = next(c for c in frame.columns if c.lower() == column.lower())
            self.indexes[key] = {v: i for i, v in enumerate(frame[col].tolist()) if v is not None}
        return self.indexes[key]


def expand(
    schema: Schema,
    plan: GenerationPlan,
    *,
    seed: int = 0,
    pools: dict[PoolKey, list[str]] | None = None,
    order: GenerationOrder | None = None,
) -> dict[str, pd.DataFrame]:
    """Generate every table in dependency order; return DataFrames (object dtype, None for NULL)."""
    order = order or generation_order(schema)
    ctx = _Context(rng=random.Random(seed), faker=Faker("en_US"), seed=seed)
    ctx.faker.seed_instance(seed)
    ctx.pools = {_key(t, c): list(v) for (t, c), v in (pools or {}).items()}
    deferred_cols: list[tuple[Table, str]] = []
    for name in order.tables:
        table = schema.table(name)
        tplan = plan.table(name)
        if tplan is None:
            raise ExpansionError(f"plan has no entry for table {name!r}")
        deferred = {c.lower() for fk in order.deferred_for(name) for c in fk.columns}
        ctx.tables[table.name] = _expand_table(ctx, schema, table, tplan, deferred)
        deferred_cols.extend((table, c) for c in table.column_names if c.lower() in deferred)
    for table, column in deferred_cols:
        _fill_deferred(ctx, schema, table, column, plan)
    _fill_aggregates(ctx, schema, plan)
    return {t.name: ctx.tables[t.name] for t in schema.tables}


# --- one table ------------------------------------------------------------------------------------
def _expand_table(
    ctx: _Context, schema: Schema, table: Table, tplan: TablePlan, deferred: set[str]
) -> pd.DataFrame:
    n = tplan.rows
    plans = {c.column.lower(): c for c in tplan.columns}
    missing = [c.name for c in table.columns if c.name.lower() not in plans]
    if missing:
        raise ExpansionError(f"{table.name}: plan has no recipe for column(s) {', '.join(missing)}")
    columns: dict[str, list[Value]] = {}
    for col in _column_order(table, plans):
        cplan = plans[col.name.lower()]
        if col.name.lower() in deferred:
            columns[col.name] = [None] * n
            continue
        values = _generate_column(ctx, schema, table, col, cplan, n, columns)
        values = _apply_nulls(ctx, col, cplan, values)
        values = [_conform(col, v) for v in values]
        columns[col.name] = values
    _enforce_unique_sets(ctx, table, columns, n, plans)
    return pd.DataFrame({c.name: pd.Series(columns[c.name], dtype=object) for c in table.columns})


def _column_order(table: Table, plans: dict[str, ColumnPlan]) -> list[Column]:
    """Declaration order, except that columns referenced by pattern/derived/after_column come first."""
    deps: dict[str, list[str]] = {}
    for col in table.columns:
        r = plans[col.name.lower()].recipe
        refs: list[str] = []
        if isinstance(r, PatternRecipe):
            refs = re.findall(r"\{col:([A-Za-z_][A-Za-z0-9_]*)", r.template)
        elif isinstance(r, DerivedRecipe):
            try:
                refs = compile_expression(r.expression).dependencies
            except ValueError as e:
                raise ExpansionError(f"{table.name}.{col.name}: {e}") from e
        elif isinstance(r, DateWindowRecipe | DateTimeWindowRecipe) and r.after_column:
            parent_ref = parse_parent_ref(r.after_column)
            refs = [parent_ref[0]] if parent_ref else [r.after_column]
        for ref in refs:
            if not table.has_column(ref):
                raise ExpansionError(f"{table.name}.{col.name}: recipe references unknown column {ref!r}")
        deps[col.name.lower()] = [table.column(ref).name.lower() for ref in refs]
    ordered: list[Column] = []
    done: set[str] = set()

    def visit(col: Column, stack: tuple[str, ...]) -> None:
        key = col.name.lower()
        if key in done:
            return
        if key in stack:
            raise ExpansionError(f"{table.name}: circular column dependency {' -> '.join((*stack, key))}")
        for dep in deps[key]:
            visit(table.column(dep), (*stack, key))
        done.add(key)
        ordered.append(col)

    for col in table.columns:
        visit(col, ())
    return ordered


# --- value generation -----------------------------------------------------------------------------
def _generate_column(
    ctx: _Context,
    schema: Schema,
    table: Table,
    col: Column,
    cplan: ColumnPlan,
    n: int,
    generated: dict[str, list[Value]],
) -> list[Value]:
    r = cplan.recipe
    rng = ctx.rng
    if isinstance(r, SequenceRecipe):
        return [r.start + i * r.step for i in range(n)]
    if isinstance(r, ForeignKeyRecipe):
        return _fk_values(ctx, schema, table, col, r, n)
    if isinstance(r, IntRangeRecipe):
        return [_int_in(rng, r.min, r.max, r.distribution) for _ in range(n)]
    if isinstance(r, DecimalRangeRecipe):
        scale = r.scale if r.scale is not None else (col.scale if col.scale is not None else 2)
        return [_decimal_in(rng, r.min, r.max, scale, r.distribution) for _ in range(n)]
    if isinstance(r, EnumWeightedRecipe):
        return list(rng.choices(r.values, weights=r.weights, k=n)) if n else []
    if isinstance(r, BooleanRecipe):
        return [rng.random() < r.true_ratio for _ in range(n)]
    if isinstance(r, ConstantRecipe):
        return [r.value] * n
    if isinstance(r, AggregateRecipe):
        return [r.default] * n  # replaced by _fill_aggregates once the children exist
    if isinstance(r, FakerRecipe):
        provider = _faker_provider(ctx.faker_for(r.locale), r.provider)
        return _unique_or_not(ctx, col, n, lambda _i: provider(**r.kwargs), r.unique)
    if isinstance(r, TextPoolRecipe):
        return _pool_values(ctx, table, col, r, n)
    if isinstance(r, PatternRecipe):
        renderer = _compile_pattern(ctx, table, r.template, generated)
        return _unique_or_not(ctx, col, n, renderer, r.unique)
    if isinstance(r, DateWindowRecipe):
        return [
            _date_value(rng, r, _row_ref(ctx, schema, table, generated, r.after_column, i), col.nullable)
            for i in range(n)
        ]
    if isinstance(r, DateTimeWindowRecipe):
        return [
            _datetime_value(rng, r, _row_ref(ctx, schema, table, generated, r.after_column, i), col.nullable)
            for i in range(n)
        ]
    if isinstance(r, DerivedRecipe):
        return _derived_values(ctx, schema, table, col, r.expression, n, generated)
    raise ExpansionError(f"{table.name}.{col.name}: unsupported recipe {type(r).__name__}")


def _row_ref(
    ctx: _Context, schema: Schema, table: Table, generated: dict[str, list[Value]], ref: str | None, i: int
) -> Value:
    if not ref:
        return None
    parent_ref = parse_parent_ref(ref)
    if parent_ref is None:
        return generated[table.column(ref).name][i]
    fk_col, col = parent_ref
    return _parent_value(ctx, schema, table, generated, fk_col, col, i)


def _parent_value(
    ctx: _Context,
    schema: Schema,
    table: Table,
    generated: dict[str, list[Value]],
    fk_col: str,
    col: str,
    i: int,
) -> Value:
    fk = table.fk_for_column(fk_col)
    if fk is None:
        raise ExpansionError(f"{table.name}: parent({fk_col}) is not a foreign key column")
    value = generated[table.column(fk_col).name][i]
    if value is None:
        return None
    parent = schema.table(fk.ref_table)
    ref_col = parent.column(fk.ref_columns[fk.columns.index(table.column(fk_col).name)]).name
    row = ctx.parent_index(parent.name, ref_col).get(value)
    if row is None:
        return None
    return cast(Value, ctx.tables[parent.name][parent.column(col).name].iat[row])


def _unit(rng: random.Random, dist: Distribution) -> float:
    u = rng.random()
    if dist is Distribution.NORMAL:
        return min(1.0, max(0.0, rng.gauss(0.5, 1 / 6)))
    if dist is Distribution.SKEWED_LOW:
        return u**2.2
    if dist is Distribution.SKEWED_HIGH:
        return 1 - (1 - u) ** 2.2
    return u


def _int_in(rng: random.Random, lo: int, hi: int, dist: Distribution) -> int:
    if dist is Distribution.UNIFORM:
        return rng.randint(lo, hi)
    return min(hi, max(lo, lo + round(_unit(rng, dist) * (hi - lo))))


def _decimal_in(rng: random.Random, lo: float, hi: float, scale: int, dist: Distribution) -> Decimal:
    x = lo + _unit(rng, dist) * (hi - lo)
    q = Decimal(1).scaleb(-scale)
    return min(Decimal(str(hi)), max(Decimal(str(lo)), Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP)))


def _fk_values(
    ctx: _Context, schema: Schema, table: Table, col: Column, r: ForeignKeyRecipe, n: int
) -> list[Value]:
    parent = schema.table(r.ref_table)
    if parent.name not in ctx.tables:
        raise ExpansionError(
            f"{table.name}.{col.name}: parent {parent.name} is not generated yet (order problem)"
        )
    candidates = [
        v for v in ctx.tables[parent.name][parent.column(r.ref_column).name].tolist() if v is not None
    ]
    if not candidates:
        if col.nullable:
            return [None] * n
        raise ExpansionError(
            f"{table.name}.{col.name} is NOT NULL but parent {parent.name} has no rows — "
            f"give {parent.name} rows > 0"
        )
    if r.distribution is Distribution.UNIFORM or len(candidates) == 1:
        return [ctx.rng.choice(candidates) for _ in range(n)]
    ranked = list(candidates)
    ctx.rng.shuffle(ranked)
    weights = [1 / (rank + 1) ** 0.8 for rank in range(len(ranked))]
    if r.distribution is Distribution.SKEWED_HIGH:
        weights.reverse()
    return list(ctx.rng.choices(ranked, weights=weights, k=n)) if n else []


def _faker_provider(faker: Faker, name: str) -> Callable[..., Any]:
    provider = getattr(faker, name, None)
    if not callable(provider):
        raise ExpansionError(f"unknown Faker provider {name!r}")
    return provider


def _unique_or_not(
    ctx: _Context, col: Column, n: int, make: Callable[[int], Any], unique: bool
) -> list[Value]:
    if not unique:
        return [make(i) for i in range(n)]
    seen: set[Value] = set()
    out: list[Value] = []
    for i in range(n):
        value = _conform(col, make(i))
        attempts = 0
        while value in seen and attempts < UNIQUE_ATTEMPTS:
            value = _conform(col, make(i))
            attempts += 1
        if value in seen:
            value = _suffix_unique(col, value, seen)
        seen.add(value)
        out.append(value)
    return out


def _suffix_unique(col: Column, value: Value, seen: set[Value]) -> Value:
    if not isinstance(value, str):
        raise ExpansionError(
            f"{col.name}: cannot make non-text value {value!r} unique after {UNIQUE_ATTEMPTS} tries"
        )
    limit = col.max_length
    local, at, domain = value.partition("@")
    for k in range(2, 100_000):
        suffix = f"-{k}"
        if at and domain and " " not in value:  # keep e-mail-like values valid: local-2@domain
            room = None if limit is None else max(limit - len(suffix) - len(domain) - 1, 1)
            candidate = f"{local if room is None else local[:room]}{suffix}@{domain}"
        else:
            base = value if limit is None else value[: max(limit - len(suffix), 0)]
            candidate = base + suffix
        if limit is not None and len(candidate) > limit:
            break
        if candidate not in seen:
            return candidate
    raise ExpansionError(f"{col.name}: cannot produce enough unique values within {limit} characters")


def _pool_values(ctx: _Context, table: Table, col: Column, r: TextPoolRecipe, n: int) -> list[Value]:
    pool = [v for v in ctx.pools.get(_key(table.name, col.name), []) if isinstance(v, str) and v.strip()]
    pool = [_conform(col, v) for v in pool]  # type: ignore[misc]
    if not r.unique:
        if pool:
            return [ctx.rng.choice(pool) for _ in range(n)]
        provider = _faker_provider(ctx.faker, r.fallback_provider)
        return [provider(**r.fallback_kwargs) for _ in range(n)]
    distinct = list(dict.fromkeys(pool))
    ctx.rng.shuffle(distinct)
    values: list[Value] = list(distinct[:n])
    if len(values) < n:
        provider = _faker_provider(ctx.faker, r.fallback_provider)
        seen = set(values)
        extra = _unique_or_not(ctx, col, n - len(values), lambda _i: provider(**r.fallback_kwargs), True)
        for v in extra:
            if v in seen:
                v = _suffix_unique(col, v, seen)
            seen.add(v)
            values.append(v)
    return values


_MODIFIERS: dict[str, Callable[[str], str]] = {
    "slug": lambda s: re.sub(
        r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    ),
    "lower": str.lower,
    "upper": str.upper,
    "initial": lambda s: s[:1],
}
_TOKEN = re.compile(
    r"\{col:([A-Za-z_][A-Za-z0-9_]*)(?:\|([a-z]+))?\}|\{faker:([A-Za-z_][A-Za-z0-9_]*)\}|\{seq\}|[#?%]"
)


def _compile_pattern(
    ctx: _Context, table: Table, template: str, generated: dict[str, list[Value]]
) -> Callable[[int], str]:
    letters, uppers, digits = "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "0123456789"

    def render(i: int) -> str:
        def sub(m: re.Match[str]) -> str:
            token = m.group(0)
            if m.group(1):
                value = generated[table.column(m.group(1)).name][i]
                text = "" if value is None else str(value)
                mod = m.group(2)
                if mod:
                    if mod not in _MODIFIERS:
                        raise ExpansionError(f"unknown pattern modifier {mod!r} in {template!r}")
                    text = _MODIFIERS[mod](text)
                return text
            if m.group(3):
                return str(_faker_provider(ctx.faker, m.group(3))())
            if token == "{seq}":
                return str(i + 1)
            if token == "#":
                return ctx.rng.choice(digits)
            if token == "?":
                return ctx.rng.choice(letters)
            return ctx.rng.choice(uppers)

        return _TOKEN.sub(sub, template)

    return render


def _date_value(rng: random.Random, r: DateWindowRecipe, base: Value, nullable: bool = False) -> date | None:
    base_date = base.date() if isinstance(base, datetime) else base if isinstance(base, date) else None
    if base_date is not None:
        lo = max(base_date + timedelta(days=r.min_days_after), r.start)
        if lo > r.end:  # the earliest allowed moment lies beyond the window: has not happened yet
            return None if nullable else r.end
        hi = base_date + timedelta(days=r.max_days_after) if r.max_days_after is not None else r.end
        hi = min(hi, r.end) if hi >= lo else r.end  # an explicit window wins over the max-gap preference
        return lo + timedelta(days=rng.randint(0, (hi - lo).days)) if hi > lo else lo
    return r.start + timedelta(days=rng.randint(0, (r.end - r.start).days))


def _datetime_value(
    rng: random.Random, r: DateTimeWindowRecipe, base: Value, nullable: bool = False
) -> datetime | None:
    base_dt = (
        base
        if isinstance(base, datetime)
        else datetime.combine(base, datetime.min.time())
        if isinstance(base, date)
        else None
    )
    if base_dt is not None:
        lo = max(base_dt + timedelta(hours=r.min_hours_after), r.start)
        if lo > r.end:  # the earliest allowed moment lies beyond the window: has not happened yet
            return None if nullable else r.end
        hi = base_dt + timedelta(hours=r.max_hours_after) if r.max_hours_after is not None else r.end
        hi = min(hi, r.end) if hi >= lo else r.end  # an explicit window wins over the max-gap preference
        span = int((hi - lo).total_seconds())
        return lo + timedelta(seconds=rng.randint(0, span)) if span > 0 else lo
    span = int((r.end - r.start).total_seconds())
    return r.start + timedelta(seconds=rng.randint(0, max(span, 0)))


# --- derived expressions (shared restricted language, generation/expressions.py) ------------------
def _derived_values(
    ctx: _Context,
    schema: Schema,
    table: Table,
    col: Column,
    expression: str,
    n: int,
    generated: dict[str, list[Value]],
) -> list[Value]:
    try:
        compiled: Compiled = compile_expression(expression)
    except ValueError as e:
        raise ExpansionError(f"{table.name}.{col.name}: {e}") from e
    names = [c.name for c in table.columns if c.name in generated]
    out: list[Value] = []
    for i in range(n):
        row = {name: generated[name][i] for name in names}

        def parent(fk_col: str, pcol: str, _i: int = i) -> Value:
            return _parent_value(ctx, schema, table, generated, fk_col, pcol, _i)

        try:
            out.append(compiled.evaluate(row, parent, ctx.rng))
        except NullResult:
            out.append(None)
        except ValueError as e:
            raise ExpansionError(f"{table.name}.{col.name}: {e}") from e
    return out


# --- nulls, conformance, uniqueness ---------------------------------------------------------------
def _apply_nulls(ctx: _Context, col: Column, cplan: ColumnPlan, values: list[Value]) -> list[Value]:
    if not col.nullable or col.primary_key or cplan.null_ratio <= 0:
        return values
    if isinstance(cplan.recipe, DerivedRecipe | AggregateRecipe):
        return values  # NULLs of derived/aggregate columns come only from the expression (NullResult)
    return [None if ctx.rng.random() < cplan.null_ratio else v for v in values]


def _conform(col: Column, value: Value) -> Value:
    """Coerce a generated value into the column's type and hard limits (never widen a violation)."""
    if value is None:
        return None
    try:
        return _conform_unchecked(col, value)
    except (ArithmeticError, ValueError, TypeError) as e:  # decimal.InvalidOperation is an ArithmeticError
        raise ExpansionError(f"{col.name}: cannot store {value!r} in a {col.raw_type} column ({e})") from e


def _conform_unchecked(col: Column, value: Value) -> Value:
    t = col.type
    if t.is_integer:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int | float | Decimal):
            return round(float(value))
        return int(str(value).strip())
    if t is ColumnType.DECIMAL or t is ColumnType.FLOAT:
        scale = col.scale if (t is ColumnType.DECIMAL and col.scale is not None) else 2
        d = Decimal(str(value)) if not isinstance(value, Decimal) else value
        d = d.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
        if t is ColumnType.DECIMAL and col.precision:
            cap = Decimal(10) ** (col.precision - scale) - Decimal(1).scaleb(-scale)
            d = max(-cap, min(cap, d))
        lo, hi, _ = check_bounds(col.check, col.name) if col.check else (None, None, None)
        if lo is not None and d < Decimal(str(lo)):
            d = Decimal(str(lo)).quantize(Decimal(1).scaleb(-scale))
        if hi is not None and d > Decimal(str(hi)):
            d = Decimal(str(hi)).quantize(Decimal(1).scaleb(-scale))
        return d if t is ColumnType.DECIMAL else float(d)
    if t is ColumnType.BOOLEAN:
        return (
            bool(value) if not isinstance(value, str) else value.strip().lower() in {"true", "t", "1", "yes"}
        )
    if t is ColumnType.DATE:
        if isinstance(value, pd.Timestamp):
            return value.to_pydatetime().date()
        return (
            value.date()
            if isinstance(value, datetime)
            else value
            if isinstance(value, date)
            else date.fromisoformat(str(value))
        )
    if t is ColumnType.DATETIME:
        if isinstance(value, datetime):
            return value.replace(microsecond=0)
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        return datetime.fromisoformat(str(value))
    if t.is_textual or t in {ColumnType.TIME, ColumnType.JSON, ColumnType.UUID, ColumnType.UNKNOWN}:
        text = str(value)
        if t is ColumnType.ENUM and col.enum_values and text not in col.enum_values:
            return col.enum_values[0]
        if col.max_length is not None and len(text) > col.max_length and t is not ColumnType.ENUM:
            text = text[: col.max_length].rstrip()
        return text
    return value


def _enforce_unique_sets(
    ctx: _Context, table: Table, columns: dict[str, list[Value]], n: int, plans: dict[str, ColumnPlan]
) -> None:
    for cols in table.unique_column_sets:
        names = [table.column(c).name for c in cols]
        if any(
            isinstance(plans[c.lower()].recipe, ForeignKeyRecipe) and columns[c][0] is None
            for c in names
            if n
        ):
            continue  # deferred FK participating in a unique set is filled later
        seen: set[tuple[Value, ...]] = set()
        for i in range(n):
            key = tuple(columns[c][i] for c in names)
            if any(k is None for k in key):
                continue  # NULLs never collide in SQL unique constraints
            if key in seen:
                _resolve_duplicate(ctx, table, names, columns, i, seen, plans)
            seen.add(tuple(columns[c][i] for c in names))


def _resolve_duplicate(
    ctx: _Context,
    table: Table,
    names: list[str],
    columns: dict[str, list[Value]],
    i: int,
    seen: set[tuple[Value, ...]],
    plans: dict[str, ColumnPlan],
) -> None:
    """Re-draw the last column of the unique set (text → suffix, numbers → re-sample) until the tupl"""
    target = table.column(names[-1])
    recipe = plans[target.name.lower()].recipe
    current = columns[target.name][i]
    for _ in range(UNIQUE_ATTEMPTS):
        if isinstance(recipe, IntRangeRecipe):
            candidate: Value = _int_in(ctx.rng, recipe.min, recipe.max, recipe.distribution)
        elif isinstance(recipe, DecimalRangeRecipe):
            scale = recipe.scale if recipe.scale is not None else (target.scale or 2)
            candidate = _decimal_in(ctx.rng, recipe.min, recipe.max, scale, recipe.distribution)
        elif isinstance(recipe, EnumWeightedRecipe):
            candidate = ctx.rng.choice(recipe.values)
        elif isinstance(recipe, ForeignKeyRecipe):
            pool = [v for v in columns[target.name] if v is not None]
            candidate = ctx.rng.choice(pool) if pool else current
        elif isinstance(current, str):
            candidate = _suffix_unique(target, current, {k[-1] for k in seen})
        else:
            break
        key = (*(columns[c][i] for c in names[:-1]), candidate)
        if key not in seen:
            columns[target.name][i] = _conform(target, candidate)
            return
    raise ExpansionError(
        f"{table.name}: cannot satisfy UNIQUE ({', '.join(names)}) for {len(columns[names[0]])} rows — "
        "widen the value range or lower the row count"
    )


# --- second pass: deferred (cyclic / self-referencing) FKs ----------------------------------------
def _fill_deferred(ctx: _Context, schema: Schema, table: Table, column: str, plan: GenerationPlan) -> None:
    fk = table.fk_for_column(column)
    tplan = plan.table(table.name)
    assert fk is not None and tplan is not None
    cplan = tplan.column(column)
    col = table.column(column)
    parent = schema.table(fk.ref_table)
    ref_col = parent.column(fk.ref_columns[fk.columns.index(column)]).name
    frame = ctx.tables[table.name]
    parents = [v for v in ctx.tables[parent.name][ref_col].tolist() if v is not None]
    own_pk = frame[table.primary_key[0]].tolist() if len(table.primary_key) == 1 else [None] * len(frame)
    null_ratio = cplan.null_ratio if cplan is not None and col.nullable else 0.0
    values: list[Value] = []
    for i in range(len(frame)):
        if not parents or (col.nullable and ctx.rng.random() < null_ratio):
            values.append(None)
            continue
        choice = ctx.rng.choice(parents)
        if parent.name == table.name and len(parents) > 1 and choice == own_pk[i]:
            choice = ctx.rng.choice([p for p in parents if p != own_pk[i]])
        values.append(_conform(col, choice))
    if not parents and not col.nullable:
        raise ExpansionError(f"{table.name}.{column} is NOT NULL but parent {parent.name} has no rows")
    frame[col.name] = pd.Series(values, dtype=object)


# --- post-pass: aggregate columns from children ------------------------------------------------------------
def _fill_aggregates(ctx: _Context, schema: Schema, plan: GenerationPlan) -> None:
    for tplan in plan.tables:
        if not schema.has_table(tplan.table):
            continue
        table = schema.table(tplan.table)
        for cplan in tplan.columns:
            r = cplan.recipe
            if not isinstance(r, AggregateRecipe) or not table.has_column(cplan.column):
                continue
            col = table.column(cplan.column)
            child = schema.table(r.child_table)
            fk = child.fk_for_column(r.child_fk_column)
            if fk is None:
                raise ExpansionError(f"{table.name}.{col.name}: {child.name}.{r.child_fk_column} is not a FK")
            ref_col = table.column(
                fk.ref_columns[fk.columns.index(child.column(r.child_fk_column).name)]
            ).name
            child_frame = ctx.tables[child.name]
            keys = child_frame[child.column(r.child_fk_column).name].tolist()
            values = (
                child_frame[child.column(r.child_column).name].tolist() if r.child_column else [1] * len(keys)
            )
            groups: dict[Value, list[float]] = {}
            for key, value in zip(keys, values, strict=True):
                if key is None or (value is None and r.agg != "count"):
                    continue
                groups.setdefault(key, []).append(1.0 if r.agg == "count" else float(value))
            frame = ctx.tables[table.name]
            result: list[Value] = []
            for key in frame[ref_col].tolist():
                items = groups.get(key)
                if not items:
                    result.append(_conform(col, r.default))
                    continue
                agg = {
                    "sum": sum(items),
                    "count": float(len(items)),
                    "min": min(items),
                    "max": max(items),
                    "avg": sum(items) / len(items),
                }
                result.append(_conform(col, agg[r.agg]))
            frame[col.name] = pd.Series(result, dtype=object)
