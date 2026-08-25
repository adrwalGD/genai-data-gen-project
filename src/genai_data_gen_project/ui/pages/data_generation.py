"""Data Generation page (F5.1 shell; the form, preview and feedback arrive with F5.2-F5.4)."""

from __future__ import annotations

import streamlit as st

from genai_data_gen_project.ui import state

state.init()

st.header("Data Generation")
st.caption(
    "Upload a DDL schema (.sql, .txt or .ddl), describe the data you need, tune the parameters and click "
    "Generate. Then preview every table, refine it with textual feedback and download CSV/ZIP."
)

with st.container(border=True):
    st.subheader("Schema and instructions")
    st.file_uploader("Upload DDL schema", type=["sql", "txt", "ddl"], key="ddl_upload", disabled=True)
    st.text_area(
        "Instructions (prompt)",
        key=state.SS_INSTRUCTIONS,
        placeholder="Enter your prompt here…",
        disabled=True,
    )
    st.button("Generate", type="primary", disabled=True)
    st.info("The generation form is wired in the next feature (F5.2); this shell only checks navigation.")
