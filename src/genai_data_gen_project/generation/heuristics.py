"""Offline planner: assigns a recipe to every column from names, types and constraints (F2.1).

This is the baseline the LLM planner (F3.2) refines. It must always produce a plan that `validate_plan`
accepts for any parsable schema, so the whole pipeline works without network access.
"""

from __future__ import annotations

import re
from datetime import date, datetime

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from ..schema.models import Column, ColumnType, Schema, Table
from .recipes import (
    BooleanRecipe,
    ColumnPlan,
    ColumnRecipe,
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

TODAY = date(2026, 8, 25)  # fixed anchor keeps plans deterministic across runs/tests
RECENT_START = date(2023, 1, 1)
HISTORY_START = date(2015, 1, 1)

LANGUAGES = ["English", "Spanish", "French", "German", "Polish", "Italian", "Portuguese", "Japanese"]
EDITIONS = ["1st", "2nd", "3rd", "Revised", "Anniversary", "Deluxe"]
OPENING_HOURS = ["9:00 AM - 5:00 PM", "11:00 AM - 10:00 PM", "8:00 AM - 8:00 PM", "10:00 AM - 6:00 PM"]
BENEFITS = ["Health Insurance", "Dental", "Vision", "401(k) Match", "Life Insurance", "Gym Membership"]
GENRES = ["Fiction", "Mystery", "Science Fiction", "Fantasy", "Biography", "History", "Romance", "Children"]
ROLES = ["Developer", "Tech Lead", "QA Engineer", "Analyst", "Project Manager", "Designer", "Architect"]
INDUSTRIES = ["Software", "Finance", "Healthcare", "Retail", "Manufacturing", "Logistics", "Education"]

# (regex on lowercase column name, brief for the LLM pool, Faker fallback provider, kwargs)
TEXT_POOL_RULES: list[tuple[str, str, str, dict[str, str | int | float | bool]]] = [
    (r"^title$", "realistic {table} titles", "catch_phrase", {}),
    (r"(item_name|dish|product)", "realistic {table} item names", "catch_phrase", {}),
    (
        r"(description|biography|bio|summary|review_text|comments?|goals_set|special_instructions|notes?)$",
        "realistic {table} {column} text (1-2 sentences)",
        "sentence",
        {"nb_words": 12},
    ),
    (r"^(street|address)", "", "street_address", {}),
]


def check_bounds(expression: str, column: str) -> tuple[float | None, float | None, list[str] | None]:
    """Extract (min, max, in_values) for `column` from a CHECK expression; None where nothing is stated."""
    try:
        tree = sqlglot.parse_one(expression, read="postgres")
    except ParseError:
        return None, None, None
    lo: float | None = None
    hi: float | None = None
    values: list[str] | None = None

    def is_col(e: exp.Expression) -> bool:
        return isinstance(e, exp.Column) and e.name.lower() == column.lower()

    def num(e: exp.Expression) -> float | None:
        if isinstance(e, exp.Literal) and not e.is_string:
            return float(e.this)
        if isinstance(e, exp.Neg) and isinstance(e.this, exp.Literal):
            return -float(e.this.this)
        return None

    for between in tree.find_all(exp.Between):
        if is_col(between.this):
            lo, hi = num(between.args["low"]), num(between.args["high"])
    for in_node in tree.find_all(exp.In):
        if is_col(in_node.this):
            values = [str(v.this) for v in in_node.expressions if isinstance(v, exp.Literal)]
    for cls, on_left, on_right in (
        (exp.GTE, "lo", "hi"),
        (exp.GT, "lo_strict", "hi_strict"),
        (exp.LTE, "hi", "lo"),
        (exp.LT, "hi_strict", "lo_strict"),
    ):
        for cmp in tree.find_all(cls):
            left, right = cmp.this, cmp.expression
            if is_col(left) and num(right) is not None:
                target, value = on_left, num(right)
            elif is_col(right) and num(left) is not None:
                target, value = on_right, num(left)
            else:
                continue
            assert value is not None
            if target.startswith("lo"):
                lo = value + (1 if target.endswith("strict") and value == int(value) else 0)
            else:
                hi = value - (1 if target.endswith("strict") and value == int(value) else 0)
    return lo, hi, values


def rows_for(table: Table, rows_per_table: int | dict[str, int], default: int) -> int:
    if isinstance(rows_per_table, int):
        return rows_per_table
    for name, n in rows_per_table.items():
        if name.lower() == table.name.lower():
            return n
    return default


def plan(
    schema: Schema, rows_per_table: int | dict[str, int] = 100, *, default_rows: int = 100
) -> GenerationPlan:
    """Heuristic plan covering every column of every table."""
    tables = [
        TablePlan(
            table=t.name,
            rows=rows_for(t, rows_per_table, default_rows),
            columns=[plan_column(schema, t, c) for c in t.columns],
            description=None,
        )
        for t in schema.tables
    ]
    return GenerationPlan(tables=tables, notes=["heuristic plan (names, types, constraints)"])


def plan_column(schema: Schema, table: Table, col: Column) -> ColumnPlan:
    recipe = recipe_for(schema, table, col)
    return ColumnPlan(column=col.name, recipe=recipe, null_ratio=null_ratio_for(table, col, recipe))


def null_ratio_for(table: Table, col: Column, recipe: ColumnRecipe) -> float:
    if not col.nullable or col.primary_key:
        return 0.0
    name = col.name.lower()
    if re.search(r"(death|termination|return|end)_?(date|at)?$", name) or name.startswith("end_"):
        return 0.85 if "death" in name else 0.35
    if isinstance(recipe, ForeignKeyRecipe):
        return 0.15
    if re.search(
        r"(description|biography|comments?|notes?|special_instructions|middle_name|delivery_address)", name
    ):
        return 0.3
    if col.type.is_textual or col.type.is_numeric or col.type.is_temporal:
        return 0.08
    return 0.05


def recipe_for(schema: Schema, table: Table, col: Column) -> ColumnRecipe:
    name = col.name.lower()
    fk = table.fk_for_column(col.name)
    if fk is not None and len(fk.columns) == 1:
        return ForeignKeyRecipe(ref_table=fk.ref_table, ref_column=fk.ref_columns[0])
    if col.primary_key and col.type.is_integer and len(table.primary_key) == 1:
        return SequenceRecipe(start=1)
    if col.type is ColumnType.ENUM:
        return EnumWeightedRecipe(values=list(col.enum_values), weights=_enum_weights(col.enum_values))
    if col.type is ColumnType.BOOLEAN:
        return BooleanRecipe(true_ratio=0.8 if re.search(r"(active|available|enabled|is_)", name) else 0.5)
    lo, hi, in_values = (None, None, None)
    if col.check:
        lo, hi, in_values = check_bounds(col.check, col.name)
    if in_values and col.type.is_textual:
        return EnumWeightedRecipe(values=in_values)
    if col.type.is_temporal:
        return _temporal_recipe(table, col)
    if col.type.is_numeric:
        return _numeric_recipe(table, col, lo, hi)
    return _text_recipe(schema, table, col)


def _enum_weights(values: list[str]) -> list[float] | None:
    """Mild realism: the first (usually 'normal') value is most common, terminal states rarer."""
    if len(values) < 3:
        return None
    return [3.0] + [1.5] * (len(values) - 2) + [0.5]


def _temporal_recipe(table: Table, col: Column) -> ColumnRecipe:
    name = col.name.lower()
    is_dt = col.type is ColumnType.DATETIME
    birth = _find(table, r"(birth|dob|born)")
    start_like = _find(table, r"^(start|loan|hire|join|order|enrollment|registration|issue|created)")
    if re.search(r"(birth|dob|born)", name):
        return DateWindowRecipe(start=date(1945, 1, 1), end=date(2006, 12, 31))
    if "death" in name:
        return DateWindowRecipe(
            start=date(1990, 1, 1),
            end=TODAY,
            after_column=birth,
            min_days_after=365 * 30,
            max_days_after=365 * 95,
        )
    if name.startswith("due"):
        return _after(is_dt, start_like, 14, 28, RECENT_START)
    if name.startswith("return") or name.startswith("termination") or name.startswith("end"):
        return _after(
            is_dt,
            start_like,
            1 if name.startswith("return") else 30,
            45 if name.startswith("return") else 900,
            RECENT_START,
        )
    if re.search(r"(hire|join|enrollment|registration|publication|start)", name):
        return _window(
            is_dt, HISTORY_START if not re.search(r"publication", name) else date(1950, 1, 1), TODAY
        )
    if re.search(r"(order|loan|review|created|updated|transaction|payment)", name):
        return _window(is_dt, RECENT_START, TODAY)
    return _window(is_dt, HISTORY_START, TODAY)


def _find(table: Table, pattern: str) -> str | None:
    for c in table.columns:
        if c.type.is_temporal and re.search(pattern, c.name.lower()):
            return c.name
    return None


def _window(is_dt: bool, start: date, end: date) -> ColumnRecipe:
    if is_dt:
        return DateTimeWindowRecipe(
            start=datetime.combine(start, datetime.min.time()), end=datetime.combine(end, datetime.min.time())
        )
    return DateWindowRecipe(start=start, end=end)


def _after(is_dt: bool, after: str | None, min_days: int, max_days: int, start: date) -> ColumnRecipe:
    if is_dt:
        return DateTimeWindowRecipe(
            start=datetime.combine(start, datetime.min.time()),
            end=datetime.combine(TODAY, datetime.min.time()),
            after_column=after,
            min_hours_after=min_days * 24,
            max_hours_after=max_days * 24,
        )
    return DateWindowRecipe(
        start=start, end=TODAY, after_column=after, min_days_after=min_days, max_days_after=max_days
    )


def _numeric_recipe(table: Table, col: Column, lo: float | None, hi: float | None) -> ColumnRecipe:
    name = col.name.lower()
    scale = col.scale if col.type is ColumnType.DECIMAL else None
    cap = 10 ** ((col.precision or 10) - (scale or 0)) - 1 if col.type is ColumnType.DECIMAL else 10**9
    if lo is not None or hi is not None:
        low, high = lo if lo is not None else 0, hi if hi is not None else (lo or 0) + 100
        if col.type.is_integer:
            return IntRangeRecipe(min=int(low), max=int(high), distribution=Distribution.SKEWED_HIGH)
        return DecimalRangeRecipe(min=low, max=high, scale=scale, distribution=Distribution.SKEWED_HIGH)
    rules: list[tuple[str, float, float, Distribution]] = [
        (r"salary", 32000, 185000, Distribution.NORMAL),
        (r"budget", 20000, 4_500_000, Distribution.SKEWED_LOW),
        (r"coverage", 1000, 250_000, Distribution.SKEWED_LOW),
        (r"total_amount|amount_due|grand_total", 8, 320, Distribution.SKEWED_LOW),
        (r"subtotal|line_total", 4, 160, Distribution.SKEWED_LOW),
        (r"price|cost|fee", 2.5, 65, Distribution.SKEWED_LOW),
        (r"fine|penalty", 0, 45, Distribution.SKEWED_LOW),
        (r"hours", 1, 1800, Distribution.SKEWED_LOW),
        (r"rating|score|stars", 1, 5, Distribution.SKEWED_HIGH),
        (r"pages", 60, 1200, Distribution.NORMAL),
        (r"quantity|qty|count|number_of", 1, 12, Distribution.SKEWED_LOW),
        (r"percent|pct|discount", 0, 100, Distribution.UNIFORM),
        (r"(^|_)years?$", 1950, TODAY.year, Distribution.SKEWED_HIGH),
        (r"(^|_)age$", 18, 90, Distribution.NORMAL),
    ]
    if name.startswith("available_") and table.has_column(name.replace("available_", "")):
        base = table.column(name.replace("available_", "")).name
        return DerivedRecipe(expression=f"max({base} - randint(0, {base}), 0)")
    if name.endswith("_id"):  # id-like without a declared FK (e.g. a commented "FK to Employees")
        return IntRangeRecipe(min=1, max=100)
    for pattern, low, high, dist in rules:
        if re.search(pattern, name):
            high = min(high, cap)
            if col.type.is_integer:
                return IntRangeRecipe(min=int(low), max=int(high), distribution=dist)
            return DecimalRangeRecipe(min=low, max=high, scale=scale, distribution=dist)
    if col.type.is_integer:
        return IntRangeRecipe(min=0, max=min(1000, cap))
    return DecimalRangeRecipe(min=0, max=min(1000, cap), scale=scale)


def _text_recipe(schema: Schema, table: Table, col: Column) -> ColumnRecipe:
    name = col.name.lower()
    tname = table.name.lower()
    max_len = col.max_length or 10_000
    has_first_last = table.has_column("first_name") and table.has_column("last_name")

    def faker(provider: str, unique: bool = False, **kwargs: str | int | float | bool) -> FakerRecipe:
        return FakerRecipe(provider=provider, kwargs=dict(kwargs), unique=unique)

    if re.search(r"e[-_]?mail", name):
        if has_first_last:
            return PatternRecipe(
                template="{col:first_name|slug}.{col:last_name|slug}@{faker:free_email_domain}",
                unique=col.unique,
            )
        return faker("email", unique=col.unique)
    if re.search(r"(first_name|firstname|given_name)", name):
        return faker("first_name")
    if re.search(r"(last_name|lastname|surname|family_name)", name):
        return faker("last_name")
    if "middle_name" in name:
        return faker("first_name")
    if re.search(r"(phone|mobile|fax|contact_number)", name):
        return PatternRecipe(template="###-###-####" if max_len >= 12 else "##########", unique=col.unique)
    if re.search(r"(zip|postal)", name):
        return faker("postcode")
    if name == "city" or name.endswith("_city"):
        return faker("city")
    if name == "state" or name.endswith("_state"):
        return faker("state" if max_len >= 14 else "state_abbr")
    if name == "country" or name.endswith("_country") or "nationality" in name:
        return faker("country")
    if re.search(r"address|street", name):
        return faker("street_address")
    if re.search(r"(website|url|homepage)", name):
        return faker("url")
    if "isbn" in name:
        return faker("isbn13", unique=True, separator="-")
    if re.search(r"(license|licence|plate)", name):
        return PatternRecipe(template="%%-######", unique=col.unique)
    if re.search(r"(job_title|position|occupation)", name):
        return faker("job")
    if name == "role":
        return EnumWeightedRecipe(values=ROLES)
    if "genre" in name:
        return EnumWeightedRecipe(values=GENRES)
    if name == "language":
        return EnumWeightedRecipe(values=LANGUAGES, weights=[6, 2, 1.5, 1.5, 1, 1, 1, 0.5])
    if name == "edition":
        return EnumWeightedRecipe(values=EDITIONS, weights=[5, 2, 1, 1, 0.5, 0.5])
    if "opening_hours" in name or name.endswith("_hours"):
        return EnumWeightedRecipe(values=OPENING_HOURS)
    if "benefit_type" in name:
        return EnumWeightedRecipe(values=BENEFITS)
    if "industry" in name:
        return EnumWeightedRecipe(values=INDUSTRIES)
    if name == "location":
        return faker("city")
    if name == "name":
        if re.search(r"(compan|publisher|vendor|supplier|organi)", tname):
            return TextPoolRecipe(
                brief=f"realistic {table.name} company names", unique=True, fallback_provider="company"
            )
        if re.search(r"(department|team|division)", tname):
            return TextPoolRecipe(brief="realistic corporate department names", fallback_provider="bs")
        if re.search(r"(restaurant|store|shop|branch|hotel|cafe|project)", tname):
            return TextPoolRecipe(
                brief=f"realistic {table.name} names", unique=True, fallback_provider="company"
            )
        return TextPoolRecipe(brief=f"realistic {table.name} names", fallback_provider="company")
    for pattern, brief, provider, kwargs in TEXT_POOL_RULES:
        if re.search(pattern, name):
            if not brief:
                return faker(provider)
            return TextPoolRecipe(
                brief=brief.format(table=table.name, column=col.name),
                unique=name == "title",
                fallback_provider=provider,
                fallback_kwargs=kwargs,
            )
    if col.type is ColumnType.TEXT:
        return faker("paragraph", nb_sentences=2)
    if max_len <= 3:
        return PatternRecipe(template="%" * max_len, unique=col.unique)
    return faker("word") if max_len < 12 else FakerRecipe(provider="sentence", kwargs={"nb_words": 3})
