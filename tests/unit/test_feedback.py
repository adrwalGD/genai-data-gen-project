"""F4.1 — EditPlan applier: filtered edits, regeneration, add/delete + cascade, pools, errors."""

from decimal import Decimal

import pytest

from genai_data_gen_project.generation import engine, feedback
from genai_data_gen_project.generation.recipes import DecimalRangeRecipe, PatternRecipe
from genai_data_gen_project.storage.dataset import Dataset


@pytest.fixture
def restaurants(sample_ddl: dict[str, str]) -> Dataset:
    settings = engine.get_settings.__wrapped__()
    request = engine.GenerationRequest(ddl=sample_ddl["restaurants"], rows_per_table=60, seed=11)
    return engine.generate(request, None, settings)


def test_set_values_with_filter_and_constant(restaurants: Dataset) -> None:
    plan = feedback.EditPlan(
        table="Reviews",
        ops=[feedback.SetValuesOp(column="rating", where="rating < 3", value=3)],
        summary="floor ratings at 3",
    )
    before = restaurants.tables["Reviews"]["rating"].tolist()
    new, result = feedback.apply(plan, restaurants)
    after = new.tables["Reviews"]["rating"].tolist()
    assert result.report.ok and result.affected_rows == sum(1 for r in before if r < 3) > 0
    assert min(after) >= 3 and all(a == b for a, b in zip(after, before, strict=True) if b >= 3)
    assert restaurants.tables["Reviews"]["rating"].tolist() == before  # original untouched
    assert new.params["edits"][0]["summary"] == "floor ratings at 3" and new.params["edits"][0]["ok"] is True
    assert result.rows_before == result.rows_after == 60


def test_regenerate_column_with_recipe_keeps_uniqueness(restaurants: Dataset) -> None:
    plan = feedback.EditPlan(
        table="Customers",
        ops=[
            feedback.RegenerateColumnOp(
                column="email",
                recipe=PatternRecipe(
                    template="{col:first_name|slug}.{col:last_name|slug}@example.org", unique=True
                ),
            )
        ],
    )
    new, result = feedback.apply(plan, restaurants)
    emails = new.tables["Customers"]["email"].tolist()
    assert result.report.ok and result.affected_rows == 60
    assert all(e.endswith("@example.org") for e in emails) and len(set(emails)) == 60
    first = new.tables["Customers"]["first_name"].tolist()
    assert all(e.startswith(f.lower().replace(" ", "")[:3]) for e, f in zip(emails, first, strict=True))


def test_set_values_with_recipe_and_parent_filter(restaurants: Dataset) -> None:
    plan = feedback.EditPlan(
        table="Menu",
        ops=[
            feedback.SetValuesOp(
                column="price",
                where="parent(restaurant_id).cuisine_type == 'Italian'",
                recipe=DecimalRangeRecipe(min=30, max=40),
            )
        ],
    )
    new, result = feedback.apply(plan, restaurants)
    cuisine = dict(
        zip(
            new.tables["Restaurants"]["restaurant_id"], new.tables["Restaurants"]["cuisine_type"], strict=True
        )
    )
    italian = [
        p
        for p, rid in zip(new.tables["Menu"]["price"], new.tables["Menu"]["restaurant_id"], strict=True)
        if cuisine[rid] == "Italian"
    ]
    assert result.report.ok and result.affected_rows == len(italian) > 0
    assert all(Decimal(30) <= p <= Decimal(40) for p in italian)
    # subtotal = quantity * price is declared -> the consistency check must still hold after the price change
    assert not [i for i in result.report.issues if i.rule == "consistency"]
    price = dict(zip(new.tables["Menu"]["menu_id"], new.tables["Menu"]["price"], strict=True))
    items = new.tables["Order_Items"]
    for qty, mid, sub in zip(items["quantity"], items["menu_id"], items["subtotal"], strict=True):
        assert sub == (Decimal(qty) * price[mid]).quantize(Decimal("0.01"))  # subtotals followed the price


def test_add_rows_continues_pks_and_refreshes_aggregates(restaurants: Dataset) -> None:
    plan = feedback.EditPlan(
        table="Orders",
        ops=[feedback.AddRowsOp(count=15, overrides={"order_status": "Cancelled", "delivery_address": None})],
    )
    new, result = feedback.apply(plan, restaurants)
    orders = new.tables["Orders"]
    assert result.rows_after == 75 and result.affected_rows == 15 and result.report.ok, (
        result.report.summary()
    )
    assert orders["order_id"].tolist() == list(range(1, 76))
    tail = orders.tail(15)
    assert set(tail["order_status"]) == {"Cancelled"} and tail["delivery_address"].isna().all()
    assert set(tail["customer_id"]) <= set(new.tables["Customers"]["customer_id"])
    assert all(t == Decimal("0.00") for t in tail["total_amount"])  # no items yet → aggregate default


def test_delete_rows_cascades_and_nulls(restaurants: Dataset) -> None:
    victims = restaurants.tables["Restaurants"][restaurants.tables["Restaurants"]["cuisine_type"] == "Other"]
    assert len(victims) > 0
    victim_ids = set(victims["restaurant_id"])
    plan = feedback.EditPlan(
        table="Restaurants", ops=[feedback.DeleteRowsOp(where="cuisine_type == 'Other'")]
    )
    new, result = feedback.apply(plan, restaurants)
    assert result.report.ok, result.report.summary()
    assert result.affected_rows == len(victims) and result.rows_after == 60 - len(victims)
    for child in ("Menu", "Orders", "Reviews"):
        assert not set(new.tables[child]["restaurant_id"]) & victim_ids
        expected = sum(rid in victim_ids for rid in restaurants.tables[child]["restaurant_id"])
        assert result.cascaded.get(child, 0) == expected, (child, result.cascaded)
    deleted_orders = set(restaurants.tables["Orders"]["order_id"]) - set(new.tables["Orders"]["order_id"])
    deleted_menu = set(restaurants.tables["Menu"]["menu_id"]) - set(new.tables["Menu"]["menu_id"])
    items = restaurants.tables["Order_Items"]
    expected_items = sum(
        oid in deleted_orders or mid in deleted_menu
        for oid, mid in zip(items["order_id"], items["menu_id"], strict=True)
    )
    assert (
        result.cascaded.get("Order_Items", 0) == expected_items
    )  # grandchildren of deleted orders/menu items
    assert not set(new.tables["Order_Items"]["order_id"]) - set(new.tables["Orders"]["order_id"])
    assert not set(new.tables["Order_Items"]["menu_id"]) - set(new.tables["Menu"]["menu_id"])
    strict = feedback.EditPlan(
        table="Restaurants", ops=[feedback.DeleteRowsOp(where="cuisine_type == 'Italian'", cascade=False)]
    )
    with pytest.raises(feedback.EditError, match="enable cascade"):
        feedback.apply(strict, restaurants)


def test_nullable_fk_children_are_set_null(sample_ddl: dict[str, str]) -> None:
    settings = engine.get_settings.__wrapped__()
    library = engine.generate(
        engine.GenerationRequest(ddl=sample_ddl["library"], rows_per_table=40, seed=3), None, settings
    )
    managers = set(library.tables["Library_Branches"]["manager_id"].dropna())
    assert managers
    victim = sorted(managers)[0]
    plan = feedback.EditPlan(table="Employees", ops=[feedback.DeleteRowsOp(where=f"employee_id == {victim}")])
    new, result = feedback.apply(plan, library)
    assert result.report.ok, result.report.summary()
    assert victim not in set(new.tables["Library_Branches"]["manager_id"].dropna())
    assert result.cascaded.get("Library_Branches", 0) >= 1


def test_update_pool_uses_provided_values(restaurants: Dataset) -> None:
    values = [f"Trattoria {i}" for i in range(60)]
    plan = feedback.EditPlan(
        table="Restaurants",
        ops=[feedback.UpdatePoolOp(column="name", brief="Italian trattoria names", values=values)],
    )
    new, result = feedback.apply(plan, restaurants)
    assert result.report.ok and set(new.tables["Restaurants"]["name"]) == set(values)


def _plan(table: str, op: feedback.EditOp) -> feedback.EditPlan:  # type: ignore[valid-type]
    return feedback.EditPlan(table=table, ops=[op])


INVALID_PLANS = [
    (_plan("Nope", feedback.AddRowsOp(count=1)), "unknown table 'Nope'"),
    (_plan("Reviews", feedback.SetValuesOp(column="review_id", value=1)), "primary key"),
    (_plan("Reviews", feedback.SetValuesOp(column="ghost", value=1)), "no column 'ghost'"),
    (_plan("Reviews", feedback.SetValuesOp(column="rating", where="rating <", value=1)), "invalid filter"),
    (
        _plan("Reviews", feedback.SetValuesOp(column="rating", where="nope > 1", value=1)),
        "unknown column 'nope'",
    ),
    (_plan("Reviews", feedback.SetValuesOp(column="rating", value=None)), "NOT NULL"),
    (_plan("Orders", feedback.SetValuesOp(column="order_status", value="Teleported")), "not one of"),
    (_plan("Orders", feedback.AddRowsOp(count=1, overrides={"order_id": 5})), "assigned automatically"),
    (_plan("Restaurants", feedback.UpdatePoolOp(column="name", brief="x")), "has no values"),
]


@pytest.mark.parametrize(("plan", "message"), INVALID_PLANS)
def test_invalid_plans_are_rejected_before_changing_anything(
    restaurants: Dataset, plan: feedback.EditPlan, message: str
) -> None:
    before = {name: frame.copy() for name, frame in restaurants.tables.items()}
    with pytest.raises(feedback.EditError, match=message):
        feedback.apply(plan, restaurants)
    for name, frame in before.items():
        assert frame.equals(restaurants.tables[name])


def test_edit_plan_json_schema_is_llm_friendly() -> None:
    schema = feedback.EditPlan.model_json_schema()
    assert {"SetValuesOp", "RegenerateColumnOp", "AddRowsOp", "DeleteRowsOp", "UpdatePoolOp"} <= set(
        schema["$defs"]
    )
    plan = feedback.EditPlan.model_validate(
        {
            "table": "Reviews",
            "ops": [{"op": "set_values", "column": "rating", "where": "rating < 3", "value": 3}],
        }
    )
    assert isinstance(plan.ops[0], feedback.SetValuesOp)
