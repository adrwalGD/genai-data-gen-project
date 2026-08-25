"""F6.5 — each talk-to-data turn is a Langfuse trace with session id, tags, tool spans and GENERATIONs."""

import time
from collections.abc import Iterator

import httpx
import pytest

from genai_data_gen_project.chat.agent import Agent
from genai_data_gen_project.config import Settings
from genai_data_gen_project.generation import engine
from genai_data_gen_project.llm.client import GeminiClient
from genai_data_gen_project.observability import init_observability
from genai_data_gen_project.storage import postgres
from genai_data_gen_project.storage.dataset import Dataset
from tests.conftest import SAMPLE_DDLS

pytestmark = pytest.mark.llm


@pytest.fixture(scope="module")
def loaded() -> Iterator[Dataset]:
    settings = Settings()
    ddl = SAMPLE_DDLS["restaurants"].read_text(encoding="utf-8")
    dataset = engine.generate(engine.GenerationRequest(ddl=ddl, rows_per_table=40, seed=9), None, settings)
    postgres.load_dataset(dataset, settings)
    try:
        yield dataset
    finally:
        postgres.drop_dataset(dataset.id, settings)


def _fetch(settings: Settings, path: str, params: dict[str, str]) -> dict:  # type: ignore[type-arg]
    auth = (settings.langfuse_public_key or "", settings.langfuse_secret_key or "")
    return httpx.get(f"{settings.langfuse_base_url}{path}", params=params, auth=auth, timeout=30).json()  # type: ignore[no-any-return]


def test_turn_trace_has_session_tags_tool_span_and_generations(loaded: Dataset) -> None:
    settings = Settings()
    if not init_observability(settings):
        pytest.skip("Langfuse keys not configured")
    agent = Agent.for_dataset(
        loaded.id, loaded.schema, GeminiClient(settings), settings, session_id="trace-test"
    )
    events = list(agent.ask("How many customers are there?"))
    assert events[-1].kind == "final", events[-1]
    trace_id = events[-1].trace_id
    assert trace_id and trace_id == agent.last_trace_id
    trace: dict = {}  # type: ignore[type-arg]
    observations: list[dict] = []  # type: ignore[type-arg]
    for _ in range(12):  # ingestion is asynchronous
        time.sleep(5)
        trace = _fetch(settings, f"/api/public/traces/{trace_id}", {})
        observations = _fetch(
            settings, "/api/public/observations", {"traceId": trace_id, "limit": "100"}
        ).get("data", [])
        kinds = {o.get("type") for o in observations}
        if (
            trace.get("name") == "talk_to_data_turn"
            and "GENERATION" in kinds
            and any(o.get("name", "").startswith("tool.run_sql") for o in observations)
        ):
            break
    assert trace.get("name") == "talk_to_data_turn", trace
    assert trace.get("sessionId") == "trace-test" and "talk-to-data" in (trace.get("tags") or []), trace
    names = [o.get("name") for o in observations]
    assert any(n and n.startswith("tool.run_sql") for n in names), names
    assert sum(1 for o in observations if o.get("type") == "GENERATION") >= 2, (
        names
    )  # tool round + streamed answer
