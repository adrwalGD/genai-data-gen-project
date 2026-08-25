"""F3.2 — planner merge logic with a FakeLLM: valid overrides applied, invalid ones dropped with reasons."""

from genai_data_gen_project.generation import heuristics, planner
from genai_data_gen_project.generation.expander import expand
from genai_data_gen_project.generation.recipes import (
    DecimalRangeRecipe,
    EnumWeightedRecipe,
    ForeignKeyRecipe,
    TextPoolRecipe,
    validate_plan,
)
from genai_data_gen_project.generation.validator import validate
from genai_data_gen_project.schema.parser import parse_ddl
from tests.fakes import FakeLLM


def canned_output() -> dict:  # type: ignore[type-arg]
    return {
        "table_rows": [{"table": "Employee_Projects", "rows": 300}, {"table": "Nowhere", "rows": 5}],
        "overrides": [
            {"table": "Employees", "column": "salary", "kind": "decimal_range", "min": 50000, "max": 90000,
             "distribution": "normal", "rationale": "instruction: 50k-90k"},
            {"table": "Companies", "column": "city", "kind": "enum_weighted",
             "values": ["Warszawa", "Kraków", "Gdańsk"], "weights": [3, 2, 1]},
            {"table": "Employees", "column": "employment_status", "kind": "enum_weighted",
             "values": ["Full-time", "Contract"], "weights": [7, 3]},
            {"table": "Projects", "column": "name", "kind": "text_pool", "brief": "IT project code names",
             "unique": True, "examples": ["Project Aurora", "Project Basalt"]},
            {"table": "Employees", "column": "hire_date", "kind": "date_window", "start": "2020-01-01",
             "end": "2025-12-31"},
            # invalid ones — each must be dropped with a reason
            {"table": "Employees", "column": "employee_id", "kind": "int_range", "min": 1, "max": 9},
            {"table": "Employees", "column": "department_id", "kind": "constant", "value": "1"},
            {"table": "Employees", "column": "employment_status", "kind": "enum_weighted",
             "values": ["Freelance"]},
            {"table": "Employees", "column": "first_name", "kind": "null_ratio_only", "null_ratio": 0.5},
            {"table": "Employees", "column": "salary", "kind": "faker", "provider": "city"},
            {"table": "Employees", "column": "ghost", "kind": "boolean"},
            {"table": "Employees", "column": "salary", "kind": "decimal_range", "min": 10},
            {"table": "Employees", "column": "middle_name", "kind": "faker", "provider": "not_a_provider"},
        ],
        "notes": ["Interpreted 'Polish cities' as the three largest cities."],
    }  # fmt: skip


def test_merge_applies_valid_overrides_and_drops_invalid_ones(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["company"])
    fake = FakeLLM(structured={"PlannerOutput": canned_output()})
    plan, output = planner.plan_with_llm(
        schema, "salaries 50k-90k, Polish cities, 30% contractors", 100, fake
    )
    assert validate_plan(plan, schema) == []
    assert len(output.overrides) == 13
    salary = plan.table("Employees").column("salary")  # type: ignore[union-attr]
    assert isinstance(salary.recipe, DecimalRangeRecipe) and (salary.recipe.min, salary.recipe.max) == (
        50000,
        90000,
    )
    assert salary.recipe.scale == 2 and salary.rationale == "instruction: 50k-90k"
    city = plan.table("Companies").column("city").recipe  # type: ignore[union-attr]
    assert isinstance(city, EnumWeightedRecipe) and city.values[0] == "Warszawa"
    status = plan.table("Employees").column("employment_status").recipe  # type: ignore[union-attr]
    assert isinstance(status, EnumWeightedRecipe) and status.values == ["Full-time", "Contract"]
    name = plan.table("Projects").column("name").recipe  # type: ignore[union-attr]
    assert (
        isinstance(name, TextPoolRecipe)
        and name.unique
        and name.examples == ["Project Aurora", "Project Basalt"]
    )
    assert plan.table("Employee_Projects").rows == 300 and plan.table("Employees").rows == 100  # type: ignore[union-attr]
    # keys stay untouched
    assert plan.table("Employees").column("employee_id").recipe.kind == "sequence"  # type: ignore[union-attr]
    assert isinstance(plan.table("Employees").column("department_id").recipe, ForeignKeyRecipe)  # type: ignore[union-attr]
    notes = "\n".join(plan.notes)
    assert "llm: Interpreted 'Polish cities'" in notes
    assert "dropped row count for unknown table 'Nowhere'" in notes
    assert "employee_id (int_range): primary/foreign key recipes cannot be overridden" in notes
    assert "department_id (constant): primary/foreign key recipes cannot be overridden" in notes
    assert "enum values ['Freelance']" in notes
    assert "first_name (null_ratio_only): null_ratio > 0 on a NOT NULL column" in notes
    assert "salary (faker): recipe 'faker' is not allowed for type decimal" in notes
    assert "ghost (boolean): unknown column" in notes
    assert "salary (decimal_range): invalid parameters: decimal_range requires max" in notes
    assert "unknown Faker provider 'not_a_provider'" in notes
    assert "llm overrides applied: 5/13" in notes
    # the merged plan still expands and validates cleanly
    tables = expand(schema, plan, seed=3)
    assert validate(schema, tables).ok
    assert all(50000 <= float(s) <= 90000 for s in tables["Employees"]["salary"])
    assert set(tables["Companies"]["city"].dropna()) <= {"Warszawa", "Kraków", "Gdańsk"}
    assert len(tables["Employee_Projects"]) == 300


def test_prompt_contains_schema_plan_and_instructions(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    base = heuristics.plan(schema, 50)
    prompt = planner.build_prompt(schema, base, "  only vegan dishes ", 50)
    assert "SCHEMA:\n7 tables: Restaurants" in prompt
    assert "CURRENT PLAN (default 50 rows per table):\nTABLE Restaurants rows=50" in prompt
    assert "  cuisine_type: enum_weighted(values=" in prompt and "null_ratio=" in prompt
    assert "USER INSTRUCTIONS:\nonly vegan dishes" in prompt
    assert "(none" in planner.build_prompt(schema, base, "   ", 50)
    fake = FakeLLM(structured={"PlannerOutput": {"table_rows": [], "overrides": [], "notes": []}})
    plan, _ = planner.plan_with_llm(schema, None, 50, fake, temperature=0.1)
    assert fake.calls[0]["temperature"] == 0.1 and fake.calls[0]["model"] == "PlannerOutput"
    assert plan.notes[-1] == "llm overrides applied: 0/0"
    assert [t.rows for t in plan.tables] == [50] * 7


def test_datetime_and_null_ratio_overrides(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    output = {
        "overrides": [
            {"table": "Orders", "column": "order_date", "kind": "datetime_window", "start": "2025-01-01",
             "end": "2025-06-30T23:59:59"},
            {"table": "Orders", "column": "delivery_address", "kind": "null_ratio_only", "null_ratio": 0.6},
            {"table": "Menu", "column": "available", "kind": "boolean", "true_ratio": 0.95},
            {"table": "Restaurants", "column": "phone_number", "kind": "pattern",
             "template": "+48 ### ### ###"},
        ]
    }  # fmt: skip
    plan = planner.merge(heuristics.plan(schema, 20), planner.PlannerOutput.model_validate(output), schema)
    assert validate_plan(plan, schema) == []
    od = plan.table("Orders").column("order_date").recipe  # type: ignore[union-attr]
    assert od.kind == "datetime_window" and od.start.year == 2025 and od.end.month == 6  # type: ignore[union-attr]
    assert plan.table("Orders").column("delivery_address").null_ratio == 0.6  # type: ignore[union-attr]
    assert plan.table("Menu").column("available").recipe.true_ratio == 0.95  # type: ignore[union-attr]
    tables = expand(schema, plan, seed=1)
    phones = [p for p in tables["Restaurants"]["phone_number"] if p is not None]
    assert phones and all(p.startswith("+48 ") and len(p) == 15 for p in phones)
    dates = [d for d in tables["Orders"]["order_date"] if d is not None]
    assert dates and all(d.year == 2025 and d.month <= 6 for d in dates)


def test_m3_verifier_findings_constant_coercion_pool_inheritance_locale_and_row_cap(
    sample_ddl: dict[str, str],
) -> None:
    from faker.providers.person.pl_PL import Provider as PolishNames

    from genai_data_gen_project.generation.recipes import FakerRecipe

    schema = parse_ddl(sample_ddl["company"])
    base = heuristics.plan(schema, 50)
    output = {
        "table_rows": [{"table": "Employee_Projects", "rows": 999_999}],
        "overrides": [
            {"table": "Employees", "column": "salary", "kind": "constant", "value": "confidential"},
            {"table": "Employees", "column": "hire_date", "kind": "constant", "value": "not a date"},
            {"table": "Employee_Projects", "column": "hours_worked", "kind": "constant", "value": "many"},
            {"table": "Employee_Benefits", "column": "coverage_amount", "kind": "constant",
             "value": "1200.50"},
            {"table": "Projects", "column": "name", "kind": "text_pool", "brief": "IT project code names",
             "unique": False},
            {"table": "Employees", "column": "first_name", "kind": "faker", "provider": "first_name",
             "locale": "pl_PL"},
            {"table": "Employees", "column": "last_name", "kind": "faker", "provider": "last_name",
             "locale": "xx_NOPE"},
        ],
    }  # fmt: skip
    plan = planner.merge(base, planner.PlannerOutput.model_validate(output), schema, max_rows=5000)
    assert validate_plan(plan, schema) == []
    notes = "\n".join(plan.notes)
    assert (
        "salary (constant): invalid parameters: constant 'confidential' does not fit DECIMAL(10, 2)" in notes
    )
    assert "hire_date (constant): invalid parameters: constant 'not a date' does not fit DATE" in notes
    assert "hours_worked (constant): invalid parameters: constant 'many'" in notes
    assert "last_name (faker): unknown Faker locale 'xx_NOPE'" in notes
    assert (
        "row count for Employee_Projects clamped to 250 (5x the requested 50; planner asked for 999999)"
        in notes
    )
    assert plan.table("Employee_Projects").rows == 250  # type: ignore[union-attr]
    coverage = plan.table("Employee_Benefits").column("coverage_amount").recipe  # type: ignore[union-attr]
    assert coverage.kind == "constant" and coverage.value == "1200.50"  # type: ignore[union-attr]
    name = plan.table("Projects").column("name").recipe  # type: ignore[union-attr]
    assert isinstance(name, TextPoolRecipe) and name.unique is True and name.fallback_provider == "company"
    first = plan.table("Employees").column("first_name").recipe  # type: ignore[union-attr]
    assert isinstance(first, FakerRecipe) and first.locale == "pl_PL"
    plan.table("Employee_Projects").rows = 40  # type: ignore[union-attr]
    tables = expand(schema, plan, seed=3)
    assert validate(schema, tables).ok
    polish = set(PolishNames.first_names)
    assert all(n in polish for n in tables["Employees"]["first_name"])
    assert all(str(c) == "1200.50" for c in tables["Employee_Benefits"]["coverage_amount"] if c is not None)
