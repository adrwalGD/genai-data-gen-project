"""F5.2 — Data Generation page: sample schema → parameters → Generate (offline) → per-table preview."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from genai_data_gen_project.ui import state

APP = Path(__file__).resolve().parents[2] / "src" / "genai_data_gen_project" / "ui" / "app.py"


@pytest.fixture
def at(monkeypatch, tmp_path: Path) -> AppTest:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    app = AppTest.from_file(str(APP), default_timeout=120)
    app.session_state[state.SS_LLM_ENABLED] = False  # offline heuristics + Faker; no network in e2e tests
    return app


def test_generate_from_sample_schema_and_preview_tables(at: AppTest) -> None:
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.button(key="generate").disabled is True  # no schema yet
    at.radio(key="schema_source").set_value("Sample schema").run()
    at.selectbox(key="sample_schema").select("restaurants").run()
    assert "Parsed 7 tables" in at.success[0].value
    at.number_input(key=state.SS_ROWS_PER_TABLE).set_value(25).run()
    assert at.button(key="generate").disabled is False
    at.button(key="generate").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    dataset = at.session_state[state.SS_DATASET]
    assert dataset is not None and dataset.total_rows == 25 * 7 and dataset.params["llm"] is False
    assert any("no constraint violations" in s.value for s in at.success)
    assert at.selectbox(key="preview_table").options == dataset.schema.table_names
    assert len(at.dataframe) == 1 and at.metric[0].value == "25"
    at.selectbox(key="preview_table").select("Menu").run()
    assert at.session_state[state.SS_SELECTED_TABLE] == "Menu"
    assert list(at.dataframe[0].value.columns) == dataset.schema.table("Menu").column_names


def test_pasted_ddl_and_parse_errors(at: AppTest) -> None:
    at.run()
    at.radio(key="schema_source").set_value("Paste DDL").run()
    at.text_area(key="ddl_paste").input("CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(20) NOT NULL").run()
    assert any("could not be parsed" in e.value for e in at.error)
    assert at.button(key="generate").disabled is True
    at.text_area(key="ddl_paste").input(
        "CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(20) NOT NULL);"
    ).run()
    assert "Parsed 1 tables" in at.success[0].value
    at.number_input(key=state.SS_ROWS_PER_TABLE).set_value(5).run()
    at.button(key="generate").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state[state.SS_DATASET].total_rows == 5
