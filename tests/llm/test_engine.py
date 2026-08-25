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
        assert dataset.params["trace_id"], "expected a Langfuse trace id when tracing is enabled"
    print("langfuse trace:", dataset.params["trace_id"])
