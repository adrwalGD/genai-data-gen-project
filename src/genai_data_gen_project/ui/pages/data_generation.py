"""Data Generation page (F5.2): schema input → instructions → parameters → Generate → per-table preview."""

from __future__ import annotations

import logging

import streamlit as st

from genai_data_gen_project.generation import engine, export
from genai_data_gen_project.schema.parser import DDLParseError, error_context, parse_ddl
from genai_data_gen_project.schema.summary import schema_summary
from genai_data_gen_project.ui import services, state

state.init()
cfg = services.settings()

st.header("Data Generation")
st.caption(
    "Upload a DDL schema (.sql, .txt or .ddl), describe the data you need, tune the parameters and click "
    "Generate. Then preview every table, refine it with textual feedback and download CSV/ZIP."
)

# --- 1. schema ------------------------------------------------------------------------------------
with st.container(border=True):
    st.subheader("1 · Schema")
    source = st.radio(
        "Schema source", ["Upload file", "Sample schema", "Paste DDL"], horizontal=True, key="schema_source"
    )
    if source == "Upload file":
        uploaded = st.file_uploader("Upload DDL schema", type=["sql", "txt", "ddl"], key="ddl_upload")
        if uploaded is not None:
            st.session_state[state.SS_DDL_TEXT] = uploaded.getvalue().decode("utf-8", errors="replace")
            st.session_state[state.SS_DDL_NAME] = uploaded.name
    elif source == "Sample schema":
        sample = st.selectbox("Sample schema", list(services.SAMPLE_SCHEMAS), key="sample_schema")
        try:
            st.session_state[state.SS_DDL_TEXT] = services.sample_ddl(sample)
            st.session_state[state.SS_DDL_NAME] = f"sample: {sample}"
        except services.UIError as e:
            st.error(str(e))
    else:
        pasted = st.text_area("Paste DDL", key="ddl_paste", height=180, placeholder="CREATE TABLE ...")
        if pasted.strip():
            st.session_state[state.SS_DDL_TEXT] = pasted
            st.session_state[state.SS_DDL_NAME] = "pasted DDL"

    ddl_text = st.session_state[state.SS_DDL_TEXT]
    schema = None
    if ddl_text.strip():
        try:
            schema = parse_ddl(ddl_text)
            names = ", ".join(schema.table_names)
            st.success(
                f"Parsed {len(schema.tables)} tables from {st.session_state[state.SS_DDL_NAME]}: {names}"
            )
            for note in schema.notes:
                st.warning(note)
            with st.expander("Tables, columns and keys"):
                st.code(schema_summary(schema), language="text")
            with st.expander("Show DDL"):
                st.code(ddl_text, language="sql")
        except DDLParseError as e:
            st.error(f"The DDL could not be parsed: {e}")
            snippet = error_context(ddl_text, str(e))
            if snippet:
                st.code(snippet, language="text")
    else:
        st.info("Choose a schema to continue.")

# --- 2. instructions and parameters ---------------------------------------------------------------
with st.container(border=True):
    st.subheader("2 · Instructions and parameters")
    st.text_area(
        "Instructions (prompt)",
        key=state.SS_INSTRUCTIONS,
        placeholder=(
            "e.g. family-run Italian and Indian restaurants in New Jersey; "
            "realistic dish names; reviews in English"
        ),
        height=90,
    )
    with st.expander("Advanced parameters", expanded=True):
        col_a, col_b, col_c, col_d = st.columns(4)
        col_a.slider("Temperature", 0.0, 2.0, step=0.05, key=state.SS_TEMPERATURE)
        col_b.number_input(
            "Rows per table",
            min_value=1,
            max_value=cfg.max_rows_per_table,
            step=10,
            key=state.SS_ROWS_PER_TABLE,
        )
        col_c.number_input("Seed", min_value=0, step=1, key=state.SS_SEED)
        col_d.toggle(
            "Use Gemini", key=state.SS_LLM_ENABLED, help="Off = offline heuristics + Faker (no LLM calls)"
        )
    generate = st.button("Generate", type="primary", key="generate", disabled=schema is None)

if generate and schema is not None:
    log: list[str] = []
    st.session_state[state.SS_LAST_ERROR] = None
    try:
        llm = services.llm_backend()
        params = services.GenerationParams(
            ddl=ddl_text,
            instructions=st.session_state[state.SS_INSTRUCTIONS],
            rows_per_table=int(st.session_state[state.SS_ROWS_PER_TABLE]),
            temperature=float(st.session_state[state.SS_TEMPERATURE]),
            seed=int(st.session_state[state.SS_SEED]),
        )
        with st.status("Generating…", expanded=True) as status:

            def report_progress(message: str) -> None:
                log.append(message)
                status.write(message)

            dataset = services.run_generation(params, llm, progress=report_progress)
            status.update(
                label=f"Generated {dataset.total_rows} rows in {len(dataset.tables)} tables", state="complete"
            )
        st.session_state[state.SS_DATASET] = dataset
        st.session_state[state.SS_SELECTED_TABLE] = dataset.schema.table_names[0]
        st.session_state[state.SS_GENERATION_LOG] = log
        st.session_state[state.SS_EDIT_LOG] = []
    except services.UIError as e:
        st.session_state[state.SS_LAST_ERROR] = str(e)
        st.error(str(e))
    except Exception as e:  # F7.2: never show a raw traceback
        logging.getLogger(__name__).exception("generation failed")
        st.session_state[state.SS_LAST_ERROR] = str(e)
        st.error(f"Unexpected error while generating ({type(e).__name__}: {str(e)[:200]}) — see the app log.")

# --- 3. preview -----------------------------------------------------------------------------------
dataset = st.session_state[state.SS_DATASET]
if dataset is not None:
    with st.container(border=True):
        st.subheader("3 · Data preview")
        report = engine.report_of(dataset)
        if report is not None and report.ok:
            st.success(report.summary())
        elif report is not None:
            st.error(report.summary())
        top = st.columns([3, 1])
        options = dataset.schema.table_names
        current = st.session_state[state.SS_SELECTED_TABLE]
        index = options.index(current) if current in options else 0
        table_name = top[0].selectbox("Table", options, index=index, key="preview_table")
        st.session_state[state.SS_SELECTED_TABLE] = table_name
        frame = dataset.tables[table_name]
        top[1].metric("Rows", len(frame))
        st.dataframe(frame.head(200), width="stretch", hide_index=True)
        if len(frame) > 200:
            st.caption(f"Showing the first 200 of {len(frame)} rows.")
        # --- 4. feedback on the selected table -------------------------------------------------------------
        st.markdown(f"**Refine `{table_name}` with feedback**")
        feedback_cols = st.columns([5, 1])
        feedback_text = feedback_cols[0].text_input(
            "Quick edit instructions",
            key="feedback_text",
            placeholder="e.g. set ratings below 3 to 3 · add 20 rows · regenerate emails as name@example.org",
            label_visibility="collapsed",
        )
        submit = feedback_cols[1].button("Submit", key="feedback_submit", type="primary", width="stretch")
        if submit:
            try:
                llm = services.llm_backend()
                with st.spinner("Interpreting the feedback and updating the table…"):
                    new_dataset, result, plan = services.run_feedback(
                        dataset, table_name, feedback_text, llm, seed=int(st.session_state[state.SS_SEED])
                    )
                st.session_state[state.SS_DATASET] = new_dataset
                st.session_state[state.SS_EDIT_LOG].append(
                    {
                        "table": result.table,
                        "feedback": feedback_text,
                        "summary": result.summary,
                        "affected_rows": result.affected_rows,
                        "rows": f"{result.rows_before} → {result.rows_after}",
                        "ok": result.report.ok,
                        "issues": [str(i) for i in result.report.issues[:5]],
                        "ops": [op.model_dump(mode="json", exclude_none=True) for op in plan.ops],
                    }
                )
                st.rerun()
            except services.UIError as e:
                st.error(str(e))
        for entry in reversed(st.session_state[state.SS_EDIT_LOG]):
            icon = "✅" if entry["ok"] else "⚠️"
            with st.expander(
                f"{icon} {entry['table']}: {entry['feedback']} — {entry['affected_rows']} rows affected"
            ):
                st.write(entry["summary"])
                st.caption(f"rows {entry['rows']}")
                if entry["issues"]:
                    st.warning("\n".join(entry["issues"]))
                st.json(entry["ops"], expanded=False)
        # --- 5. download and save ---------------------------------------------------------------------------
        st.markdown("**Download or save**")
        save_cols = st.columns([1, 1, 2, 1])
        save_cols[0].download_button(
            f"Download CSV ({table_name})",
            data=export.to_csv_bytes(dataset, table_name),
            file_name=export.csv_filename(dataset, table_name),
            mime="text/csv",
            key="download_csv",
            width="stretch",
        )
        save_cols[1].download_button(
            "Download ZIP (all tables)",
            data=export.to_zip_bytes(dataset),
            file_name=export.zip_filename(dataset),
            mime="application/zip",
            key="download_zip",
            width="stretch",
        )
        dataset_name = save_cols[2].text_input(
            "Dataset name", value=dataset.name, key="dataset_name", label_visibility="collapsed"
        )
        if save_cols[3].button("Save dataset", key="save_dataset", width="stretch"):
            try:
                with st.spinner("Saving and loading into PostgreSQL…"):
                    outcome = services.save_dataset(dataset, name=dataset_name)
                st.session_state[state.SS_ACTIVE_DATASET_ID] = outcome.dataset_id
                total = sum(outcome.row_counts.values())
                if outcome.loaded:
                    st.success(
                        f"Saved dataset `{outcome.dataset_id}` ({total} rows) and loaded it into PostgreSQL "
                        "— it is now available in Talk to your data."
                    )
                else:
                    st.warning(
                        f"Saved dataset `{outcome.dataset_id}` to disk, but PostgreSQL is not available: "
                        f"{outcome.warning}"
                    )
            except OSError as e:
                st.error(f"Could not save the dataset: {e}")
        notes = dataset.params.get("notes", [])
        if notes or st.session_state[state.SS_GENERATION_LOG]:
            with st.expander("Generation details"):
                mode = (
                    "Gemini " + str(dataset.params.get("model")) if dataset.params.get("llm") else "offline"
                )
                seed, temp = dataset.params.get("seed"), dataset.params.get("temperature")
                st.write(f"Mode: {mode} · seed {seed} · temperature {temp}")
                if dataset.params.get("trace_id"):
                    st.caption(f"Langfuse trace: {dataset.params['trace_id']}")
                for line in st.session_state[state.SS_GENERATION_LOG]:
                    st.text(line)
                for note in notes:
                    st.text(f"note: {note}")
