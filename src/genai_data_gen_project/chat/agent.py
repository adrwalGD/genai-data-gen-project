"""Talk-to-data agent (F6.2): question → Gemini function calling (run_sql / render_chart) → streamed answer.

Manual loop (docs/gemini-rules.md): every round asks Gemini with the tools declared and AFC disabled; tool
calls are dispatched through `chat.tools` and fed back as function responses; when a round returns no calls
the final answer is produced with a real streaming call so the UI shows tokens as they arrive. Events describe
everything the UI renders: SQL + tables, chart specs, text deltas, the final text, or an error.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from ..config import Settings, get_settings
from ..llm.client import LLMBackend, LLMError, function_response, user_content
from ..schema.models import Schema
from ..schema.summary import schema_summary
from ..storage import postgres
from . import tools

EventKind = Literal["tool_call", "tool_result", "text_delta", "final", "error"]


@dataclass
class AgentEvent:
    kind: EventKind
    text: str = ""
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    result: postgres.QueryResult | None = None
    chart: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class Turn:
    question: str
    answer: str
    sql: list[str] = field(default_factory=list)


SYSTEM_PROMPT = """You are a data analyst answering questions about ONE PostgreSQL dataset in plain language.
Rules:
- Never guess facts about the data: call run_sql and answer from its rows. Use exact lowercase table/column
  names from the schema below; aggregate in SQL; add ORDER BY and LIMIT for top-N questions; never modify
  data.
- If run_sql returns an error, read the hint and try a corrected query once or twice; then explain the
  problem.
- When the user asks for a chart/plot/graph or a comparison that benefits from one, first run_sql for the
  data (one row per category or time bucket), then call render_chart with columns from that result.
- Answer concisely in the user's language: the key numbers first, then one or two sentences of context.
  Mention when a result was truncated. Do not paste SQL into the answer (the interface shows it).

SCHEMA (PostgreSQL identifiers are lowercase):
{schema}
"""


class Agent:
    def __init__(
        self,
        schema: Schema,
        llm: LLMBackend,
        executor: tools.Executor,
        *,
        settings: Settings | None = None,
        max_tool_rounds: int = 6,
        temperature: float = 0.0,
    ) -> None:
        self.llm = llm
        self.executor = executor
        self.settings = settings or get_settings()
        self.max_tool_rounds = max_tool_rounds
        self.temperature = temperature
        self.system = SYSTEM_PROMPT.format(schema=schema_summary(schema, lowercase=True))

    @classmethod
    def for_dataset(
        cls, dataset_id: str, schema: Schema, llm: LLMBackend, settings: Settings | None = None
    ) -> Agent:
        settings = settings or get_settings()
        return cls(schema, llm, tools.make_executor(dataset_id, settings), settings=settings)

    def ask(self, question: str, history: list[Turn] | None = None) -> Iterator[AgentEvent]:
        """Yield events for one question; the last event is `final` (with the full answer) or `error`."""
        contents: list[Any] = []
        for turn in (history or [])[-6:]:
            contents.append(user_content(turn.question))
            recap = turn.answer if not turn.sql else f"{turn.answer}\n(SQL used: {'; '.join(turn.sql)})"
            contents.append(_model_text(recap))
        contents.append(user_content(question))
        last_result: postgres.QueryResult | None = None
        try:
            for _round in range(self.max_tool_rounds):
                reply = self.llm.generate_with_tools(
                    contents, tools.TOOLS, system=self.system, temperature=self.temperature
                )
                if not reply.calls:
                    break
                if reply.content is not None:
                    contents.append(reply.content)
                for call in reply.calls:
                    yield AgentEvent("tool_call", tool=call.name, args=call.args)
                    outcome = tools.dispatch(call.name, call.args, self.executor, last_result)
                    if outcome.result is not None:
                        last_result = outcome.result
                    chart = (
                        outcome.response.get("spec")
                        if call.name == "render_chart" and not outcome.error
                        else None
                    )
                    yield AgentEvent(
                        "tool_result",
                        tool=call.name,
                        args=call.args,
                        result=outcome.result,
                        chart=chart,
                        error=outcome.error,
                        text=outcome.error or "",
                    )
                    contents.append(function_response(call.name, outcome.response))
            else:
                yield AgentEvent(
                    "error",
                    error=f"stopped after {self.max_tool_rounds} tool rounds without a final answer",
                    text="I could not finish answering — please rephrase or make the question more specific.",
                )
                return
            pieces: list[str] = []
            for delta in self.llm.stream_text(
                contents, system=self.system, temperature=max(self.temperature, 0.2)
            ):
                pieces.append(delta)
                yield AgentEvent("text_delta", text=delta)
            yield AgentEvent("final", text="".join(pieces).strip())
        except LLMError as e:
            yield AgentEvent("error", error=str(e), text=f"Gemini failed: {e.message} — {e.hint}")


def _model_text(text: str) -> Any:
    from google.genai import types

    return types.Content(role="model", parts=[types.Part.from_text(text=text)])
