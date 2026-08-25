"""F3.1 — offline behaviour of the Gemini wrapper: retries, error classification, parsing, tool turns."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel

from genai_data_gen_project.config import Settings
from genai_data_gen_project.llm import client as llm
from tests.fakes import FakeLLM, tool_call


class Person(BaseModel):
    name: str
    age: int


@dataclass
class FakeUsage:
    prompt_token_count: int = 10
    candidates_token_count: int = 5
    total_token_count: int = 15


@dataclass
class FakeResponse:
    text: str | None = None
    parsed: Any = None
    function_calls: list[Any] = field(default_factory=list)
    candidates: list[Any] = field(default_factory=list)
    usage_metadata: Any = field(default_factory=FakeUsage)


class FakeError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class ScriptedModels:
    """Stands in for `genai.Client().models`: returns/raises scripted results in order."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def generate_content_stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return iter(item)


def make_client(script: list[Any], **settings: Any) -> tuple[llm.GeminiClient, ScriptedModels, list[float]]:
    models = ScriptedModels(script)
    sleeps: list[float] = []
    fake_sdk = type("SDK", (), {"models": models})()
    cfg = Settings(_env_file=None, langfuse_public_key=None, langfuse_secret_key=None, **settings)
    return llm.GeminiClient(cfg, client=fake_sdk, sleep=sleeps.append), models, sleeps


def test_structured_uses_parsed_and_counts_usage() -> None:
    client, models, _ = make_client(
        [FakeResponse(text='{"name":"A","age":3}', parsed=Person(name="A", age=3))]
    )
    person = client.generate_structured(Person, "make a person", temperature=0.2, system="sys")
    assert person == Person(name="A", age=3)
    cfg = models.calls[0]["config"]
    assert cfg.response_mime_type == "application/json" and cfg.response_schema is Person
    assert cfg.temperature == 0.2 and cfg.system_instruction == "sys"
    assert cfg.thinking_config.thinking_budget == 0
    assert client.usage.calls == 1 and client.usage.total_tokens == 15


def test_structured_retries_once_on_validation_error_then_fails() -> None:
    client, models, _ = make_client(
        [FakeResponse(text='{"name":"A"}'), FakeResponse(text='{"name":"A","age":"x"}')]
    )
    with pytest.raises(llm.LLMError, match="does not match Person"):
        client.generate_structured(Person, "make a person")
    assert len(models.calls) == 2 and "did not match the schema" in models.calls[1]["contents"]
    client2, _, _ = make_client(
        [FakeResponse(text='{"name":"A"}'), FakeResponse(text='{"name":"B","age":4}')]
    )
    assert client2.generate_structured(Person, ["ctx", "make a person"]) == Person(name="B", age=4)


def test_retries_with_backoff_on_429_and_gives_up_on_400() -> None:
    client, models, sleeps = make_client(
        [
            FakeError(429, "RESOURCE_EXHAUSTED"),
            FakeError(503, "unavailable"),
            FakeResponse(text='{"rows": []}'),
        ],
        llm_max_retries=3,
    )
    assert client.generate_json({"type": "object"}, "go") == {"rows": []}
    assert len(models.calls) == 3 and len(sleeps) == 2 and 1 <= sleeps[0] < 1.6 and 2 <= sleeps[1] < 2.6
    client2, _, sleeps2 = make_client([FakeError(400, "bad schema")])
    with pytest.raises(llm.LLMError) as exc_info:
        client2.generate_json({"type": "object"}, "go")
    assert exc_info.value.status == 400 and "check the schema" in str(exc_info.value) and not sleeps2
    client3, _, sleeps3 = make_client([FakeError(429, "quota")] * 6, llm_max_retries=2)
    with pytest.raises(llm.LLMError) as exc_info3:
        client3.generate_json({"type": "object"}, "go")
    assert exc_info3.value.retryable and len(sleeps3) == 2 and "quota" in str(exc_info3.value)


def test_error_classification_hints() -> None:
    assert "GEMINI_MODEL" in llm.classify_error(FakeError(404, "not found")).hint
    assert "gcloud auth" in llm.classify_error(FakeError(401, "invalid_grant")).hint
    assert llm.classify_error(FakeError(500, "boom")).retryable
    assert llm.classify_error(ValueError("weird")).hint.startswith("see docs/environment.md")


def test_empty_text_is_an_actionable_error() -> None:
    finish = type("C", (), {"finish_reason": "MAX_TOKENS", "content": None})()
    client, _, _ = make_client([FakeResponse(text=None, candidates=[finish])])
    with pytest.raises(llm.LLMError, match="finish_reason=MAX_TOKENS"):
        client.generate_json({"type": "object"}, "go")


def test_stream_and_tool_turns() -> None:
    chunks = [
        type("Ch", (), {"text": "Hel", "usage_metadata": None})(),
        type("Ch", (), {"text": "lo", "usage_metadata": FakeUsage()})(),
    ]
    client, _, _ = make_client([chunks])
    assert "".join(client.stream_text("hi", system="s")) == "Hello"
    assert client.usage.total_tokens == 15
    fc = type("FC", (), {"name": "run_sql", "args": {"sql": "SELECT 1"}})()
    candidate = type("Cand", (), {"content": "MODEL_CONTENT"})()
    client2, models2, _ = make_client([FakeResponse(text=None, function_calls=[fc], candidates=[candidate])])
    decl = llm.function_declaration(
        "run_sql", "run", {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    )
    turn = client2.generate_with_tools([llm.user_content("count")], [decl], system="sys")
    assert turn.calls == [llm.FunctionCall(name="run_sql", args={"sql": "SELECT 1"})]
    assert turn.text == "" and turn.content == "MODEL_CONTENT"
    cfg = models2.calls[0]["config"]
    assert cfg.automatic_function_calling.disable is True and len(cfg.tools) == 1
    resp = llm.function_response("run_sql", {"rows": [[1]]})
    assert resp.role == "user" and resp.parts[0].function_response.name == "run_sql"


def test_pro_models_keep_thinking_enabled() -> None:
    client, models, _ = make_client([FakeResponse(text="{}")], gemini_model="gemini-2.5-pro")
    client.generate_json({"type": "object"}, "go")
    assert models.calls[0]["config"].thinking_config is None


def test_fake_llm_implements_the_protocol() -> None:
    fake = FakeLLM(structured={"Person": {"name": "Z", "age": 1}}, json_responses={"default": {"ok": True}},
                   tool_turns=[tool_call("run_sql", sql="SELECT 1")], final_text="done here")  # fmt: skip
    backend: llm.LLMBackend = fake
    assert backend.generate_structured(Person, "x") == Person(name="Z", age=1)
    assert backend.generate_json({"type": "object"}, "x") == {"ok": True}
    assert "".join(backend.stream_text("x")) == "done here"
    assert backend.generate_with_tools([], []).calls[0].name == "run_sql"
    assert backend.generate_with_tools([], []).text == "done here"
    assert [c["kind"] for c in fake.calls] == ["structured", "json", "stream", "tools", "tools"]


def test_settings_bound_concurrency_gate_exists() -> None:
    client, _, _ = make_client([], llm_max_concurrency=2)
    gate: Callable[[], bool] = client._gate.acquire
    assert gate() and gate() and not client._gate.acquire(blocking=False)


def test_stream_retries_when_the_first_chunk_fails_and_classifies_mid_stream_errors() -> None:
    client, models, sleeps = make_client(
        [FakeError(429, "RESOURCE_EXHAUSTED"), [FakeResponse(text="a"), FakeResponse(text="b")]]
    )
    assert list(client.stream_text("hi")) == ["a", "b"]
    assert len(models.calls) == 2 and len(sleeps) == 1  # first attempt failed at the first next()

    def broken() -> Any:
        yield FakeResponse(text="partial")
        raise FakeError(503, "UNAVAILABLE")

    client, _, _ = make_client([broken()])
    pieces: list[str] = []
    with pytest.raises(llm.LLMError) as info:
        pieces.extend(client.stream_text("hi"))
    assert pieces == ["partial"] and info.value.retryable and info.value.status == 503


def test_model_content_and_404_hint_use_the_configured_default_model() -> None:
    content = llm.model_content("earlier answer")
    assert content.role == "model" and content.parts[0].text == "earlier answer"
    err = llm.classify_error(FakeError(404, "Publisher Model not found"))
    assert err.status == 404 and Settings.model_fields["gemini_model"].default in err.hint
