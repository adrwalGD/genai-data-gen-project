"""F5.1 — the app shell renders both pages without an LLM or a database."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[2] / "src" / "genai_data_gen_project" / "ui" / "app.py"


def test_shell_renders_both_pages(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATA_DIR", str(tmp_path))  # no saved datasets
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.sidebar.title[0].value == "Data Assistant"
    assert at.header[0].value == "Data Generation"
    assert at.button[0].label == "Generate"
    at.switch_page("pages/talk_to_data.py").run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.header[0].value == "Talk to your data"
    assert "No saved datasets yet" in at.info[0].value
