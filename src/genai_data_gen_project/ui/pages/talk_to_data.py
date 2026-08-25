"""Talk to your data page (F6.4): pick a saved dataset, ask questions, get streamed text, tables, charts."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pandas as pd
import streamlit as st

from genai_data_gen_project.chat.agent import AgentEvent, Turn
from genai_data_gen_project.chat.charts import ChartSpec, ChartSpecError, to_figure
from genai_data_gen_project.ui import services, state

state.init()

st.header("Talk to your data")
st.caption("Ask questions about a saved dataset in plain language — answers come as text, tables and plots.")

saved = services.saved_datasets()
if not saved:
    st.info("No saved datasets yet — generate and save one on the Data Generation page.")
    st.chat_input("Ask a question about your data…", disabled=True)
    st.stop()

labels = {info.label: info for info in saved}
default_index = next(
    (i for i, info in enumerate(saved) if info.id == st.session_state[state.SS_ACTIVE_DATASET_ID]), 0
)
top = st.columns([4, 1, 1])
chosen_label = top[0].selectbox("Dataset", list(labels), index=default_index, key="dataset_label")
chosen = labels[chosen_label]
if chosen.id != st.session_state[state.SS_ACTIVE_DATASET_ID]:
    st.session_state[state.SS_ACTIVE_DATASET_ID] = chosen.id
    st.session_state[state.SS_CHAT] = []
loaded = services.dataset_loaded(chosen.id)
if loaded is False:
    if top[1].button("Load into PostgreSQL", key="load_db", width="stretch"):
        try:
            with st.spinner("Loading…"):
                result = services.load_saved_into_db(chosen.id)
            st.success(f"Loaded {result.total_rows} rows into {result.schema_name}.")
            st.rerun()
        except services.UIError as e:
            st.error(str(e))
    st.warning("This dataset is saved on disk but not loaded into PostgreSQL yet — load it to ask questions.")
elif loaded is None:
    st.error("PostgreSQL is not reachable — run `make db-up` (or check DATABASE_URL).")
if top[2].button("Clear chat", key="clear_chat", width="stretch"):
    st.session_state[state.SS_CHAT] = []
    st.rerun()


# --- history --------------------------------------------------------------------------------------
def render_sql(sql: str, frame: pd.DataFrame | None, error: str | None) -> None:
    with st.expander("SQL" if error is None else "SQL (failed)", expanded=False):
        st.code(sql, language="sql")
        if error:
            st.warning(error)
    if frame is not None:
        st.dataframe(frame, width="stretch", hide_index=True)


def render_chart(spec_args: dict[str, Any], columns: list[str], rows: list[list[Any]]) -> None:
    try:
        fig = to_figure(ChartSpec.from_tool_args(spec_args), columns, rows)
        st.plotly_chart(fig, width="stretch")
    except (ChartSpecError, ValueError) as e:
        st.warning(f"Chart could not be drawn: {e}")


for past in st.session_state[state.SS_CHAT]:
    with st.chat_message("user"):
        st.markdown(past["question"])
    with st.chat_message("assistant"):
        for past_item in past["items"]:
            if past_item["kind"] == "sql":
                frame = (
                    pd.DataFrame(past_item["rows"], columns=past_item["columns"])
                    if past_item.get("columns")
                    else None
                )
                render_sql(past_item["sql"], frame, past_item.get("error"))
            elif past_item["kind"] == "chart":
                render_chart(past_item["spec"], past_item["columns"], past_item["rows"])
        if past.get("error"):
            st.error(past["error"])
        st.markdown(past["answer"])
        if past.get("trace_id"):
            st.caption(f"Langfuse trace: {past['trace_id']}")

# --- new question ---------------------------------------------------------------------------------
question = st.chat_input("Ask a question about your data…", key="question", disabled=loaded is not True)
if question:
    with st.chat_message("user"):
        st.markdown(question)
    history = [
        Turn(
            question=r["question"],
            answer=r["answer"],
            sql=[i["sql"] for i in r["items"] if i["kind"] == "sql"],
        )
        for r in st.session_state[state.SS_CHAT]
    ]
    record: dict[str, Any] = {
        "question": question,
        "answer": "",
        "items": [],
        "error": None,
        "trace_id": None,
    }
    with st.chat_message("assistant"):
        try:
            agent = services.agent_for(chosen.id)
            events = agent.ask(question, history)
            last_result: tuple[list[str], list[list[Any]]] | None = None
            first_text: AgentEvent | None = None
            for event in events:
                if event.kind == "tool_call":
                    continue
                if event.kind == "tool_result" and event.tool == "run_sql":
                    frame = None
                    item: dict[str, Any] = {
                        "kind": "sql",
                        "sql": event.args.get("sql", ""),
                        "error": event.error,
                    }
                    if event.result is not None:
                        frame = pd.DataFrame(event.result.rows, columns=event.result.columns)
                        item.update(
                            columns=event.result.columns, rows=event.result.rows, sql=event.result.sql
                        )
                        last_result = (event.result.columns, event.result.rows)
                        if event.result.truncated:
                            item["error"] = None
                            st.caption(f"Showing the first {event.result.row_count} rows (result truncated).")
                    render_sql(item["sql"], frame, item["error"])
                    record["items"].append(item)
                elif event.kind == "tool_result" and event.tool == "render_chart":
                    if event.chart and last_result is not None:
                        columns, rows = last_result
                        render_chart(event.chart, columns, rows)
                        record["items"].append(
                            {"kind": "chart", "spec": event.chart, "columns": columns, "rows": rows}
                        )
                elif event.kind in {"text_delta", "final", "error"}:
                    first_text = event
                    break

            def stream(
                first: AgentEvent | None, rest: Iterator[AgentEvent], sink: dict[str, Any]
            ) -> Iterator[str]:
                pending = [first] if first is not None else []
                for ev in [*pending, *rest]:
                    if ev.kind == "text_delta":
                        yield ev.text
                    elif ev.kind == "final":
                        sink["answer"] = ev.text
                        sink["trace_id"] = ev.trace_id
                    elif ev.kind == "error":
                        sink["error"] = ev.text or ev.error
                        sink["trace_id"] = ev.trace_id
                        yield ev.text

            streamed = st.write_stream(stream(first_text, events, record))
            record["answer"] = record["answer"] or (
                streamed if isinstance(streamed, str) else "".join(map(str, streamed))
            )
            if record["error"]:
                st.error(record["error"])
            record["trace_id"] = record["trace_id"] or agent.last_trace_id
            if record["trace_id"]:
                st.caption(f"Langfuse trace: {record['trace_id']}")
        except services.UIError as e:
            record["error"] = str(e)
            record["answer"] = record["answer"] or "I could not answer that."
            st.error(str(e))
    st.session_state[state.SS_CHAT].append(record)
