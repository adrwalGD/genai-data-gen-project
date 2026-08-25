"""F6.4 — Talk-to-data page with a fake backend: table + chart for a question, history replay, clear."""

from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from genai_data_gen_project.generation import engine
from genai_data_gen_project.llm.client import ToolTurn
from genai_data_gen_project.storage import datasets, postgres
from genai_data_gen_project.ui import state
from tests.conftest import SAMPLE_DDLS
from tests.fakes import FakeLLM, tool_call

APP = Path(__file__).resolve().parents[2] / "src" / "genai_data_gen_project" / "ui" / "app.py"


def fake_executor(sql_text: str) -> postgres.QueryResult:
    if "count" in sql_text.lower():
        return postgres.QueryResult(
            columns=["n"], rows=[[60]], row_count=1, truncated=False, elapsed_ms=2, sql=sql_text
        )
    rows = [["Kraków", Decimal("120.50")], ["Gdańsk", Decimal("80.00")]]
    return postgres.QueryResult(
        columns=["city", "revenue"], rows=rows, row_count=2, truncated=False, elapsed_ms=2, sql=sql_text
    )


class ScriptedLLM(FakeLLM):
    """Tool turns keyed by the question (FakeLLM's single queue would be drained by the first question)."""

    def __init__(self) -> None:
        super().__init__(stream_texts=["There are 60 restaurants.", "Kraków leads with 120.50."])
        self.script = {
            "How many restaurants": [tool_call("run_sql", sql="SELECT count(*) AS n FROM restaurants")],
            "Plot revenue": [
                tool_call(
                    "run_sql",
                    sql="SELECT city, sum(total_amount) AS revenue FROM orders JOIN customers "
                    "USING (customer_id) GROUP BY city",
                ),
                tool_call("render_chart", chart_type="bar", x="city", y="revenue", title="Revenue by city"),
            ],
        }
        self.progress: dict[str, int] = {}

    def generate_with_tools(self, contents, tools, *, system=None, temperature=0.0):  # type: ignore[no-untyped-def,override]
        question = ""
        for content in reversed(contents):
            if getattr(content, "role", None) == "user":
                texts = [p.text for p in content.parts if getattr(p, "text", None)]
                if texts:
                    question = texts[0]
                    break
        key = next((k for k in self.script if k in question), "")
        turns = self.script.get(key, [])
        index = self.progress.get(key, 0)
        if index < len(turns):
            self.progress[key] = index + 1
            return turns[index]
        return ToolTurn(text="", calls=[], content=None)


@pytest.fixture
def at(monkeypatch, tmp_path: Path) -> AppTest:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:x@localhost:1/x")  # offline: executor is injected
    cfg = engine.get_settings.__wrapped__()
    ddl = SAMPLE_DDLS["restaurants"].read_text(encoding="utf-8")
    dataset = engine.generate(
        engine.GenerationRequest(ddl=ddl, rows_per_table=10, seed=1, name="talk demo"), None, cfg
    )
    datasets.save(dataset, cfg.datasets_dir)
    app = AppTest.from_file(str(APP), default_timeout=120)
    app.session_state[state.SS_SQL_EXECUTOR] = fake_executor
    app.session_state[state.SS_LLM_BACKEND] = ScriptedLLM()
    app.run()
    app.switch_page("pages/talk_to_data.py").run()
    assert not app.exception, [str(e.value) for e in app.exception]
    return app


def test_question_to_table_and_chart_with_history(at: AppTest) -> None:
    assert at.selectbox(key="dataset_label").options[0].startswith("talk demo · 7 tables · 70 rows")
    at.chat_input(key="question").set_value("How many restaurants are there?").run()
    assert not at.exception, [str(e.value) for e in at.exception]
    chat = at.session_state[state.SS_CHAT]
    assert len(chat) == 1 and chat[0]["answer"] == "There are 60 restaurants."
    assert chat[0]["items"][0]["kind"] == "sql" and chat[0]["items"][0]["rows"] == [[60]]
    assert any("There are 60 restaurants." in m.value for m in at.markdown)
    assert any(e.label == "SQL" for e in at.expander) and len(at.dataframe) >= 1
    at.chat_input(key="question").set_value("Plot revenue by city").run()
    assert not at.exception, [str(e.value) for e in at.exception]
    chat = at.session_state[state.SS_CHAT]
    assert len(chat) == 2 and chat[1]["answer"] == "Kraków leads with 120.50."
    kinds = [i["kind"] for i in chat[1]["items"]]
    assert kinds == ["sql", "chart"] and chat[1]["items"][1]["spec"]["chart_type"] == "bar"
    assert len(at.dataframe) == 2  # history replay keeps the first table
    at.button(key="clear_chat").click().run()
    assert at.session_state[state.SS_CHAT] == [] and not at.dataframe


def test_offline_mode_explains_that_gemini_is_needed(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:x@localhost:1/x")
    cfg = engine.get_settings.__wrapped__()
    ddl = SAMPLE_DDLS["company"].read_text(encoding="utf-8")
    datasets.save(
        engine.generate(engine.GenerationRequest(ddl=ddl, rows_per_table=5, seed=2), None, cfg),
        cfg.datasets_dir,
    )
    app = AppTest.from_file(str(APP), default_timeout=120)
    app.session_state[state.SS_SQL_EXECUTOR] = fake_executor
    app.session_state[state.SS_LLM_ENABLED] = False
    app.run()
    app.switch_page("pages/talk_to_data.py").run()
    app.chat_input(key="question").set_value("How many employees?").run()
    assert any("needs Gemini" in e.value for e in app.error)
    assert (
        app.session_state[state.SS_CHAT][0]["error"]
        and "needs Gemini" in app.session_state[state.SS_CHAT][0]["error"]
    )
