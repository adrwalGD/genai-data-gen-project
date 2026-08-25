"""F2.3 — validator: clean on expander output, exactly one issue per injected violation class."""

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from genai_data_gen_project.generation import heuristics
from genai_data_gen_project.generation.expander import expand
from genai_data_gen_project.generation.validator import validate
from genai_data_gen_project.schema.parser import parse_ddl


@pytest.mark.parametrize("name", ["library", "restaurants", "company"])
def test_expander_output_validates_clean(name: str, sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl[name])
    tables = expand(schema, heuristics.plan(schema, 120), seed=11)
    report = validate(schema, tables, expected_rows=dict.fromkeys(schema.table_names, 120))
    assert report.ok, report.summary()
    assert report.issues == [] and sum(report.row_counts.values()) == 120 * len(schema.tables)
    assert report.summary().startswith("OK")
    assert report.notes == []


@pytest.fixture
def restaurants(sample_ddl: dict[str, str]):  # type: ignore[no-untyped-def]
    schema = parse_ddl(sample_ddl["restaurants"])
    tables = expand(schema, heuristics.plan(schema, 40), seed=2)
    return schema, tables


def set_cell(frame: pd.DataFrame, column: str, index: int, value: object) -> None:
    frame.at[index, column] = value


@pytest.mark.parametrize(
    ("table", "column", "value", "rule"),
    [
        ("Reviews", "rating", 9, "check"),
        ("Reviews", "rating", None, "not_null"),
        ("Reviews", "rating", "five", "type"),
        ("Restaurants", "cuisine_type", "Martian", "enum"),
        ("Restaurants", "zip_code", "12345-67890-extra", "varchar_length"),
        ("Menu", "price", Decimal("1.234"), "decimal_scale"),
        ("Menu", "price", Decimal("123456789.00"), "decimal_precision"),
        ("Orders", "customer_id", 99999, "fk_dangling"),
        ("Delivery_Drivers", "join_date", "not-a-date", "date_invalid"),
        ("Menu", "available", "yes", "type"),
    ],
)
def test_single_violations_are_reported_once(
    restaurants, table: str, column: str, value: object, rule: str
) -> None:  # type: ignore[no-untyped-def]
    schema, tables = restaurants
    set_cell(tables[table], column, 3, value)
    report = validate(schema, tables)
    assert not report.ok
    assert [(i.table, i.column, i.rule, i.count) for i in report.issues] == [(table, column, rule, 1)], (
        report.summary()
    )
    if value is not None:
        assert report.issues[0].examples == [repr(value)[:60]]
    assert str(report.issues[0]).startswith(f"[{rule}] {table}.{column}")


def test_duplicate_pk_and_unique(restaurants) -> None:  # type: ignore[no-untyped-def]
    schema, tables = restaurants
    customers = tables["Customers"]
    set_cell(customers, "customer_id", 5, customers.at[0, "customer_id"])
    set_cell(customers, "email", 7, customers.at[1, "email"])
    report = validate(schema, tables)
    rules = sorted((i.rule, i.column) for i in report.by_table("Customers"))
    assert rules == [("pk_duplicate", "customer_id"), ("unique", "email")]
    # overwriting id 6 with a duplicate removes it from the parent key set → children of 6 dangle, only
    others = [i for i in report.issues if i.table != "Customers"]
    assert all(i.rule == "fk_dangling" and i.examples == ["6"] for i in others), others


def test_structural_problems(restaurants) -> None:  # type: ignore[no-untyped-def]
    schema, tables = restaurants
    tables["Menu"] = tables["Menu"].drop(columns=["price"])
    del tables["Reviews"]
    report = validate(schema, tables, expected_rows={"Orders": 41})
    rules = {(i.table, i.rule) for i in report.issues}
    assert ("Menu", "columns_mismatch") in rules
    assert ("Reviews", "missing_table") in rules
    assert ("Orders", "row_count") in rules
    assert "Reviews" not in report.row_counts


def test_multi_column_checks_and_unsupported_ones() -> None:
    schema = parse_ddl(
        "CREATE TABLE p (id INT PRIMARY KEY, start_date DATE NOT NULL, end_date DATE,"
        " name VARCHAR(20) NOT NULL, CHECK (end_date >= start_date), CHECK (LENGTH(name) > 2));"
    )
    frame = pd.DataFrame(
        {
            "id": pd.Series([1, 2, 3], dtype=object),
            "start_date": pd.Series([date(2024, 1, 1)] * 3, dtype=object),
            "end_date": pd.Series([date(2024, 2, 1), None, date(2023, 12, 31)], dtype=object),
            "name": pd.Series(["Alpha", "Beta", "Gamma"], dtype=object),
        }
    )
    report = validate(schema, {"p": frame})
    assert [(i.rule, i.count) for i in report.issues] == [("check", 1)]
    assert report.issues[0].examples == [repr((date(2023, 12, 31), date(2024, 1, 1)))[:60]]
    assert any("LENGTH(name) > 2" in n and "unsupported" in n for n in report.notes)


def test_summary_lists_issues(restaurants) -> None:  # type: ignore[no-untyped-def]
    schema, tables = restaurants
    set_cell(tables["Reviews"], "rating", 0, 42)
    summary = validate(schema, tables).summary()
    assert summary.startswith("1 issue(s) across 1 table(s): [check] Reviews.rating")
