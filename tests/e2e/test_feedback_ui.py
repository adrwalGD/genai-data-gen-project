"""F5.3 — per-table feedback: Submit applies a (fake) Gemini EditPlan, refreshes preview, keeps history."""

import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from genai_data_gen_project.ui import state
from tests.fakes import FakeLLM

APP = Path(__file__).resolve().parents[2] / "src" / "genai_data_gen_project" / "ui" / "app.py"


def fake_llm() -> FakeLLM:
    counter = {"n": 0}

    def pool(prompt: str) -> dict:  # type: ignore[type-arg]
        n = int(re.search(r"Generate (\d+) distinct values", prompt).group(1))  # type: ignore[union-attr]
        values = [f"Fake Value {counter['n'] + i}" for i in range(n)]
        counter["n"] += n
        return {"values": values}

    def draft(prompt: str) -> dict:  # type: ignore[type-arg]
        if "add 10 cancelled orders" in prompt:
            add = {
                "op": "add_rows",
                "count": 10,
                "overrides": [{"column": "order_status", "value": "Cancelled"}],
            }
            return {"table": "Orders", "summary": "ten cancelled orders", "ops": [add]}
        floor = {"op": "set_values", "column": "rating", "where": "rating < 3", "value": "3"}
        return {"table": "Reviews", "summary": "floor ratings at 3", "ops": [floor]}

    return FakeLLM(
        structured={
            "PlannerOutput": {"table_rows": [], "overrides": [], "notes": ["fake planner"]},
            "EditPlanDraft": draft,
        },
        json_responses={"TextPool": pool},
    )


@pytest.fixture
def at(monkeypatch, tmp_path: Path) -> AppTest:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=120)
    app.session_state[state.SS_LLM_BACKEND] = fake_llm()
    app.run()
    app.radio(key="schema_source").set_value("Sample schema").run()
    app.selectbox(key="sample_schema").select("restaurants").run()
    app.number_input(key=state.SS_ROWS_PER_TABLE).set_value(30).run()
    app.button(key="generate").click().run()
    assert not app.exception, [str(e.value) for e in app.exception]
    return app


def test_feedback_applies_to_the_selected_table_and_keeps_history(at: AppTest) -> None:
    dataset = at.session_state[state.SS_DATASET]
    assert dataset.params["llm"] is True and dataset.params["model"] == "fake-gemini"
    before = dataset.tables["Reviews"]["rating"].tolist()
    at.selectbox(key="preview_table").select("Reviews").run()
    at.text_input(key="feedback_text").input("set all ratings below 3 to 3").run()
    at.button(key="feedback_submit").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    after = at.session_state[state.SS_DATASET].tables["Reviews"]["rating"].tolist()
    assert min(after) >= 3 and all(a == b for a, b in zip(after, before, strict=True) if b >= 3)
    log = at.session_state[state.SS_EDIT_LOG]
    assert len(log) == 1 and log[0]["table"] == "Reviews" and log[0]["ok"] is True
    assert log[0]["affected_rows"] == sum(1 for r in before if r < 3)
    assert any("set all ratings below 3 to 3" in e.label for e in at.expander)
    at.selectbox(key="preview_table").select("Orders").run()
    at.text_input(key="feedback_text").input("add 10 cancelled orders").run()
    at.button(key="feedback_submit").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    orders = at.session_state[state.SS_DATASET].tables["Orders"]
    assert len(orders) == 40 and set(orders.tail(10)["order_status"]) == {"Cancelled"}
    assert len(at.session_state[state.SS_EDIT_LOG]) == 2 and at.metric[0].value == "40"


def test_empty_feedback_is_rejected_with_a_hint(at: AppTest) -> None:
    at.button(key="feedback_submit").click().run()
    assert any("before clicking Submit" in e.value for e in at.error)
    assert at.session_state[state.SS_EDIT_LOG] == []
