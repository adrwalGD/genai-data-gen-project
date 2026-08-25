"""F3.3 — text pools: sizing, batching, dedupe, top-up and Faker fallback with a FakeLLM."""

import re

from genai_data_gen_project.generation import heuristics, pools
from genai_data_gen_project.generation.expander import expand
from genai_data_gen_project.generation.validator import validate
from genai_data_gen_project.llm.client import LLMError
from genai_data_gen_project.schema.parser import parse_ddl
from tests.fakes import FakeLLM


def test_pool_requests_are_sized_by_uniqueness(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["library"])
    plan = heuristics.plan(schema, 300)
    requests = {r.key: r for r in pools.pool_requests(plan, schema)}
    title = requests[("Books", "title")]
    assert (
        title.unique
        and title.size == 300
        and title.max_length == 255
        and "TABLE Books" in title.table_context
    )
    bio = requests[("Authors", "biography")]
    assert not bio.unique and bio.size == pools.SAMPLE_POOL and bio.max_length is None
    assert ("Publishers", "name") in requests and ("Library_Branches", "name") in requests
    assert requests[("Publishers", "name")].fallback_provider == "company"
    big = pools.pool_requests(heuristics.plan(schema, 5000), schema, max_unique=1500)
    assert next(r for r in big if r.key == ("Books", "title")).size == 1500


def counting_llm(per_call: int, prefix: str = "Value") -> FakeLLM:
    counter = {"n": 0}

    def respond(prompt: str) -> dict:  # type: ignore[type-arg]
        match = re.search(r"Generate (\d+) distinct values", prompt)
        n = min(int(match.group(1)) if match else per_call, per_call)
        values = [f"{prefix} {counter['n'] + i}" for i in range(n)]
        counter["n"] += n
        return {"values": values}

    return FakeLLM(json_responses={"TextPool": respond})


def test_fill_pools_batches_dedupes_and_truncates(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["library"])
    plan = heuristics.plan(schema, 120)
    fake = counting_llm(per_call=50)
    result = pools.fill_pools(plan, schema, fake, instructions="Polish books", temperature=0.5, max_workers=2)
    titles = result.pools[("Books", "title")]
    assert len(titles) == 120 and len(set(titles)) == 120
    title_calls = [c for c in fake.calls if "column `title` of table `Books`" in c["contents"]]
    assert len(title_calls) == 3  # 50 + 50 + 20
    assert "Generate 20 distinct values" in title_calls[-1]["contents"]
    assert "User instructions for the whole dataset: Polish books" in title_calls[0]["contents"]
    assert "Already used (avoid)" in title_calls[1]["contents"] and title_calls[0]["temperature"] == 0.5
    assert result.fallback_values == 0 and result.llm_values > 0 and result.notes == []
    tables = expand(schema, plan, seed=1, pools=result.pools)
    assert validate(schema, tables).ok and set(tables["Books"]["title"]) == set(titles)


def test_short_and_duplicate_responses_are_topped_up() -> None:
    schema = parse_ddl("CREATE TABLE Books (id INT PRIMARY KEY, title VARCHAR(12) NOT NULL);")
    plan = heuristics.plan(schema, 30)
    dup = FakeLLM(
        json_responses={
            "TextPool": {
                "values": ["Same Title Repeated Forever", "Same Title Repeated Forever", "  Other   one "]
            }
        }
    )
    result = pools.fill_pools(plan, schema, dup, max_workers=1)
    titles = result.pools[("Books", "title")]
    assert len(titles) == 30 and len(set(titles)) == 30
    assert titles[0] == "Same Title R" and titles[1] == "Other one"  # truncated to 12, whitespace normalised
    assert all(len(t) <= 12 for t in titles)
    assert any("from Faker" in n for n in result.notes) and result.fallback_values == 28
    tables = expand(schema, plan, seed=2, pools=result.pools)
    assert validate(schema, tables).ok


def test_llm_failure_falls_back_to_faker_without_raising() -> None:
    schema = parse_ddl("CREATE TABLE Restaurants (id INT PRIMARY KEY, name VARCHAR(60) NOT NULL);")
    plan = heuristics.plan(schema, 25)

    def boom(prompt: str) -> dict:  # type: ignore[type-arg]
        raise LLMError("quota", hint="wait")

    result = pools.fill_pools(plan, schema, FakeLLM(json_responses={"TextPool": boom}))
    names = result.pools[("Restaurants", "name")]
    assert len(names) == 25 and len(set(names)) == 25
    assert result.notes and "fell back to Faker" in result.notes[0] and result.fallback_values == 25


def test_offline_mode_uses_faker_only(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    plan = heuristics.plan(schema, 40)
    result = pools.fill_pools(plan, schema, None, seed=7)
    assert result.llm_values == 0 and result.fallback_values > 0
    assert ("Restaurants", "name") in result.pools and len(result.pools[("Restaurants", "name")]) == 40
    again = pools.fill_pools(plan, schema, None, seed=7)
    assert again.pools == result.pools  # deterministic by seed
    assert (
        pools.fill_pools(
            heuristics.plan(parse_ddl("CREATE TABLE t (id INT PRIMARY KEY);"), 5),
            parse_ddl("CREATE TABLE t (id INT PRIMARY KEY);"),
            None,
        ).pools
        == {}
    )
