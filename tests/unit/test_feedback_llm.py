"""F4.2 — draft → EditPlan conversion, retry on invalid drafts, pool filling, apply_feedback (FakeLLM)."""

import pytest

from genai_data_gen_project.generation import engine, feedback
from genai_data_gen_project.generation.recipes import DecimalRangeRecipe, FakerRecipe, PatternRecipe
from genai_data_gen_project.storage.dataset import Dataset
from tests.fakes import FakeLLM


@pytest.fixture
def restaurants(sample_ddl: dict[str, str]) -> Dataset:
    request = engine.GenerationRequest(ddl=sample_ddl["restaurants"], rows_per_table=40, seed=21)
    return engine.generate(request, None, engine.get_settings.__wrapped__())


def test_draft_conversion_covers_every_op(restaurants: Dataset) -> None:
    draft = feedback.EditPlanDraft.model_validate(
        {
            "table": "customers",
            "summary": "several edits",
            "ops": [
                {"op": "set_values", "column": "city", "where": "state == 'CA'", "value": "Kraków"},
                {"op": "set_values", "column": "phone_number", "kind": "pattern",
                 "template": "+48 ### ### ###"},
                {"op": "regenerate_column", "column": "email", "kind": "pattern",
                 "template": "{col:first_name|slug}.{col:last_name|slug}@example.org", "unique": True},
                {"op": "regenerate_column", "column": "first_name", "kind": "faker", "provider": "first_name",
                 "locale": "pl_PL"},
                {"op": "add_rows", "count": 5, "overrides": [{"column": "state", "value": "CA"}]},
                {"op": "delete_rows", "where": "registration_date is None"},
                {"op": "update_pool", "column": "address", "brief": "Polish street addresses"},
            ],
        }
    )  # fmt: skip
    plan = feedback.draft_to_plan(draft, restaurants)
    assert plan.table == "Customers" and plan.summary == "several edits"
    kinds = [type(op).__name__ for op in plan.ops]
    assert kinds == ["SetValuesOp", "SetValuesOp", "RegenerateColumnOp", "RegenerateColumnOp", "AddRowsOp",
                     "DeleteRowsOp", "UpdatePoolOp"]  # fmt: skip
    assert plan.ops[0].value == "Kraków" and plan.ops[0].where == "state == 'CA'"  # type: ignore[union-attr]
    assert isinstance(plan.ops[1].recipe, PatternRecipe)  # type: ignore[union-attr]
    assert isinstance(plan.ops[2].recipe, PatternRecipe) and plan.ops[2].recipe.unique  # type: ignore[union-attr]
    assert isinstance(plan.ops[3].recipe, FakerRecipe) and plan.ops[3].recipe.locale == "pl_PL"  # type: ignore[union-attr]
    assert plan.ops[4].count == 5 and plan.ops[4].overrides == {"state": "CA"}  # type: ignore[union-attr]
    assert plan.ops[5].cascade is True  # type: ignore[union-attr]
    assert plan.ops[6].brief == "Polish street addresses" and plan.ops[6].values == []  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("draft", "message"),
    [
        ({"table": "Nope", "ops": [{"op": "add_rows", "count": 1}]}, "unknown table 'Nope'"),
        ({"table": "Customers", "ops": []}, "no operations"),
        ({"table": "Customers", "ops": [{"op": "set_values", "column": "ghost", "value": "1"}]},
         "unknown column 'ghost'"),
        ({"table": "Customers", "ops": [{"op": "regenerate_column", "column": "city"}]},
         "recipe kind is required"),
        ({"table": "Customers", "ops": [{"op": "delete_rows"}]}, "needs a `where` filter"),
        ({"table": "Customers",
          "ops": [{"op": "set_values", "column": "city", "kind": "int_range", "min": 1}]},
         "invalid recipe parameters"),
    ],
)  # fmt: skip
def test_invalid_drafts_raise_edit_errors(restaurants: Dataset, draft: dict, message: str) -> None:  # type: ignore[type-arg]
    with pytest.raises(feedback.EditError, match=message):
        feedback.draft_to_plan(feedback.EditPlanDraft.model_validate(draft), restaurants)


def test_plan_edit_retries_once_with_the_error(restaurants: Dataset) -> None:
    def respond(prompt: str) -> dict:  # type: ignore[type-arg]
        if "Your previous plan was invalid" not in prompt:
            return {"table": "Reviews", "ops": [{"op": "set_values", "column": "review_id", "value": "1"}]}
        assert "primary key" in prompt
        return {"table": "Reviews", "summary": "floor ratings", "ops": [
            {"op": "set_values", "column": "rating", "where": "rating < 3", "value": "3"}]}  # fmt: skip

    fake = FakeLLM(structured={"EditPlanDraft": respond})
    plan = feedback.plan_edit(restaurants, "Reviews", "set all ratings below 3 to 3", fake)
    assert (
        len(fake.calls) == 2
        and fake.calls[0]["model"] == "EditPlanDraft"
        and fake.calls[0]["temperature"] == 0.2
    )
    assert "TABLE (40 rows):\nTABLE Reviews" in fake.calls[0]["contents"]
    assert (
        "SAMPLE ROWS:" in fake.calls[0]["contents"]
        and "USER FEEDBACK:\nset all ratings below 3 to 3" in fake.calls[0]["contents"]
    )
    assert isinstance(plan.ops[0], feedback.SetValuesOp) and plan.ops[0].value == "3"
    always_bad = FakeLLM(structured={"EditPlanDraft": {"table": "Reviews", "ops": [{"op": "delete_rows"}]}})
    with pytest.raises(feedback.EditError, match="invalid edit plan twice"):
        feedback.plan_edit(restaurants, "Reviews", "delete stuff", always_bad)


def test_update_pool_values_are_fetched_from_the_llm(restaurants: Dataset) -> None:
    counter = {"n": 0}

    def pool(prompt: str) -> dict:  # type: ignore[type-arg]
        import re

        n = int(re.search(r"Generate (\d+) distinct values", prompt).group(1))  # type: ignore[union-attr]
        values = [f"Bar Mleczny {counter['n'] + i}" for i in range(n)]
        counter["n"] += n
        return {"values": values}

    fake = FakeLLM(
        structured={
            "EditPlanDraft": {
                "table": "Restaurants",
                "summary": "Polish names",
                "ops": [{"op": "update_pool", "column": "name", "brief": "Polish milk bar names"}],
            }
        },
        json_responses={"TextPool": pool},
    )
    new, result, plan = feedback.apply_feedback(
        restaurants, "Restaurants", "give restaurants Polish names", fake
    )
    assert isinstance(plan.ops[0], feedback.UpdatePoolOp) and len(plan.ops[0].values) == 40
    assert result.report.ok and all(n.startswith("Bar Mleczny") for n in new.tables["Restaurants"]["name"])
    assert len(set(new.tables["Restaurants"]["name"])) == 40  # heuristic pool for restaurant names is unique
    assert new.params["edits"][-1]["summary"] == "Polish names"


def test_apply_feedback_with_recipe_edit(restaurants: Dataset) -> None:
    draft = {
        "table": "Menu",
        "summary": "cheaper menu",
        "ops": [{"op": "set_values", "column": "price", "kind": "decimal_range", "min": 5, "max": 9.99}],
    }
    fake = FakeLLM(structured={"EditPlanDraft": draft})
    new, result, plan = feedback.apply_feedback(
        restaurants, "Menu", "prices between 5 and 9.99", fake, seed=2
    )
    assert isinstance(plan.ops[0].recipe, DecimalRangeRecipe)  # type: ignore[union-attr]
    assert result.report.ok and all(5 <= float(p) <= 9.99 for p in new.tables["Menu"]["price"])


def test_conditional_feedback_without_filter_triggers_retry(restaurants: Dataset) -> None:
    def respond(prompt: str) -> dict:  # type: ignore[type-arg]
        if "Your previous plan was invalid" not in prompt:
            return {"table": "Reviews", "ops": [{"op": "set_values", "column": "rating", "value": "3"}]}
        assert "no `where` filter" in prompt
        return {
            "table": "Reviews",
            "ops": [{"op": "set_values", "column": "rating", "where": "rating < 3", "value": "3"}],
        }

    fake = FakeLLM(structured={"EditPlanDraft": respond})
    plan = feedback.plan_edit(restaurants, "Reviews", "set all ratings below 3 to 3", fake)
    assert plan.ops[0].where == "rating < 3" and len(fake.calls) == 2  # type: ignore[union-attr]
    # unconditional feedback is accepted without a filter
    plain = FakeLLM(
        structured={
            "EditPlanDraft": {
                "table": "Reviews",
                "ops": [{"op": "set_values", "column": "rating", "value": "5"}],
            }
        }
    )
    assert feedback.plan_edit(restaurants, "Reviews", "make every rating 5", plain).ops[0].where is None  # type: ignore[union-attr]


def test_sloppy_filter_types_do_not_crash(restaurants: Dataset) -> None:
    plan = feedback.EditPlan(
        table="Reviews", ops=[feedback.SetValuesOp(column="rating", where="rating < 'x'", value=4)]
    )
    _new, result = feedback.apply(plan, restaurants)
    assert result.affected_rows == 0 and result.report.ok
