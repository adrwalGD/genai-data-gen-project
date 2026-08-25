"""F3.4 — engine orchestration with FakeLLM: planner overrides + pools used, failures degrade to notes."""

import re

from genai_data_gen_project.config import Settings
from genai_data_gen_project.generation import engine
from genai_data_gen_project.generation.recipes import DecimalRangeRecipe
from genai_data_gen_project.llm.client import LLMError
from tests.fakes import FakeLLM


def settings() -> Settings:
    return Settings(_env_file=None, langfuse_public_key=None, langfuse_secret_key=None, llm_max_concurrency=2)


def test_offline_generation_produces_a_valid_dataset(sample_ddl: dict[str, str]) -> None:
    messages: list[str] = []
    request = engine.GenerationRequest(ddl=sample_ddl["restaurants"], rows_per_table=40, seed=3, name="demo")
    dataset = engine.generate(request, None, settings(), progress=messages.append)
    assert dataset.name == "demo" and dataset.total_rows == 40 * 7 and dataset.report_ok is True
    assert (
        dataset.params["llm"] is False
        and dataset.params["model"] is None
        and dataset.params["trace_id"] is None
    )
    assert set(dataset.params["timings_s"]) == {"parse", "plan", "pools", "expand", "validate"}
    assert dataset.params["pool_values"]["llm"] == 0 and dataset.params["pool_values"]["fallback"] > 0
    assert messages[0] == "Parsing DDL" and "Generating rows" in messages and "Validating" in messages
    report = engine.report_of(dataset)
    plan = engine.plan_of(dataset)
    assert report is not None and report.ok and plan is not None and plan.table("Orders").rows == 40  # type: ignore[union-attr]


def pool_llm() -> FakeLLM:
    counter = {"n": 0}

    def pool(prompt: str) -> dict:  # type: ignore[type-arg]
        n = int(re.search(r"Generate (\d+) distinct", prompt).group(1))  # type: ignore[union-attr]
        values = [f"LLM Name {counter['n'] + i}" for i in range(n)]
        counter["n"] += n
        return {"values": values}

    planner_output = {
        "table_rows": [{"table": "Order_Items", "rows": 90}],
        "overrides": [
            {
                "table": "Menu",
                "column": "price",
                "kind": "decimal_range",
                "min": 5,
                "max": 25,
                "rationale": "cheap",
            },
        ],
        "notes": ["kept it simple"],
    }
    return FakeLLM(structured={"PlannerOutput": planner_output}, json_responses={"TextPool": pool})


def test_llm_generation_uses_planner_and_pools(sample_ddl: dict[str, str]) -> None:
    fake = pool_llm()
    request = engine.GenerationRequest(
        ddl=sample_ddl["restaurants"], instructions="cheap menu", rows_per_table=30, temperature=1.0, seed=1,
        session_id="s1",
    )  # fmt: skip
    dataset = engine.generate(request, fake, settings())
    assert (
        dataset.report_ok is True
        and dataset.params["llm"] is True
        and dataset.params["model"] == "fake-gemini"
    )
    assert len(dataset.tables["Order_Items"]) == 90 and len(dataset.tables["Menu"]) == 30
    plan = engine.plan_of(dataset)
    price = plan.table("Menu").column("price").recipe  # type: ignore[union-attr]
    assert isinstance(price, DecimalRangeRecipe) and (price.min, price.max) == (5, 25)
    assert all(5 <= float(p) <= 25 for p in dataset.tables["Menu"]["price"])
    names = dataset.tables["Restaurants"]["name"].tolist()
    assert all(n.startswith("LLM Name") for n in names) and len(set(names)) == 30
    kinds = [c["kind"] for c in fake.calls]
    assert kinds[0] == "structured" and kinds.count("json") >= 1
    assert fake.calls[0]["temperature"] == 0.6  # planner runs cooler than the requested temperature
    assert any(n.startswith("llm: kept it simple") for n in dataset.params["notes"])
    assert dataset.params["pool_values"]["llm"] > 0


def test_llm_failures_degrade_to_notes(sample_ddl: dict[str, str]) -> None:
    def boom(prompt: str) -> dict:  # type: ignore[type-arg]
        raise LLMError("quota exhausted", hint="wait")

    def boom_structured(prompt: str) -> dict:  # type: ignore[type-arg]
        raise LLMError("quota exhausted", hint="wait")

    fake = FakeLLM(structured={"PlannerOutput": boom_structured}, json_responses={"TextPool": boom})
    request = engine.GenerationRequest(ddl=sample_ddl["company"], rows_per_table=20, seed=2)
    dataset = engine.generate(request, fake, settings())
    assert dataset.report_ok is True and dataset.total_rows == 20 * 7
    notes = "\n".join(dataset.params["notes"])
    assert "planner: Gemini failed" in notes and "pool fell back to Faker" in notes
    assert dataset.params["pool_values"]["llm"] == 0
