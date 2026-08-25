"""F3.1 — real Gemini on Vertex AI (RUN_LLM_TESTS=1). Asserts shapes/constraints, never exact wording."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from genai_data_gen_project.config import Settings
from genai_data_gen_project.llm import client as llm

pytestmark = pytest.mark.llm


class Person(BaseModel):
    first_name: str
    last_name: str
    age: int


class People(BaseModel):
    rows: list[Person]


@pytest.fixture(scope="module")
def gemini() -> llm.GeminiClient:
    return llm.GeminiClient(Settings())


def test_structured_output_with_pydantic(gemini: llm.GeminiClient) -> None:
    people = gemini.generate_structured(People, "Generate exactly 3 fictional adults.", temperature=0.5)
    assert len(people.rows) == 3 and all(p.age >= 18 for p in people.rows)
    assert gemini.usage.calls >= 1 and gemini.usage.total_tokens > 0


def test_dynamic_json_schema_with_nullable_and_enum(gemini: llm.GeminiClient) -> None:
    schema = {
        "title": "Restaurants",
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "cuisine": {"type": "string", "enum": ["Italian", "Mexican", "Other"]},
                        "phone": {"type": ["string", "null"]},
                    },
                    "required": ["name", "cuisine", "phone"],
                },
            }
        },
        "required": ["rows"],
    }
    data = gemini.generate_json(
        schema, "Generate 5 restaurants; about half with phone null.", temperature=0.9
    )
    rows = data["rows"]
    assert len(rows) == 5 and all(r["cuisine"] in {"Italian", "Mexican", "Other"} for r in rows)
    assert all(r["phone"] is None or isinstance(r["phone"], str) for r in rows)


def test_streaming(gemini: llm.GeminiClient) -> None:
    chunks = list(gemini.stream_text("Count from 1 to 15 separated by spaces, nothing else.", temperature=0))
    assert chunks and "15" in "".join(chunks)


def test_manual_function_calling_round_trip(gemini: llm.GeminiClient) -> None:
    run_sql = llm.function_declaration(
        "run_sql",
        "Run one read-only SQL SELECT against PostgreSQL and return the rows.",
        {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "a SELECT"}},
            "required": ["sql"],
        },
    )
    history = [
        llm.user_content("How many rows are in the customers table? Use the tool, then answer briefly.")
    ]
    turn = gemini.generate_with_tools(history, [run_sql], system="You answer questions about a SQL database.")
    assert turn.calls and turn.calls[0].name == "run_sql" and "customers" in turn.calls[0].args["sql"].lower()
    history += [turn.content, llm.function_response("run_sql", {"columns": ["count"], "rows": [[42]]})]
    final = gemini.generate_with_tools(
        history, [run_sql], system="You answer questions about a SQL database."
    )
    assert not final.calls and "42" in final.text


def test_retired_model_is_an_actionable_error() -> None:
    bad = llm.GeminiClient(Settings(_env_file=None, gemini_model="gemini-2.0-flash-001", llm_max_retries=0))
    with pytest.raises(llm.LLMError) as exc_info:
        bad.generate_json({"type": "object"}, "hi")
    assert exc_info.value.status == 404 and "GEMINI_MODEL" in str(exc_info.value)
