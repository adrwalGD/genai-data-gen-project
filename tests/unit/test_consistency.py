"""F2.7 — expression language, aggregate recipes, consistency heuristics and the consistency checker."""

import random
from datetime import date, datetime
from decimal import Decimal

import pytest

from genai_data_gen_project.generation import engine, heuristics
from genai_data_gen_project.generation.consistency import check_consistency
from genai_data_gen_project.generation.expander import expand
from genai_data_gen_project.generation.expressions import (
    ANCHOR_DATE,
    ExpressionError,
    NullResult,
    compile_expression,
    parse_parent_ref,
)
from genai_data_gen_project.generation.recipes import (
    AggregateRecipe,
    ColumnPlan,
    DateWindowRecipe,
    DerivedRecipe,
    validate_plan,
)
from genai_data_gen_project.generation.validator import validate
from genai_data_gen_project.schema.parser import parse_ddl


def parent(fk_col: str, col: str) -> object:
    return {
        ("menu_id", "price"): Decimal("12.50"),
        ("customer_id", "registration_date"): date(2024, 1, 1),
    }.get((fk_col, col))


def test_expression_language() -> None:
    rng = random.Random(0)
    c = compile_expression("quantity * parent(menu_id).price")
    assert (
        c.column_refs == ["quantity"]
        and c.parent_refs == [("menu_id", "price")]
        and c.dependencies == ["quantity", "menu_id"]
    )
    assert c.evaluate({"quantity": 3, "menu_id": 7}, parent, rng) == 37.5
    with pytest.raises(NullResult):
        c.evaluate({"quantity": None, "menu_id": 7}, parent, rng)
    status = compile_expression(
        "'Returned' if return_date is not None else ('Overdue' if due_date < TODAY else 'Checked Out')"
    )
    assert status.evaluate({"return_date": datetime(2024, 1, 1), "due_date": None}, parent, rng) == "Returned"
    assert status.evaluate({"return_date": None, "due_date": date(2020, 1, 1)}, parent, rng) == "Overdue"
    assert status.evaluate({"return_date": None, "due_date": ANCHOR_DATE}, parent, rng) == "Checked Out"
    assert compile_expression("due_date < TODAY").evaluate({"due_date": None}, parent, rng) is None
    assert compile_expression("coalesce(a, b, 5)").evaluate({"a": None, "b": None}, parent, rng) == 5
    assert (
        compile_expression("days_between(a, b)").evaluate(
            {"a": date(2024, 1, 1), "b": date(2024, 1, 31)}, parent, rng
        )
        == 30
    )
    r = compile_expression("randint(1, 3) + random()")
    assert r.uses_random and 1 <= r.evaluate({}, parent, rng) < 4
    assert (
        compile_expression("max(quantity - randint(0, quantity), 0)").evaluate({"quantity": 5}, parent, rng)
        >= 0
    )
    assert compile_expression("not (a == 1) and b >= 2").evaluate({"a": 2, "b": 2}, parent, rng) is True
    for bad in ("import os", "x.y", "f(1)", "[1, 2]", "quantity +", "parent(a, b).c", "lambda: 1"):
        with pytest.raises(ExpressionError):
            compile_expression(bad)
    assert parse_parent_ref("parent(customer_id).registration_date") == ("customer_id", "registration_date")
    assert parse_parent_ref("registration_date") is None


def test_restaurants_relations_hold(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    plan = heuristics.plan(schema, 300)
    assert validate_plan(plan, schema) == []
    subtotal = plan.table("Order_Items").column("subtotal").recipe  # type: ignore[union-attr]
    assert isinstance(subtotal, DerivedRecipe) and subtotal.expression == "quantity * parent(menu_id).price"
    total = plan.table("Orders").column("total_amount").recipe  # type: ignore[union-attr]
    assert isinstance(total, AggregateRecipe) and (total.child_table, total.child_column) == (
        "Order_Items",
        "subtotal",
    )
    order_date = plan.table("Orders").column("order_date").recipe  # type: ignore[union-attr]
    assert (
        isinstance(order_date, DateWindowRecipe | type(order_date))
        and order_date.after_column == "parent(customer_id).registration_date"
    )  # type: ignore[union-attr]
    assert (
        plan.table("Reviews").column("review_date").recipe.after_column
        == "parent(customer_id).registration_date"
    )  # type: ignore[union-attr]
    tables = expand(schema, plan, seed=4)
    assert validate(schema, tables).ok
    assert check_consistency(schema, plan, tables) == []
    menu_price = dict(zip(tables["Menu"]["menu_id"], tables["Menu"]["price"], strict=True))
    items = tables["Order_Items"]
    for qty, menu_id, sub in zip(items["quantity"], items["menu_id"], items["subtotal"], strict=True):
        assert sub == (Decimal(qty) * menu_price[menu_id]).quantize(Decimal("0.01"))
    sums: dict[int, Decimal] = {}
    for oid, sub in zip(items["order_id"], items["subtotal"], strict=True):
        sums[oid] = sums.get(oid, Decimal(0)) + sub
    for oid, total_amount in zip(tables["Orders"]["order_id"], tables["Orders"]["total_amount"], strict=True):
        assert total_amount == sums.get(oid, Decimal("0.00"))
    registration = dict(
        zip(tables["Customers"]["customer_id"], tables["Customers"]["registration_date"], strict=True)
    )
    for cid, od in zip(tables["Orders"]["customer_id"], tables["Orders"]["order_date"], strict=True):
        if od is not None and registration[cid] is not None:
            assert od >= registration[cid]
    drivers = tables["Delivery_Drivers"]
    assert all(t <= ANCHOR_DATE for t in drivers["termination_date"] if t is not None)
    assert all(
        j <= t
        for j, t in zip(drivers["join_date"], drivers["termination_date"], strict=True)
        if t is not None and j is not None
    )


def test_library_loan_status_follows_dates(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["library"])
    plan = heuristics.plan(schema, 300)
    status = plan.table("Book_Loans").column("loan_status").recipe  # type: ignore[union-attr]
    assert isinstance(status, DerivedRecipe) and status.expression.startswith(
        "'Returned' if return_date is not None"
    )
    assert plan.table("Book_Loans").column("loan_status").null_ratio == 0.0  # type: ignore[union-attr]
    loans = expand(schema, plan, seed=2)["Book_Loans"]
    assert loans["loan_status"].isna().sum() == 0
    for st, ret, due in zip(loans["loan_status"], loans["return_date"], loans["due_date"], strict=True):
        if st is None:
            continue
        if ret is not None:
            assert st == "Returned"
        elif due < ANCHOR_DATE:
            assert st == "Overdue"
        else:
            assert st == "Checked Out"
    statuses = set(loans["loan_status"].dropna())
    assert {"Returned", "Overdue"} <= statuses <= {"Returned", "Overdue", "Checked Out"}
    assert plan.table("Book_Loans").column("loan_date").recipe.after_column == "parent(member_id).join_date"  # type: ignore[union-attr]


def test_consistency_checker_flags_broken_relations(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    plan = heuristics.plan(schema, 60)
    tables = expand(schema, plan, seed=1)
    tables["Order_Items"].at[0, "subtotal"] = Decimal("999999.99")
    tables["Orders"].at[3, "total_amount"] = Decimal("0.01")
    first_order_customer = tables["Orders"].at[5, "customer_id"]
    tables["Customers"].loc[
        tables["Customers"]["customer_id"] == first_order_customer, "registration_date"
    ] = datetime(2030, 1, 1)
    issues = check_consistency(schema, plan, tables)
    rules = {(i.table, i.column): i for i in issues}
    assert set(rules) >= {("Order_Items", "subtotal"), ("Orders", "total_amount"), ("Orders", "order_date")}
    assert rules[("Order_Items", "subtotal")].message == "does not equal quantity * parent(menu_id).price"
    assert rules[("Order_Items", "subtotal")].examples[0].startswith("Decimal('999999.99') !=")
    assert "sum(Order_Items.subtotal)" in rules[("Orders", "total_amount")].message
    assert rules[("Orders", "order_date")].message == "must follow parent(customer_id).registration_date"
    assert all(i.rule == "consistency" for i in issues)


def test_validate_plan_rejects_bad_relations(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    plan = heuristics.plan(schema, 10)
    orders = plan.table("Orders")
    assert orders is not None
    orders.column("total_amount").recipe = AggregateRecipe(
        child_table="Menu", child_fk_column="restaurant_id", child_column="price"
    )  # type: ignore[union-attr]
    orders.column("delivery_address").recipe = DerivedRecipe(expression="parent(customer_id).nope")  # type: ignore[union-attr]
    orders.column("order_date").recipe = DateWindowRecipe(
        start=date(2024, 1, 1), end=date(2025, 1, 1), after_column="parent(customer_id).first_name"
    )  # type: ignore[union-attr]
    orders.columns.append(ColumnPlan(column="order_status", recipe=DerivedRecipe(expression="import os")))
    problems = "\n".join(validate_plan(plan, schema))
    assert "Menu.restaurant_id is not a foreign key to Orders" in problems
    assert "Customers has no column 'nope'" in problems
    assert "has no date/time column 'first_name'" in problems
    assert "invalid expression" in problems


def test_engine_report_includes_consistency(sample_ddl: dict[str, str]) -> None:
    request = engine.GenerationRequest(ddl=sample_ddl["restaurants"], rows_per_table=50, seed=9)
    dataset = engine.generate(request, None, engine.get_settings.__wrapped__())  # fresh Settings
    assert dataset.report_ok is True
    report = engine.report_of(dataset)
    assert report is not None and not [i for i in report.issues if i.rule == "consistency"]
