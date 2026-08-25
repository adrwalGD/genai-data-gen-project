"""Data Generation page (F5.2): schema input → instructions → parameters → Generate → per-table preview."""

from __future__ import annotations

import streamlit as st

from genai_data_gen_project.generation import engine
from genai_data_gen_project.schema.parser import DDLParseError, parse_ddl
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
            with st.expander("Show DDL"):
                st.code(ddl_text, language="sql")
        except DDLParseError as e:
            st.error(f"The DDL could not be parsed: {e}")
    else:
        st.info("Choose a schema to continue.")

# --- 2. instructions and parameters ---------------------------------------------------------------
with st.container(border=True):
    st.subheader("2 · Instructions and parameters")
    st.text_area(
        "Instructions (prompt)",
        key=state.SS_INSTRUCTIONS,
        placeholder="e.g. Italian and Polish restaurants in Kraków; realistic dish names; reviews in English",
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
