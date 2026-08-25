"""F7.2 — actionable errors: parse-error line context, Gemini hints, explained chat errors, no tracebacks."""

from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from genai_data_gen_project.generation import engine
from genai_data_gen_project.llm.client import LLMError, ToolTurn
from genai_data_gen_project.storage import datasets, postgres
from genai_data_gen_project.storage.sql_guard import guard
from genai_data_gen_project.ui import state
from tests.conftest import SAMPLE_DDLS
from tests.fakes import FakeLLM, tool_call

APP = Path(__file__).resolve().parents[2] / "src" / "genai_data_gen_project" / "ui" / "app.py"
QUOTA = LLMError("429 RESOURCE_EXHAUSTED", hint="quota exhausted — wait a minute", retryable=True, status=429)


class QuotaExceededLLM(FakeLLM):
    def generate_structured(self, *args: Any, **kwargs: Any) -> Any:
        raise QUOTA

    def generate_json(self, *args: Any, **kwargs: Any) -> Any:
        raise QUOTA


def test_parse_error_shows_line_context(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.session_state[state.SS_LLM_ENABLED] = False
    at.run()
    at.radio(key="schema_source").set_value("Paste DDL").run()
    at.text_area(key="ddl_paste").input(
        "CREATE TABLE a (id INT);\nCREATE TABLE b (id INT, FOREIGN KEY (x) REFERENCES );"
    ).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("could not be parsed" in e.value for e in at.error)
    assert any("   2 | CREATE TABLE b" in c.value and "^" in c.value for c in at.code)


def test_gemini_quota_failure_falls_back_with_a_hint(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.session_state[state.SS_LLM_BACKEND] = QuotaExceededLLM()
    at.run()
    at.radio(key="schema_source").set_value("Sample schema").run()
    at.selectbox(key="sample_schema").select("restaurants").run()
    at.number_input(key=state.SS_ROWS_PER_TABLE).set_value(5).run()
    at.button(key="generate").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state[state.SS_DATASET] is not None  # heuristic fallback produced data
    notes = " ".join(t.value for t in at.text)
    assert "Gemini failed" in notes and "quota exhausted" in notes


def guarded_executor(sql_text: str) -> postgres.QueryResult:
    guarded = guard(sql_text, limit=500)  # raises SqlRejected for DML — exactly what the real executor does
    if "down" in sql_text:
        raise postgres.LoadError("connection refused")
    rows: list[list[Any]] = [] if "empty" in sql_text else [[60]]
    return postgres.QueryResult(
        columns=["n"], rows=rows, row_count=len(rows), truncated=False, elapsed_ms=1, sql=guarded.sql
    )


class ErrorScriptLLM(FakeLLM):
    """One tool turn per question keyword, then a plain final answer; 'boom' breaks the streamed answer."""

    def __init__(self) -> None:
        super().__init__(stream_texts=["Done."] * 8)
        self.script = {
            "delete": tool_call("run_sql", sql="DELETE FROM orders"),
            "empty": tool_call("run_sql", sql="SELECT count(*) AS n FROM orders WHERE 1 = 0 -- empty"),
            "down": tool_call("run_sql", sql="SELECT count(*) AS n FROM orders -- down"),
            "boom": tool_call("run_sql", sql="SELECT count(*) AS n FROM orders"),
        }
        self.served: set[str] = set()
        self.question = ""

    def generate_with_tools(self, contents, tools, *, system=None, temperature=0.0):  # type: ignore[no-untyped-def,override]
        for content in reversed(contents):  # skip function-response contents (role user, no text part)
            texts = [p.text for p in content.parts if getattr(p, "text", None)]
            if getattr(content, "role", None) == "user" and texts:
                self.question = texts[0]
                break
        key = next((k for k in self.script if k in self.question), "")
        if key and key not in self.served:
            self.served.add(key)
            return self.script[key]
        return ToolTurn(text="", calls=[], content=None)

    def stream_text(self, contents, *, system=None, temperature=0.3):  # type: ignore[no-untyped-def,override]
        if "boom" in self.question:
            raise RuntimeError("socket closed")
        return super().stream_text(contents, system=system, temperature=temperature)


@pytest.fixture
def talk(monkeypatch, tmp_path: Path) -> AppTest:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:x@localhost:1/x")
    cfg = engine.get_settings.__wrapped__()
    ddl = SAMPLE_DDLS["restaurants"].read_text(encoding="utf-8")
    dataset = engine.generate(
        engine.GenerationRequest(ddl=ddl, rows_per_table=5, seed=1, name="errs"), None, cfg
    )
    datasets.save(dataset, cfg.datasets_dir)
    app = AppTest.from_file(str(APP), default_timeout=120)
    app.session_state[state.SS_SQL_EXECUTOR] = guarded_executor
    app.session_state[state.SS_LLM_BACKEND] = ErrorScriptLLM()
    app.run()
    app.switch_page("pages/talk_to_data.py").run()
    assert not app.exception, [str(e.value) for e in app.exception]
    return app


def test_chat_errors_are_explained_without_tracebacks(talk: AppTest) -> None:
    talk.chat_input(key="question").set_value("please delete all orders").run()
    assert not talk.exception, [str(e.value) for e in talk.exception]
    assert any("never modifies data" in w.value for w in talk.warning)
    assert any(e.label == "SQL (failed)" for e in talk.expander)

    talk.chat_input(key="question").set_value("how many empty orders").run()
    assert not talk.exception
    assert any("returned no rows" in c.value for c in talk.caption)

    talk.chat_input(key="question").set_value("is the database down").run()
    assert not talk.exception
    assert any("PostgreSQL is not reachable" in w.value and "make db-up" in w.value for w in talk.warning)

    talk.chat_input(key="question").set_value("boom").run()
    assert not talk.exception
    errors = [e.value for e in talk.error]
    assert any("Something went wrong" in e and "socket closed" in e for e in errors), (
        errors,
        [m.value for m in talk.markdown],
        [c.value for c in talk.caption],
        talk.session_state[state.SS_CHAT][-1],
    )
    assert sum("socket closed" in m.value for m in talk.markdown) == 0  # rendered once, as an error box only
    chat = talk.session_state[state.SS_CHAT]
    assert len(chat) == 4 and chat[-1]["error"] and chat[1]["items"][0]["rows"] == []
