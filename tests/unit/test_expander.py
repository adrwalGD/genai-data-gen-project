"""F2.2 — the expander honors every constraint class deterministically (validator arrives in F2.3)."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from genai_data_gen_project.generation import heuristics
from genai_data_gen_project.generation.expander import ExpansionError, expand
from genai_data_gen_project.generation.recipes import PatternRecipe, TextPoolRecipe
from genai_data_gen_project.schema.models import Schema
from genai_data_gen_project.schema.order import generation_order
from genai_data_gen_project.schema.parser import parse_ddl


def non_null(frame: pd.DataFrame, column: str) -> list:  # type: ignore[type-arg]
    return [v for v in frame[column].tolist() if v is not None]


def assert_constraints(schema: Schema, tables: dict[str, pd.DataFrame]) -> None:
    order = generation_order(schema)
    for table in schema.tables:
        frame = tables[table.name]
        assert list(frame.columns) == table.column_names
        for col in table.columns:
            values = frame[col.name].tolist()
            if not col.nullable:
                assert None not in values, f"{table.name}.{col.name} has NULLs"
            present = [v for v in values if v is not None]
            if col.type.is_integer:
                assert all(isinstance(v, int) and not isinstance(v, bool) for v in present), (
                    table.name,
                    col.name,
                )
            if col.type.value == "decimal":
                assert all(isinstance(v, Decimal) for v in present)
                assert all(-v.as_tuple().exponent <= (col.scale or 0) for v in present)  # type: ignore[operator]
                assert all(
                    abs(v) < Decimal(10) ** ((col.precision or 10) - (col.scale or 0)) for v in present
                )
            if col.type.value == "enum":
                assert set(present) <= set(col.enum_values), (table.name, col.name)
            if col.max_length is not None and col.type.is_textual:
                assert all(len(v) <= col.max_length for v in present), (table.name, col.name)
            if col.type.value == "date":
                assert all(type(v) is date for v in present)
            if col.type.value == "datetime":
                assert all(isinstance(v, datetime) for v in present)
            if col.type.value == "boolean":
                assert all(isinstance(v, bool) for v in present)
        for cols in table.unique_column_sets:
            keys = [tuple(row) for row in frame[cols].itertuples(index=False) if None not in row]
            assert len(keys) == len(set(keys)), f"{table.name} UNIQUE {cols} violated"
        for fk in table.foreign_keys:
            parent = tables[fk.ref_table]
            parent_values = set(non_null(parent, fk.ref_columns[0]))
            child_values = set(non_null(frame, fk.columns[0]))
            assert child_values <= parent_values, f"{table.name}.{fk.columns} dangling"
            if not order.is_deferred(table.name, fk) and not table.fk_is_nullable(fk):
                assert None not in frame[fk.columns[0]].tolist()


@pytest.mark.parametrize("name", ["library", "restaurants", "company"])
def test_sample_schemas_expand_with_all_constraints(name: str, sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl[name])
    tables = expand(schema, heuristics.plan(schema, 150), seed=7)
    assert all(len(f) == 150 for f in tables.values())
    assert_constraints(schema, tables)


def test_library_semantics(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["library"])
    tables = expand(schema, heuristics.plan(schema, 300), seed=1)
    books, loans, authors = tables["Books"], tables["Book_Loans"], tables["Authors"]
    assert books["book_id"].tolist() == list(range(1, 301))
    assert len(set(books["isbn"])) == 300
    for loan, due in zip(loans["loan_date"], loans["due_date"], strict=True):
        assert 14 <= (due - loan.date()).days <= 28
    returned = [
        (lo, r) for lo, r in zip(loans["loan_date"], loans["return_date"], strict=True) if r is not None
    ]
    assert returned and all(r > lo for lo, r in returned)
    assert 0.15 < loans["return_date"].isna().mean() < 0.6
    assert authors["death_date"].isna().mean() > 0.6
    for b, d in zip(authors["birth_date"], authors["death_date"], strict=True):
        if d is not None and b is not None:
            assert d > b + timedelta(days=365 * 29)
    inv = tables["Book_Inventory"]
    assert all(0 <= a <= q for a, q in zip(inv["available_quantity"], inv["quantity"], strict=True))
    # deferred FKs filled in the second pass, staying inside the parent key set
    managers = non_null(tables["Library_Branches"], "manager_id")
    assert managers and set(managers) <= set(tables["Employees"]["employee_id"])
    dept = non_null(tables["Employees"], "department_id")
    assert dept and set(dept) <= set(tables["Departments"]["department_id"])
    emails = tables["Library_Members"]["email"].tolist()
    assert len(set(emails)) == 300 and all("@" in e and e == e.lower() for e in emails)


def test_restaurants_checks_booleans_and_fk_distribution(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    tables = expand(schema, heuristics.plan(schema, 400), seed=3)
    ratings = tables["Reviews"]["rating"].tolist()
    assert set(ratings) <= {1, 2, 3, 4, 5} and ratings.count(5) > ratings.count(1)
    rest_rating = non_null(tables["Restaurants"], "rating")
    assert all(Decimal("1.00") <= r <= Decimal("5.00") for r in rest_rating)
    available = non_null(tables["Menu"], "available")
    assert 0.65 < sum(available) / len(available) < 0.95
    assert set(tables["Menu"]["restaurant_id"]) <= set(tables["Restaurants"]["restaurant_id"])
    assert len(set(tables["Delivery_Drivers"]["license_number"])) == 400


def test_determinism(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["company"])
    plan = heuristics.plan(schema, 60)
    a, b, c = expand(schema, plan, seed=42), expand(schema, plan, seed=42), expand(schema, plan, seed=43)
    for name in schema.table_names:
        pd.testing.assert_frame_equal(a[name], b[name])
    assert not a["Employees"]["first_name"].equals(c["Employees"]["first_name"])


def test_self_reference_and_text_pools() -> None:
    schema = parse_ddl(
        "CREATE TABLE Emp (id INT PRIMARY KEY AUTO_INCREMENT, title VARCHAR(40) NOT NULL, manager_id INT,"
        " FOREIGN KEY (manager_id) REFERENCES Emp(id));"
    )
    plan = heuristics.plan(schema, 50)
    plan.table("Emp").column("manager_id").null_ratio = 0.0  # type: ignore[union-attr]
    title = plan.table("Emp").column("title")  # type: ignore[union-attr]
    assert title is not None and isinstance(title.recipe, TextPoolRecipe) and title.recipe.unique
    pool = {("Emp", "title"): [f"Pool Title {i}" for i in range(20)]}
    frame = expand(schema, plan, seed=5, pools=pool)["Emp"]
    ids = frame["id"].tolist()
    assert all(m in ids and m != own for own, m in zip(ids, frame["manager_id"].tolist(), strict=True))
    titles = frame["title"].tolist()
    assert len(set(titles)) == 50 and sum(t.startswith("Pool Title") for t in titles) == 20


def test_pattern_rendering_and_column_dependency_order() -> None:
    schema = parse_ddl(
        "CREATE TABLE p (id INT PRIMARY KEY, code VARCHAR(40) NOT NULL, first_name VARCHAR(20) NOT NULL,"
        " last_name VARCHAR(20) NOT NULL, phone VARCHAR(20));"
    )
    plan = heuristics.plan(schema, 30)
    plan.table("p").column("code").recipe = PatternRecipe(  # type: ignore[union-attr]
        template="{col:last_name|initial}{col:first_name|slug}-{seq}-#?%", unique=True
    )
    frame = expand(schema, plan, seed=9)["p"]
    for i, (code, _first, last) in enumerate(
        zip(frame["code"], frame["first_name"], frame["last_name"], strict=True)
    ):
        assert code.startswith(last[:1]) and f"-{i + 1}-" in code
        assert code[-3].isdigit() and code[-2].islower() and code[-1].isupper()
    phones = non_null(frame, "phone")
    assert all(len(p) == 12 and p[3] == "-" and p[7] == "-" for p in phones)


def test_unique_exhaustion_and_missing_plan_errors() -> None:
    schema = parse_ddl("CREATE TABLE t (id INT PRIMARY KEY, code CHAR(1) UNIQUE NOT NULL);")
    plan = heuristics.plan(schema, 100)
    with pytest.raises(ExpansionError, match="unique"):
        expand(schema, plan, seed=1)
    plan.tables = []
    with pytest.raises(ExpansionError, match="no entry for table"):
        expand(schema, plan, seed=1)


def test_not_null_fk_to_empty_parent_is_an_error(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    plan = heuristics.plan(schema, {"Restaurants": 0}, default_rows=10)
    with pytest.raises(ExpansionError, match="Restaurants has no rows"):
        expand(schema, plan, seed=1)


def test_unique_email_suffixes_stay_valid_addresses() -> None:
    import re

    from genai_data_gen_project.generation.recipes import ConstantRecipe

    schema = parse_ddl(
        "CREATE TABLE Customers (id INT PRIMARY KEY, first_name VARCHAR(20) NOT NULL,"
        " last_name VARCHAR(20) NOT NULL, email VARCHAR(40) UNIQUE NOT NULL);"
    )
    plan = heuristics.plan(schema, 300)
    plan.table("Customers").column("first_name").recipe = ConstantRecipe(value="Ann")  # type: ignore[union-attr]
    plan.table("Customers").column("last_name").recipe = ConstantRecipe(value="Lee")  # type: ignore[union-attr]
    emails = expand(schema, plan, seed=1)["Customers"]["email"].tolist()
    assert len(set(emails)) == 300 and all(len(e) <= 40 for e in emails)
    pattern = re.compile(r"^[a-z0-9.]+(-\d+)?@[a-z0-9.-]+\.[a-z]+$")
    assert all(pattern.match(e) for e in emails), [e for e in emails if not pattern.match(e)][:5]
