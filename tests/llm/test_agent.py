"""F6.2 — real Gemini + local PostgreSQL: a count question runs SQL and answers with the true number."""

from collections.abc import Iterator

import pytest

from genai_data_gen_project.chat.agent import Agent
from genai_data_gen_project.config import Settings
from genai_data_gen_project.generation import engine
from genai_data_gen_project.llm.client import GeminiClient
from genai_data_gen_project.storage import postgres
from genai_data_gen_project.storage.dataset import Dataset
from tests.conftest import SAMPLE_DDLS

pytestmark = pytest.mark.llm


@pytest.fixture(scope="module")
def loaded() -> Iterator[Dataset]:
    settings = Settings()
    ddl = SAMPLE_DDLS["restaurants"].read_text(encoding="utf-8")
    dataset = engine.generate(engine.GenerationRequest(ddl=ddl, rows_per_table=60, seed=6), None, settings)
    postgres.load_dataset(dataset, settings)
    try:
        yield dataset
    finally:
        postgres.drop_dataset(dataset.id, settings)


def test_count_question(loaded: Dataset) -> None:
    settings = Settings()
    agent = Agent.for_dataset(loaded.id, loaded.schema, GeminiClient(settings), settings)
    events = list(agent.ask("How many restaurants are there?"))
    assert events[-1].kind == "final", events[-1]
    calls = [e for e in events if e.kind == "tool_call"]
    assert calls and calls[0].tool == "run_sql" and "restaurants" in calls[0].args["sql"].lower()
    assert "60" in events[-1].text
    assert any(e.kind == "text_delta" for e in events)


def test_top_n_with_chart(loaded: Dataset) -> None:
    settings = Settings()
    agent = Agent.for_dataset(loaded.id, loaded.schema, GeminiClient(settings), settings)
    events = list(agent.ask("Show a bar chart of the 5 restaurants with the highest order revenue."))
    assert events[-1].kind == "final", events[-1]
    charts = [e for e in events if e.kind == "tool_result" and e.tool == "render_chart" and e.chart]
    assert charts, [(e.kind, e.tool, e.error) for e in events]
    assert charts[0].chart["chart_type"] in {"bar", "line", "pie"}  # type: ignore[index]
    sql_results = [e for e in events if e.kind == "tool_result" and e.tool == "run_sql" and e.result]
    assert sql_results and sql_results[-1].result.row_count <= 5  # type: ignore[union-attr]
