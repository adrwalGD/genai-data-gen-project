"""Shared pytest configuration: marker-based skipping and environment isolation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = REPO_ROOT / "project-spec"
SAMPLE_DDLS = {
    "library": SPEC_DIR / "library_mgm_schema.ddl",
    "restaurants": SPEC_DIR / "restrurants_schema.ddl",
    "company": SPEC_DIR / "company_employee_schema.ddl",
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    run_llm = os.environ.get("RUN_LLM_TESTS") == "1"
    skip_llm = pytest.mark.skip(
        reason="LLM test: set RUN_LLM_TESTS=1 (make test-llm) to run against Vertex AI"
    )
    for item in items:
        if "llm" in item.keywords and not run_llm:
            item.add_marker(skip_llm)


@pytest.fixture(autouse=True)
def _quiet_langfuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit/UI tests never trace. LLM tests may opt in by setting keys themselves."""
    if os.environ.get("RUN_LLM_TESTS") != "1":
        monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")
        # empty env vars override the .env file for pydantic-settings → Settings.langfuse_enabled is False
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")


@pytest.fixture
def sample_ddl() -> dict[str, str]:
    return {name: path.read_text(encoding="utf-8") for name, path in SAMPLE_DDLS.items()}
