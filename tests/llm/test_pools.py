"""F3.3 — real Gemini text pools: 300 unique book titles within the VARCHAR length."""

import pytest

from genai_data_gen_project.config import Settings
from genai_data_gen_project.generation import heuristics, pools
from genai_data_gen_project.llm.client import GeminiClient
from genai_data_gen_project.schema.parser import parse_ddl

pytestmark = pytest.mark.llm


def test_300_unique_book_titles(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["library"])
    plan = heuristics.plan(schema, 300)
    # keep the test focused on one pool: strip other text_pool recipes' cost by lowering their table rows
    for tp in plan.tables:
        if tp.table != "Books":
            tp.rows = 5
    result = pools.fill_pools(
        plan, schema, GeminiClient(Settings()), instructions="fiction and non-fiction", max_workers=4
    )
    titles = result.pools[("Books", "title")]
    assert len(titles) >= 300 and len(set(titles)) == len(titles)
    assert all(0 < len(t) <= 255 for t in titles)
    assert result.llm_values >= 250, result.notes  # a small Faker top-up is tolerated, not a fallback
