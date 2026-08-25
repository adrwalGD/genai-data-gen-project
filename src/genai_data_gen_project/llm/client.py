"""Gemini on Vertex AI (F3.1) — the ONLY module that constructs `genai.Client` (CLAUDE.md rule 1).

Verified call shapes live in docs/gemini-rules.md. This wrapper adds what every caller needs and must not
re-implement: model + thinking budget from Settings, structured output (Pydantic or dynamic JSON schema),
streaming, manual function-calling turns (AFC disabled so the UI can show tool activity), bounded
concurrency, retries with exponential backoff on 429/5xx, pydantic re-validation with one corrective retry,
usage accounting, and `LLMError`s whose messages carry the fix. Langfuse/OpenInference instrumentation is
process-wide (`observability.init_observability`), so every call appears as a GENERATION when tracing is on.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import Settings, get_settings
from ..observability import init_observability

_log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
Contents = str | list[Any]


class LLMError(RuntimeError):
    """A Gemini call failed for good; `hint` says what to do about it."""

    def __init__(
        self, message: str, *, hint: str = "", retryable: bool = False, status: int | None = None
    ) -> None:
        super().__init__(f"{message} — {hint}" if hint else message)
        self.message = message
        self.hint = hint
        self.retryable = retryable
        self.status = status


@dataclass
class FunctionCall:
    name: str
    args: dict[str, Any]


@dataclass
class ToolTurn:
    """One model turn in a manual function-calling loop."""

    text: str
    calls: list[FunctionCall]
    content: Any = field(default=None, repr=False, metadata={"doc": "raw model Content to append to history"})


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def add(self, metadata: Any) -> None:
        self.calls += 1
        if metadata is None:
            return
        self.prompt_tokens += int(getattr(metadata, "prompt_token_count", 0) or 0)
        self.output_tokens += int(getattr(metadata, "candidates_token_count", 0) or 0)
        self.total_tokens += int(getattr(metadata, "total_token_count", 0) or 0)


class LLMBackend(Protocol):
    """What generation/chat depend on; `GeminiClient` and `tests/fakes.py::FakeLLM` implement it."""

    model: str

    def generate_structured(
        self,
        response_model: type[T],
        contents: Contents,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
    ) -> T: ...

    def generate_json(
        self,
        json_schema: dict[str, Any],
        contents: Contents,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
    ) -> Any: ...

    def stream_text(
        self, contents: Contents, *, system: str | None = None, temperature: float = 0.3
    ) -> Iterator[str]: ...

    def generate_with_tools(
        self,
        contents: list[Any],
        tools: Sequence[Any],
        *,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> ToolTurn: ...


RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class GeminiClient:
    """Thread-safe Gemini client. Construct once per process (e.g. `st.cache_resource`)."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or get_settings()
        self.model = self.settings.gemini_model
        self.usage = Usage()
        self._sleep = sleep
        self._gate = threading.BoundedSemaphore(self.settings.llm_max_concurrency)
        init_observability(self.settings)
        for name in (
            "google_genai",
            "google_genai.models",
        ):  # "Direct use of AFC ... not recommended" on every call
            logging.getLogger(name).setLevel(logging.ERROR)
        if client is None:
            from google import genai

            client = genai.Client(
                vertexai=True,
                project=self.settings.google_cloud_project,
                location=self.settings.google_cloud_location,
            )
        self._client = client

    # -- config ----------------------------------------------------------------------------------------------
    def _config(self, **overrides: Any) -> Any:
        from google.genai import types

        budget = self.settings.gemini_thinking_budget
        kwargs: dict[str, Any] = {}
        if not ("pro" in self.model and budget == 0):  # 2.5-pro cannot disable thinking
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        return types.GenerateContentConfig(**kwargs)

    # -- public API -----------------------------------------------------------------------------------------
    def generate_structured(
        self,
        response_model: type[T],
        contents: Contents,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
    ) -> T:
        config = self._config(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=response_model,
        )
        response = self._call(
            lambda: self._client.models.generate_content(model=self.model, contents=contents, config=config)
        )
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, response_model):
            return parsed
        try:
            return response_model.model_validate_json(_text_of(response))
        except ValidationError as first_error:
            _log.warning(
                "structured output failed validation (%s); retrying once with the error",
                response_model.__name__,
            )
            fix = (
                f"Your previous JSON did not match the schema: {first_error.errors()[:3]}. "
                "Return ONLY valid JSON matching the schema."
            )
            response = self._call(
                lambda: self._client.models.generate_content(
                    model=self.model, contents=_append_text(contents, fix), config=config
                )
            )
            parsed = getattr(response, "parsed", None)
            if isinstance(parsed, response_model):
                return parsed
            try:
                return response_model.model_validate_json(_text_of(response))
            except ValidationError as e:
                raise LLMError(
                    f"Gemini returned JSON that does not match {response_model.__name__}: {e.errors()[:3]}",
                    hint="lower the temperature or simplify the schema",
                ) from e

    def generate_json(
        self,
        json_schema: dict[str, Any],
        contents: Contents,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
    ) -> Any:
        config = self._config(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=json_schema,
        )
        response = self._call(
            lambda: self._client.models.generate_content(model=self.model, contents=contents, config=config)
        )
        text = _text_of(response)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(
                f"Gemini returned invalid JSON ({e.msg} at {e.pos}); output may be truncated",
                hint="raise max_output_tokens or ask for fewer rows per batch",
            ) from e

    def stream_text(
        self, contents: Contents, *, system: str | None = None, temperature: float = 0.3
    ) -> Iterator[str]:
        config = self._config(temperature=temperature, system_instruction=system)

        def start() -> Any:
            return self._client.models.generate_content_stream(
                model=self.model, contents=contents, config=config
            )

        stream = self._call(start)
        last_usage = None
        for chunk in stream:
            last_usage = getattr(chunk, "usage_metadata", None) or last_usage
            if getattr(chunk, "text", None):
                yield chunk.text
        self.usage.add(last_usage)

    def generate_with_tools(
        self,
        contents: list[Any],
        tools: Sequence[Any],
        *,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> ToolTurn:
        from google.genai import types

        config = self._config(
            temperature=temperature,
            system_instruction=system,
            tools=[types.Tool(function_declarations=list(tools))],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response = self._call(
            lambda: self._client.models.generate_content(model=self.model, contents=contents, config=config)
        )
        calls = [
            FunctionCall(name=fc.name, args=dict(fc.args or {})) for fc in (response.function_calls or [])
        ]
        content = response.candidates[0].content if getattr(response, "candidates", None) else None
        text = "" if calls else _text_of(response, allow_empty=True)
        return ToolTurn(text=text, calls=calls, content=content)

    # -- helpers ---------------------------------------------------------------------------------------------
    def _call(self, fn: Callable[[], Any]) -> Any:
        """Run one SDK call under the concurrency gate with retries; count usage; normalise errors."""
        attempts = self.settings.llm_max_retries + 1
        for attempt in range(1, attempts + 1):
            with self._gate:
                try:
                    result = fn()
                except Exception as e:  # classified below
                    error = classify_error(e)
                    if error.retryable and attempt < attempts:
                        delay = min(2 ** (attempt - 1), 16) + random.uniform(0, 0.5)
                        _log.warning(
                            "Gemini call failed (%s); retry %d/%d in %.1fs",
                            error.message,
                            attempt,
                            attempts - 1,
                            delay,
                        )
                        self._sleep(delay)
                        continue
                    raise error from e
            if not isinstance(result, Iterator) and hasattr(result, "usage_metadata"):
                self.usage.add(result.usage_metadata)
            return result
        raise LLMError("unreachable", hint="")  # pragma: no cover


def classify_error(e: Exception) -> LLMError:
    """Map SDK/network exceptions to LLMError with an actionable hint."""
    if isinstance(e, LLMError):
        return e
    status = getattr(e, "code", None) or getattr(e, "status_code", None)
    text = str(e).strip().splitlines()[0][:300] if str(e).strip() else type(e).__name__
    if status in RETRYABLE_STATUS or type(e).__name__ in {
        "ServerError",
        "ConnectionError",
        "ReadTimeout",
        "TimeoutError",
    }:
        hint = (
            "quota exhausted — wait a minute or lower LLM_MAX_CONCURRENCY"
            if status == 429
            else "transient Vertex error"
        )
        return LLMError(text, hint=hint, retryable=True, status=status if isinstance(status, int) else None)
    if status == 404:
        return LLMError(text, hint="model not found/retired — set GEMINI_MODEL=gemini-2.5-flash", status=404)
    if status in (401, 403) or "invalid_grant" in text or "Reauthentication" in text:
        return LLMError(
            text,
            hint="credentials problem — run `gcloud auth application-default login`",
            status=status if isinstance(status, int) else None,
        )
    if status == 400:
        return LLMError(
            text, hint="request rejected by Gemini — check the schema/tool declarations", status=400
        )
    return LLMError(text, hint="see docs/environment.md → Troubleshooting")


def _text_of(response: Any, *, allow_empty: bool = False) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text)
    if allow_empty:
        return ""
    finish = None
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        finish = getattr(candidates[0], "finish_reason", None)
    raise LLMError(
        f"Gemini returned no text (finish_reason={finish})",
        hint="MAX_TOKENS → raise max_output_tokens or set GEMINI_THINKING_BUDGET=0; "
        "SAFETY → rephrase the prompt",
    )


def _append_text(contents: Contents, extra: str) -> Contents:
    if isinstance(contents, str):
        return f"{contents}\n\n{extra}"
    return [*contents, extra]


def function_declaration(name: str, description: str, parameters: dict[str, Any]) -> Any:
    """Build a FunctionDeclaration from a JSON-schema-like dict (type/properties/required)."""
    from google.genai import types

    return types.FunctionDeclaration(name=name, description=description, parameters=_schema(parameters))


def _schema(node: dict[str, Any]) -> Any:
    from google.genai import types

    type_name = str(node.get("type", "object")).upper()
    kwargs: dict[str, Any] = {"type": getattr(types.Type, type_name)}
    if "description" in node:
        kwargs["description"] = node["description"]
    if "enum" in node:
        kwargs["enum"] = list(node["enum"])
    if "properties" in node:
        kwargs["properties"] = {k: _schema(v) for k, v in node["properties"].items()}
    if "required" in node:
        kwargs["required"] = list(node["required"])
    if "items" in node:
        kwargs["items"] = _schema(node["items"])
    if node.get("nullable"):
        kwargs["nullable"] = True
    return types.Schema(**kwargs)


def function_response(name: str, response: dict[str, Any]) -> Any:
    from google.genai import types

    return types.Content(role="user", parts=[types.Part.from_function_response(name=name, response=response)])


def user_content(text: str) -> Any:
    from google.genai import types

    return types.Content(role="user", parts=[types.Part.from_text(text=text)])
