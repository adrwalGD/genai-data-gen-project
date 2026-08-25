"""F5.4 — Save dataset: persisted to the registry (DB down → warning) and listed on Talk to your data."""

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from genai_data_gen_project.ui import state

APP = Path(__file__).resolve().parents[2] / "src" / "genai_data_gen_project" / "ui" / "app.py"


@pytest.fixture
def at(monkeypatch, tmp_path: Path) -> AppTest:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:x@localhost:1/x")  # e2e tests stay offline
    app = AppTest.from_file(str(APP), default_timeout=120)
    app.session_state[state.SS_LLM_ENABLED] = False
    app.run()
    app.radio(key="schema_source").set_value("Sample schema").run()
    app.selectbox(key="sample_schema").select("company").run()
    app.number_input(key=state.SS_ROWS_PER_TABLE).set_value(12).run()
    app.button(key="generate").click().run()
    assert not app.exception, [str(e.value) for e in app.exception]
    return app


def test_save_persists_and_is_listed_for_talk_to_data(at: AppTest, tmp_path: Path) -> None:
    at.text_input(key="dataset_name").input("acme demo").run()
    at.button(key="save_dataset").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    dataset_id = at.session_state[state.SS_ACTIVE_DATASET_ID]
    assert dataset_id
    assert any("PostgreSQL is not available" in w.value and dataset_id in w.value for w in at.warning)
    manifest = json.loads((tmp_path / "datasets" / dataset_id / "manifest.json").read_text())
    assert manifest["name"] == "acme demo" and manifest["tables"]["Employees"] == 12
    assert sorted(p.name for p in (tmp_path / "datasets" / dataset_id / "tables").iterdir()) == sorted(
        f"{t}.csv" for t in manifest["tables"]
    )
    at.switch_page("pages/talk_to_data.py").run()
    assert not at.exception, [str(e.value) for e in at.exception]
    labels = at.selectbox(key="dataset_label").options
    assert len(labels) == 1 and labels[0].startswith("acme demo · 7 tables · 84 rows")
