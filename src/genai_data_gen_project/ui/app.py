"""Streamlit entry point: sidebar navigation between "Data Generation" and "Talk to your data".

Run with `make run` (or `streamlit run src/genai_data_gen_project/ui/app.py`). Pages are script files under
`pages/` (not functions) so `streamlit.testing.v1.AppTest.switch_page()` can drive them (docs/ui-rules.md).
"""

from __future__ import annotations

import streamlit as st

from genai_data_gen_project.observability import configure_logging, init_observability
from genai_data_gen_project.ui import state

st.set_page_config(page_title="Data Assistant", page_icon="🗄️", layout="wide")
configure_logging()
init_observability()  # idempotent; tracing stays off without Langfuse keys
state.init()

st.sidebar.title("Data Assistant")
navigation = st.navigation(
    [
        st.Page(
            "pages/data_generation.py", title="Data Generation", icon=":material/database:", default=True
        ),
        st.Page("pages/talk_to_data.py", title="Talk to your data", icon=":material/forum:"),
    ]
)
navigation.run()
