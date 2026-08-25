"""UI-side service helpers (docs/ui-rules.md): resolve the LLM backend, load sample DDLs, run generation.

Pages call these instead of touching infrastructure directly. Tests inject a `FakeLLM` through
`st.session_state[SS_LLM_BACKEND]`; the real Gemini client is created once per process (`st.cache_resource`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from genai_data_gen_project.config import Settings, get_settings
from genai_data_gen_project.generation import engine, feedback
from genai_data_gen_project.generation.expander import ExpansionError
from genai_data_gen_project.llm.client import LLMBackend, LLMError
from genai_data_gen_project.schema.order import UnsatisfiableSchemaError
from genai_data_gen_project.schema.parser import DDLParseError
from genai_data_gen_project.storage import datasets, postgres
from genai_data_gen_project.storage.dataset import Dataset
from genai_data_gen_project.ui import state

SPEC_DIR = Path(__file__).resolve().parents[3] / "project-spec"
SAMPLE_SCHEMAS: dict[str, str] = {
    "restaurants": "restrurants_schema.ddl",
    "library": "library_mgm_schema.ddl",
    "company": "company_employee_schema.ddl",
}


class UIError(Exception):
    """User-facing problem with a hint; pages render it with st.error (never a raw traceback)."""


def settings() -> Settings:
    return get_settings()


def sample_ddl(name: str) -> str:
    path = SPEC_DIR / SAMPLE_SCHEMAS[name]
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise UIError(f"sample schema {name!r} is not available ({e}) — upload or paste a DDL instead") from e


@st.cache_resource(show_spinner="Connecting to Gemini on Vertex AI…")
def _gemini_client() -> LLMBackend:
    from genai_data_gen_project.llm.client import GeminiClient  # lazy: only when Gemini is enabled

    return GeminiClient(get_settings())


def llm_backend() -> LLMBackend | None:
    """Injected backend (tests) → real Gemini client (when enabled) → None (offline heuristics + Faker)."""
    injected = st.session_state.get(state.SS_LLM_BACKEND)
    if injected is not None:
        return injected
    if not st.session_state.get(state.SS_LLM_ENABLED, True):
        return None
    try:
        return _gemini_client()
    except Exception as e:  # credentials / network problems must not crash the page
        raise UIError(
            f"Gemini is not reachable ({str(e).splitlines()[0][:160]}) — run `make check-env`, "
            "or switch off 'Use Gemini' to generate offline"
        ) from e


@dataclass
class GenerationParams:
    ddl: str
    instructions: str
    rows_per_table: int
    temperature: float
    seed: int
    name: str | None = None


def run_generation(
    params: GenerationParams, llm: LLMBackend | None, *, progress: Callable[[str], None] | None = None
) -> Dataset:
    """engine.generate with UI-friendly errors and the configured row cap."""
    cfg = get_settings()
    rows = max(1, min(params.rows_per_table, cfg.max_rows_per_table))
    request = engine.GenerationRequest(
        ddl=params.ddl,
        instructions=params.instructions or None,
        rows_per_table=rows,
        temperature=params.temperature,
        seed=params.seed,
        name=params.name,
        session_id=st.session_state.get("session_id"),
    )
    try:
        return engine.generate(request, llm, cfg, progress=progress)
    except DDLParseError as e:
        raise UIError(f"The DDL could not be parsed: {e}") from e
    except UnsatisfiableSchemaError as e:
        raise UIError(f"The schema cannot be generated: {e}") from e
    except ExpansionError as e:
        raise UIError(f"Row generation failed: {e}") from e
    except LLMError as e:
        raise UIError(f"Gemini failed: {e} — switch off 'Use Gemini' to generate offline") from e


def run_feedback(
    dataset: Dataset, table: str, text: str, llm: LLMBackend | None, *, seed: int = 0
) -> tuple[Dataset, feedback.EditResult, feedback.EditPlan]:
    """Textual feedback on one table → Gemini EditPlan → applied dataset; UI-friendly errors."""
    if not text.strip():
        raise UIError("Type what should change in the selected table before clicking Submit.")
    if llm is None:
        raise UIError("Textual feedback needs Gemini to interpret it — switch on 'Use Gemini' and try again.")
    try:
        return feedback.apply_feedback(dataset, table, text, llm, seed=seed)
    except feedback.EditError as e:
        raise UIError(f"The feedback could not be applied: {e}") from e
    except ExpansionError as e:
        raise UIError(f"Applying the feedback failed while regenerating values: {e}") from e
    except LLMError as e:
        raise UIError(f"Gemini failed while interpreting the feedback: {e}") from e


@dataclass
class SaveOutcome:
    dataset_id: str
    path: Path
    loaded: bool
    row_counts: dict[str, int]
    warning: str | None = None


def save_dataset(dataset: Dataset, *, name: str | None = None, cfg: Settings | None = None) -> SaveOutcome:
    """Persist to the filesystem registry, then load into PostgreSQL (a DB outage degrades to a warning)."""
    cfg = cfg or get_settings()
    if name and name.strip():
        dataset.name = name.strip()
    path = datasets.save(dataset, cfg.datasets_dir)
    try:
        result = postgres.load_dataset(dataset, cfg)
    except postgres.LoadError as e:
        return SaveOutcome(dataset.id, path, False, dataset.row_counts(), warning=str(e))
    return SaveOutcome(dataset.id, path, True, result.row_counts)
