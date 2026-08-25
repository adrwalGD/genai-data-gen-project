"""F4.2 — real Gemini turns three styles of textual feedback into valid, applied EditPlans."""

import re

import pytest

from genai_data_gen_project.config import Settings
from genai_data_gen_project.generation import engine, feedback
from genai_data_gen_project.llm.client import GeminiClient
from genai_data_gen_project.storage.dataset import Dataset
from tests.conftest import SAMPLE_DDLS

pytestmark = pytest.mark.llm


@pytest.fixture(scope="module")
def restaurants() -> Dataset:
    ddl = SAMPLE_DDLS["restaurants"].read_text(encoding="utf-8")
    request = engine.GenerationRequest(ddl=ddl, rows_per_table=60, seed=8)
    return engine.generate(request, None, Settings())


@pytest.fixture(scope="module")
def gemini() -> GeminiClient:
    return GeminiClient(Settings())


def test_floor_ratings(restaurants: Dataset, gemini: GeminiClient) -> None:
    new, result, plan = feedback.apply_feedback(
        restaurants, "Reviews", "set all ratings below 3 to 3", gemini
    )
    assert result.report.ok, result.report.summary()
    ratings = new.tables["Reviews"]["rating"].tolist()
    before = restaurants.tables["Reviews"]["rating"].tolist()
    assert min(ratings) >= 3 and max(ratings) == 5, plan
    assert all(a == b for a, b in zip(ratings, before, strict=True) if b >= 3), plan
    assert any(isinstance(op, feedback.SetValuesOp) for op in plan.ops), plan


def test_regenerate_emails(restaurants: Dataset, gemini: GeminiClient) -> None:
    new, result, _plan = feedback.apply_feedback(
        restaurants, "Customers", "regenerate emails as firstname.lastname@example.org (lowercase)", gemini
    )
    assert result.report.ok, result.report.summary()
    emails = new.tables["Customers"]["email"].tolist()
    assert all(re.fullmatch(r"[a-z0-9.\-]+@example\.org", e) for e in emails), emails[:5]
    assert len(set(emails)) == len(emails)


def test_add_cancelled_orders(restaurants: Dataset, gemini: GeminiClient) -> None:
    new, result, _plan = feedback.apply_feedback(restaurants, "Orders", "add 15 cancelled orders", gemini)
    assert result.report.ok, result.report.summary()
    assert result.rows_after == result.rows_before + 15
    added = new.tables["Orders"].tail(15)
    assert set(added["order_status"]) == {"Cancelled"}
    assert set(added["customer_id"]) <= set(new.tables["Customers"]["customer_id"])


def test_cross_table_condition(restaurants: Dataset, gemini: GeminiClient) -> None:
    new, result, plan = feedback.apply_feedback(
        restaurants, "Menu", "make all Italian restaurants' dishes cost between 30 and 40", gemini
    )
    assert result.report.ok, result.report.summary()
    cuisine = dict(
        zip(
            new.tables["Restaurants"]["restaurant_id"], new.tables["Restaurants"]["cuisine_type"], strict=True
        )
    )
    old_price = dict(
        zip(restaurants.tables["Menu"]["menu_id"], restaurants.tables["Menu"]["price"], strict=True)
    )
    changed = 0
    menu = new.tables["Menu"]
    for mid, rid, price in zip(menu["menu_id"], menu["restaurant_id"], menu["price"], strict=True):
        if cuisine[rid] == "Italian":
            assert 30 <= float(price) <= 40, plan
            changed += int(price != old_price[mid])
        else:
            assert price == old_price[mid], plan
    assert changed > 0, plan
