"""Offline stand-ins for the LLM layer (docs/testing.md). `FakeLLM` implements `llm.client.LLMBackend`."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from genai_data_gen_project.llm.client import FunctionCall, ToolTurn

T = TypeVar("T", bound=BaseModel)
Responder = Callable[[str], Any]


@dataclass
class FakeLLM:
    """Canned responses keyed by Pydantic model name / JSON-schema title; scripted tool turns; word streaming.

    - `structured[name]` may be an instance, a dict, or a callable(prompt_text) returning either.
    - `json_responses[title]` likewise for `generate_json` (title = schema['title'] or 'default').
    - `tool_turns` is consumed in order by `generate_with_tools`; when empty, the reply is `final_text`.
    """

    model: str = "fake-gemini"
    structured: dict[str, Any] = field(default_factory=dict)
    json_responses: dict[str, Any] = field(default_factory=dict)
    tool_turns: list[ToolTurn] = field(default_factory=list)
    final_text: str = "This is a fake answer."
    stream_texts: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def _record(self, kind: str, contents: Any, **extra: Any) -> str:
        text = contents if isinstance(contents, str) else " ".join(str(c) for c in contents)
        self.calls.append({"kind": kind, "contents": text, **extra})
        return text

    def generate_structured(
        self,
        response_model: type[T],
        contents: Any,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
    ) -> T:
        text = self._record("structured", contents, model=response_model.__name__, temperature=temperature)
        value = self.structured.get(response_model.__name__)
        if value is None:
            raise AssertionError(f"FakeLLM has no canned response for {response_model.__name__}")
        if callable(value) and not isinstance(value, type):
            value = value(text)
        return value if isinstance(value, response_model) else response_model.model_validate(value)

    def generate_json(
        self,
        json_schema: dict[str, Any],
        contents: Any,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
    ) -> Any:
        title = str(json_schema.get("title", "default"))
        text = self._record("json", contents, title=title, temperature=temperature)
        value = self.json_responses.get(title, self.json_responses.get("default"))
        if value is None:
            raise AssertionError(f"FakeLLM has no canned JSON response for {title!r}")
        return value(text) if callable(value) else value

    def stream_text(
        self, contents: Any, *, system: str | None = None, temperature: float = 0.3
    ) -> Iterator[str]:
        self._record("stream", contents, temperature=temperature)
        text = self.stream_texts.pop(0) if self.stream_texts else self.final_text
        for i, word in enumerate(text.split(" ")):
            yield word if i == 0 else f" {word}"

    def generate_with_tools(
        self,
        contents: list[Any],
        tools: Sequence[Any],
        *,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> ToolTurn:
        self._record("tools", contents, tools=[getattr(t, "name", str(t)) for t in tools])
        if self.tool_turns:
            return self.tool_turns.pop(0)
        return ToolTurn(text=self.final_text, calls=[], content=None)


def tool_call(name: str, **args: Any) -> ToolTurn:
    return ToolTurn(text="", calls=[FunctionCall(name=name, args=args)], content=None)
