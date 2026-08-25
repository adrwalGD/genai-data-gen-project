"""F3.4 — real end-to-end generation with Gemini for the restaurants schema, traced in Langfuse."""

import pytest

from genai_data_gen_project.config import Settings
from genai_data_gen_project.generation import engine
from genai_data_gen_project.llm.client import GeminiClient
from genai_data_gen_project.observability import init_observability

pytestmark = pytest.mark.llm


def test_restaurants_with_gemini(sample_ddl: dict[str, str]) -> None:
    settings = Settings()
    tracing = init_observability(settings)
    request = engine.GenerationRequest(
        ddl=sample_ddl["restaurants"],
        instructions="Italian and Polish restaurants in Kraków; realistic dish names and reviews in English.",
        rows_per_table=60,
        temperature=0.8,
        seed=5,
        session_id="llm-test",
    )
    dataset = engine.generate(request, GeminiClient(settings), settings)
    assert dataset.report_ok is True, dataset.report
    assert dataset.total_rows == 60 * 7 and dataset.params["llm"] is True
    assert dataset.params["pool_values"]["llm"] > 0, dataset.params["notes"]
    names = dataset.tables["Restaurants"]["name"].tolist()
    assert len(set(names)) == 60 and not any(n.startswith("Restaurants ") for n in names)
    if tracing:
        trace_id = dataset.params["trace_id"]
        assert trace_id, "expected a Langfuse trace id when tracing is enabled"
        generations = _langfuse_generations(settings, trace_id)
        # planner + every text-pool batch must nest under the data_generation trace (M3 gate finding)
        assert generations >= 2, f"trace {trace_id} has {generations} GENERATION observations"
    print("langfuse trace:", dataset.params["trace_id"])


def _langfuse_generations(settings: Settings, trace_id: str) -> int:
    import time

    import httpx

    auth = (settings.langfuse_public_key or "", settings.langfuse_secret_key or "")
    url = f"{settings.langfuse_base_url}/api/public/observations"
    for _ in range(12):  # ingestion is asynchronous; poll up to ~60 s
        time.sleep(5)
        data = httpx.get(url, params={"traceId": trace_id, "limit": 100}, auth=auth, timeout=30).json()
        count = sum(1 for o in data.get("data", []) if o.get("type") == "GENERATION")
        if count >= 2:
            return count
    return count
