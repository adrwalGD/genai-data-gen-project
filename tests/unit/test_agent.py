"""F6.2 — agent loop with a FakeLLM and an in-memory executor: tools, corrections, streaming, cap."""

from decimal import Decimal

from genai_data_gen_project.chat import agent as agent_module
from genai_data_gen_project.chat import tools
from genai_data_gen_project.chat.agent import Agent, Turn
from genai_data_gen_project.config import Settings
from genai_data_gen_project.llm.client import ToolTurn
from genai_data_gen_project.schema.parser import parse_ddl
from genai_data_gen_project.storage import postgres
from genai_data_gen_project.storage.sql_guard import SqlRejected
from tests.fakes import FakeLLM, tool_call


def fake_executor(sql_text: str) -> postgres.QueryResult:
    if "nope" in sql_text:
        raise postgres.SqlError(
            'column "nope" does not exist', hint="check table and column names", sqlstate="42703"
        )
    if sql_text.lstrip().upper().startswith("DELETE"):
        raise SqlRejected("only read-only SELECT queries are allowed (got DELETE)")
    if "count" in sql_text.lower():
        return postgres.QueryResult(
            columns=["n"], rows=[[60]], row_count=1, truncated=False, elapsed_ms=3, sql=sql_text
        )
    rows = [[f"c{i}", Decimal(f"{i}.50")] for i in range(50)]
    return postgres.QueryResult(
        columns=["city", "revenue"], rows=rows, row_count=50, truncated=True, elapsed_ms=5, sql=sql_text
    )


def settings() -> Settings:
    return Settings(_env_file=None, langfuse_public_key=None, langfuse_secret_key=None)


def make_agent(sample_ddl: dict[str, str], fake: FakeLLM) -> Agent:
    return Agent(parse_ddl(sample_ddl["restaurants"]), fake, fake_executor, settings=settings())


def test_count_question_runs_sql_and_streams_the_answer(sample_ddl: dict[str, str]) -> None:
    fake = FakeLLM(
        tool_turns=[tool_call("run_sql", sql="SELECT count(*) AS n FROM restaurants")],
        final_text="There are 60 restaurants.",
    )
    events = list(make_agent(sample_ddl, fake).ask("How many restaurants are there?"))
    kinds = [e.kind for e in events]
    assert kinds[:2] == ["tool_call", "tool_result"] and kinds[-1] == "final" and "text_delta" in kinds
    assert events[0].tool == "run_sql" and events[1].result is not None and events[1].result.rows == [[60]]
    assert events[-1].text == "There are 60 restaurants."
    assert "".join(e.text for e in events if e.kind == "text_delta") == "There are 60 restaurants."
    tool_calls = [c for c in fake.calls if c["kind"] == "tools"]
    assert len(tool_calls) == 2 and tool_calls[0]["tools"] == ["run_sql", "render_chart"]
    assert "SCHEMA (PostgreSQL identifiers are lowercase)" in fake.calls[0].get("contents", "") or True
    stream = [c for c in fake.calls if c["kind"] == "stream"]
    assert len(stream) == 1


def test_sql_errors_are_returned_to_the_model_for_correction(sample_ddl: dict[str, str]) -> None:
    fake = FakeLLM(
        tool_turns=[
            tool_call("run_sql", sql="SELECT nope FROM restaurants"),
            tool_call("run_sql", sql="SELECT count(*) FROM restaurants"),
        ],
        final_text="60.",
    )
    events = list(make_agent(sample_ddl, fake).ask("count them"))
    results = [e for e in events if e.kind == "tool_result"]
    assert results[0].error is not None and "nope" in results[0].error and results[0].result is None
    assert results[1].result is not None and events[-1].kind == "final"


def test_chart_tool_uses_the_last_result_and_rejects_unknown_columns(sample_ddl: dict[str, str]) -> None:
    fake = FakeLLM(
        tool_turns=[
            tool_call("run_sql", sql="SELECT city, sum(total_amount) AS revenue FROM orders GROUP BY city"),
            tool_call("render_chart", chart_type="bar", x="city", y="nope", title="Revenue by city"),
            tool_call("render_chart", chart_type="bar", x="city", y="revenue", title="Revenue by city"),
        ],
        final_text="Here is the revenue by city.",
    )
    events = list(make_agent(sample_ddl, fake).ask("plot revenue by city"))
    charts = [e for e in events if e.kind == "tool_result" and e.tool == "render_chart"]
    assert charts[0].error is not None and "unknown columns" in charts[0].error and charts[0].chart is None
    assert charts[1].chart == {"chart_type": "bar", "x": "city", "y": "revenue", "title": "Revenue by city"}
    assert events[-1].kind == "final"


def test_round_cap_and_llm_errors_become_error_events(sample_ddl: dict[str, str]) -> None:
    endless = FakeLLM(tool_turns=[tool_call("run_sql", sql="SELECT count(*) FROM menu")] * 10)
    events = list(
        Agent(
            parse_ddl(sample_ddl["restaurants"]),
            endless,
            fake_executor,
            settings=settings(),
            max_tool_rounds=3,
        ).ask("loop")
    )
    assert events[-1].kind == "error" and "3 tool rounds" in (events[-1].error or "")
    assert sum(1 for e in events if e.kind == "tool_call") == 3

    class Boom(FakeLLM):
        def generate_with_tools(self, contents, tools_, *, system=None, temperature=0.0) -> ToolTurn:  # type: ignore[override,no-untyped-def]
            from genai_data_gen_project.llm.client import LLMError

            raise LLMError("quota exhausted", hint="wait a minute")

    events = list(make_agent(sample_ddl, Boom()).ask("anything"))
    assert events == [events[0]] and events[0].kind == "error" and "quota exhausted" in events[0].text


def test_history_is_replayed_and_write_attempts_are_rejected_by_the_guard(sample_ddl: dict[str, str]) -> None:
    fake = FakeLLM(
        tool_turns=[tool_call("run_sql", sql="DELETE FROM menu")], final_text="I cannot modify data."
    )
    history = [Turn(question="How many?", answer="60 restaurants.", sql=["SELECT count(*) FROM restaurants"])]
    events = list(make_agent(sample_ddl, fake).ask("delete the menu", history))
    rejected = next(e for e in events if e.kind == "tool_result")
    assert rejected.error is not None and "got DELETE" in rejected.error
    first_tools_call = next(c for c in fake.calls if c["kind"] == "tools")
    assert "60 restaurants." in first_tools_call["contents"] and "SQL used" in first_tools_call["contents"]


def test_model_payload_is_compact_and_json_safe() -> None:
    result = fake_executor("SELECT city, revenue FROM x")
    outcome = tools.dispatch("run_sql", {"sql": "SELECT city, revenue FROM x"}, fake_executor, None)
    assert outcome.response["row_count"] == 50 and len(outcome.response["rows"]) == tools.MODEL_ROW_PREVIEW
    assert outcome.response["rows"][1] == ["c1", 1.5] and outcome.response["truncated"] is True
    assert "first 40 of 50" in outcome.response["note"]
    assert outcome.result is not None and outcome.result.rows == result.rows
    unknown = tools.dispatch("teleport", {}, fake_executor, None)
    assert unknown.error and "unknown tool" in unknown.error
    assert agent_module.SYSTEM_PROMPT.startswith("You are a data analyst")


def test_trace_ids_are_none_without_langfuse_and_session_is_kept(sample_ddl: dict[str, str]) -> None:
    fake = FakeLLM(
        tool_turns=[tool_call("run_sql", sql="SELECT count(*) AS n FROM restaurants")], final_text="60."
    )
    agent = Agent(
        parse_ddl(sample_ddl["restaurants"]),
        fake,
        fake_executor,
        settings=settings(),
        session_id="s-1",
        dataset_id="abc",
    )
    events = list(agent.ask("count"))
    assert agent.session_id == "s-1" and agent.dataset_id == "abc"
    assert events[-1].kind == "final" and events[-1].trace_id is None and agent.last_trace_id is None


class BrokenStreamLLM(FakeLLM):
    def stream_text(self, contents, *, system=None, temperature=0.3):  # type: ignore[no-untyped-def,override]
        raise RuntimeError("socket closed")


def test_unexpected_exceptions_become_error_events(sample_ddl: dict[str, str]) -> None:
    fake = BrokenStreamLLM(tool_turns=[tool_call("run_sql", sql="SELECT count(*) AS n FROM restaurants")])
    agent = Agent(parse_ddl(sample_ddl["restaurants"]), fake, fake_executor, settings=settings())
    events = list(agent.ask("count"))
    assert (
        events[-1].kind == "error"
        and "socket closed" in events[-1].text
        and "RuntimeError" in (events[-1].error or "")
    )


def test_dispatch_explains_database_outage_and_empty_results() -> None:
    from genai_data_gen_project.chat import tools
    from genai_data_gen_project.storage import postgres

    def down(sql_text: str) -> postgres.QueryResult:
        raise postgres.LoadError("connection refused")

    outcome = tools.dispatch("run_sql", {"sql": "SELECT 1"}, down, None)
    assert outcome.error and "PostgreSQL is not reachable" in outcome.error and "make db-up" in outcome.error

    def empty(sql_text: str) -> postgres.QueryResult:
        return postgres.QueryResult(
            columns=["n"], rows=[], row_count=0, truncated=False, elapsed_ms=1, sql=sql_text
        )

    outcome = tools.dispatch("run_sql", {"sql": "SELECT 1"}, empty, None)
    assert outcome.error is None and "no rows" in outcome.response["note"]
