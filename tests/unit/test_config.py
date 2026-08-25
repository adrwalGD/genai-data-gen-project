from pathlib import Path

import pytest

from genai_data_gen_project.config import Settings


def test_defaults_match_documented_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "GEMINI_MODEL",
        "GOOGLE_CLOUD_PROJECT",
        "DATABASE_URL",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.google_cloud_project == "gd-gcp-gridu-genai"
    assert s.google_cloud_location == "us-central1"
    assert s.gemini_model.startswith("gemini-2.5")
    assert s.gemini_thinking_budget == 0
    assert s.langfuse_enabled is False
    assert s.datasets_dir == Path("data") / "datasets"


def test_env_overrides_and_langfuse_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-x")
    monkeypatch.setenv("SQL_ROW_LIMIT", "42")
    s = Settings(_env_file=None)
    assert s.gemini_model == "gemini-test-model"
    assert s.langfuse_enabled is True
    assert s.sql_row_limit == 42


def test_invalid_values_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "0")
    with pytest.raises(ValueError):
        Settings(_env_file=None)
