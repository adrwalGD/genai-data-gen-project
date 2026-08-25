"""Talk to your data page (F5.1 shell; dataset selector and chat arrive with F6.4)."""

from __future__ import annotations

import streamlit as st

from genai_data_gen_project.storage import datasets
from genai_data_gen_project.ui import state

state.init()

st.header("Talk to your data")
st.caption("Ask questions about a saved dataset in plain language — answers come as text, tables and plots.")

saved = datasets.list_datasets()
if not saved:
    st.info("No saved datasets yet — generate and save one on the Data Generation page.")
else:
    st.selectbox("Dataset", [info.label for info in saved], key="dataset_label")
st.chat_input("Ask a question about your data…", disabled=True)
