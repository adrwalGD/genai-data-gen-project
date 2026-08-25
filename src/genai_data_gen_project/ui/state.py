"""Session-state keys and initialisation shared by every page (docs/ui-rules.md).

Pages read/write `st.session_state` only through these constants; tests inject state (e.g. a FakeLLM backend
or a pasted DDL) before the first `AppTest.run()`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import streamlit as st

SS_DDL_TEXT = "ddl_text"  # current DDL text (uploaded, pasted or sample)
SS_DDL_NAME = "ddl_name"  # where the DDL came from (file name / sample name)
SS_INSTRUCTIONS = "instructions"  # free-text generation instructions
SS_ROWS_PER_TABLE = "rows_per_table"
SS_TEMPERATURE = "temperature"
SS_SEED = "seed"
SS_LLM_ENABLED = "llm_enabled"  # use Gemini (False = offline heuristics + Faker)
SS_LLM_BACKEND = "llm_backend"  # injected LLMBackend (tests); None = build the real client
SS_DATASET = "dataset"  # storage.dataset.Dataset of the last generation
SS_SELECTED_TABLE = "selected_table"
SS_GENERATION_LOG = "generation_log"  # progress lines of the last run
SS_EDIT_LOG = "edit_log"  # applied feedback summaries
SS_ACTIVE_DATASET_ID = "active_dataset_id"  # Talk-to-data: selected saved dataset
SS_CHAT = "chat_history"  # Talk-to-data: list of turns
SS_LAST_ERROR = "last_error"

DEFAULTS: dict[str, Any | Callable[[], Any]] = {
    SS_DDL_TEXT: "",
    SS_DDL_NAME: "",
    SS_INSTRUCTIONS: "",
    SS_ROWS_PER_TABLE: 100,
    SS_TEMPERATURE: 0.7,
    SS_SEED: 42,
    SS_LLM_ENABLED: True,
    SS_LLM_BACKEND: None,
    SS_DATASET: None,
    SS_SELECTED_TABLE: None,
    SS_GENERATION_LOG: list,
    SS_EDIT_LOG: list,
    SS_ACTIVE_DATASET_ID: None,
    SS_CHAT: list,
    SS_LAST_ERROR: None,
}


def init() -> None:
    """Idempotent: set every key that is still missing to its default."""
    for key, default in DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = default() if callable(default) else default
