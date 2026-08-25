"""F2.1 — recipes contract + offline heuristic planner covers every column of the sample schemas validly."""

from datetime import date

import pytest
from pydantic import ValidationError

from genai_data_gen_project.generation import heuristics
from genai_data_gen_project.generation.recipes import (
    ColumnPlan,
    DateTimeWindowRecipe,
    DateWindowRecipe,
    DecimalRangeRecipe,
    DerivedRecipe,
    EnumWeightedRecipe,
    FakerRecipe,
    ForeignKeyRecipe,
    GenerationPlan,
    IntRangeRecipe,
    PatternRecipe,
    SequenceRecipe,
    TablePlan,
    TextPoolRecipe,
    validate_plan,
)
from genai_data_gen_project.schema.parser import parse_ddl


@pytest.mark.parametrize("name", ["library", "restaurants", "company"])
def test_heuristic_plan_is_complete_and_valid(name: str, sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl[name])
    plan = heuristics.plan(schema, rows_per_table=250)
    assert validate_plan(plan, schema) == []
    assert [t.table for t in plan.tables] == schema.table_names
    assert all(t.rows == 250 for t in plan.tables)
    for table in schema.tables:
        tp = plan.table(table.name)
        assert tp is not None and [c.column for c in tp.columns] == table.column_names


def test_rows_per_table_overrides(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    plan = heuristics.plan(schema, {"orders": 500, "Order_Items": 1500}, default_rows=100)
    assert plan.table("Orders").rows == 500  # type: ignore[union-attr]
    assert plan.table("order_items").rows == 1500  # type: ignore[union-attr]
    assert plan.table("Menu").rows == 100  # type: ignore[union-attr]


def test_library_recipes_reflect_names_types_and_constraints(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["library"])
    plan = heuristics.plan(schema)

    def recipe(table: str, column: str):  # type: ignore[no-untyped-def]
        cp = plan.table(table).column(column)  # type: ignore[union-attr]
        assert cp is not None
        return cp

    assert isinstance(recipe("Books", "book_id").recipe, SequenceRecipe)
    fk = recipe("Books", "author_id").recipe
    assert isinstance(fk, ForeignKeyRecipe) and (fk.ref_table, fk.ref_column) == ("Authors", "author_id")
    isbn = recipe("Books", "isbn").recipe
    assert isinstance(isbn, FakerRecipe) and isbn.provider == "isbn13" and isbn.unique
    fmt = recipe("Books", "format").recipe
    assert isinstance(fmt, EnumWeightedRecipe) and fmt.values == [
        "Hardcover",
        "Paperback",
        "E-book",
        "Audiobook",
    ]
    title = recipe("Books", "title").recipe
    assert isinstance(title, TextPoolRecipe) and title.unique and title.fallback_provider == "catch_phrase"
    email = recipe("Library_Members", "email")
    assert isinstance(email.recipe, PatternRecipe) and email.recipe.unique
    assert "{col:first_name|slug}" in email.recipe.template and email.null_ratio == 0.0
    manager = recipe("Library_Branches", "manager_id")
    assert isinstance(manager.recipe, ForeignKeyRecipe) and manager.recipe.ref_table == "Employees"
    assert 0 < manager.null_ratio < 0.5  # nullable, deferred FK
    due = recipe("Book_Loans", "due_date").recipe
    assert isinstance(due, DateWindowRecipe) and due.after_column == "loan_date"
    assert (due.min_days_after, due.max_days_after) == (14, 28)
    ret = recipe("Book_Loans", "return_date")
    assert isinstance(ret.recipe, DateTimeWindowRecipe) and ret.recipe.after_column == "loan_date"
    assert ret.null_ratio > 0.2
    death = recipe("Authors", "death_date")
    assert isinstance(death.recipe, DateWindowRecipe) and death.recipe.after_column == "birth_date"
    assert death.null_ratio >= 0.8
    birth = recipe("Authors", "birth_date").recipe
    assert isinstance(birth, DateWindowRecipe) and birth.end <= date(2006, 12, 31)
    fine = recipe("Book_Loans", "fine_amount").recipe
    assert isinstance(fine, DecimalRangeRecipe) and fine.min == 0 and fine.max <= 45
    avail = recipe("Book_Inventory", "available_quantity").recipe
    assert isinstance(avail, DerivedRecipe) and "quantity" in avail.expression
    assert recipe("Book_Inventory", "quantity").recipe.kind == "int_range"
    assert recipe("Publishers", "contact_phone").recipe == PatternRecipe(
        template="###-###-####", unique=False
    )
    assert recipe("Employees", "job_title").recipe == FakerRecipe(provider="job")


def test_check_constraints_drive_numeric_ranges(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    plan = heuristics.plan(schema)
    rating = plan.table("Reviews").column("rating").recipe  # type: ignore[union-attr]
    assert isinstance(rating, IntRangeRecipe) and (rating.min, rating.max) == (1, 5)
    rest_rating = plan.table("Restaurants").column("rating").recipe  # type: ignore[union-attr]
    assert isinstance(rest_rating, DecimalRangeRecipe) and rest_rating.max <= 9.99 and rest_rating.scale == 2
    available = plan.table("Menu").column("available").recipe  # type: ignore[union-attr]
    assert available.kind == "boolean" and available.true_ratio == 0.8  # type: ignore[union-attr]
    cuisine = plan.table("Restaurants").column("cuisine_type").recipe  # type: ignore[union-attr]
    assert isinstance(cuisine, EnumWeightedRecipe) and cuisine.weights is not None
    assert len(cuisine.weights) == 6 and cuisine.weights[0] == max(cuisine.weights)


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("rating >= 1 AND rating <= 5", (1.0, 5.0, None)),
        ("qty BETWEEN 1 AND 100", (1.0, 100.0, None)),
        ("price > 0", (1.0, None, None)),
        ("price < 10.5", (None, 10.5, None)),
        ("5 >= rating", (None, 5.0, None)),
        ("status IN ('a', 'b')", (None, None, ["a", "b"])),
        ("not ( valid", (None, None, None)),
    ],
)
def test_check_bounds(expr: str, expected: tuple) -> None:  # type: ignore[type-arg]
    col = expr.split()[0] if not expr.startswith("5") else "rating"
    assert heuristics.check_bounds(expr, col) == expected


def test_company_manager_id_without_fk_gets_a_plain_range(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["company"])
    plan = heuristics.plan(schema)
    manager = plan.table("Departments").column("manager_id").recipe  # type: ignore[union-attr]
    assert isinstance(manager, IntRangeRecipe) and manager.min == 1
    salary = plan.table("Employees").column("salary").recipe  # type: ignore[union-attr]
    assert isinstance(salary, DecimalRangeRecipe) and salary.min >= 30000 and salary.max <= 99_999_999
    reviewer = plan.table("Performance_Reviews").column("reviewer_id").recipe  # type: ignore[union-attr]
    assert isinstance(reviewer, ForeignKeyRecipe) and reviewer.ref_table == "Employees"


def test_validate_plan_reports_problems(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    plan = heuristics.plan(schema)
    menu = plan.table("Menu")
    assert menu is not None
    menu.column("price").recipe = FakerRecipe(provider="city")  # type: ignore[union-attr]
    menu.column("category").recipe = EnumWeightedRecipe(values=["Appetizer", "Nope"])  # type: ignore[union-attr]
    menu.column("item_name").null_ratio = 0.5  # type: ignore[union-attr]  # NOT NULL column
    menu.column("restaurant_id").recipe = ForeignKeyRecipe(ref_table="Customers", ref_column="customer_id")  # type: ignore[union-attr]
    menu.columns.append(ColumnPlan(column="ghost", recipe=SequenceRecipe()))
    plan.tables.append(TablePlan(table="Nowhere", rows=1, columns=[]))
    problems = validate_plan(plan, schema)
    joined = "\n".join(problems)
    assert "Menu.price: recipe 'faker' is not allowed for type decimal" in joined
    assert "enum values ['Nope']" in joined
    assert "Menu.item_name: null_ratio > 0 on a NOT NULL column" in joined
    assert "contradicts the schema FK → Restaurants" in joined
    assert "unknown column 'ghost'" in joined and "unknown table 'Nowhere'" in joined


def test_recipe_models_validate_their_invariants() -> None:
    with pytest.raises(ValidationError):
        IntRangeRecipe(min=5, max=1)
    with pytest.raises(ValidationError):
        EnumWeightedRecipe(values=["a", "b"], weights=[1.0])
    with pytest.raises(ValidationError):
        DateWindowRecipe(start=date(2024, 1, 1), end=date(2023, 1, 1))
    plan = GenerationPlan.model_validate(
        {"tables": [{"table": "t", "rows": 3, "columns": [{"column": "c", "recipe": {"kind": "sequence"}}]}]}
    )
    assert isinstance(plan.tables[0].columns[0].recipe, SequenceRecipe)
    schema_json = GenerationPlan.model_json_schema()
    assert "ColumnPlan" in schema_json["$defs"] and "IntRangeRecipe" in schema_json["$defs"]
