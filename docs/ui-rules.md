# Streamlit UI conventions

- Entry: `src/genai_data_gen_project/ui/app.py` → `st.set_page_config(page_title="Data Assistant", layout="wide")`,
  `st.navigation([st.Page("pages/data_generation.py", title="Data Generation", icon=":material/database:"),
  st.Page("pages/talk_to_data.py", title="Talk to your data", icon=":material/forum:")])` rendered in the sidebar with
  the title "Data Assistant" (matches `project-spec/sample-ui.png`). Pages are *script files* (not functions) so
  `AppTest.switch_page("pages/talk_to_data.py")` can drive them.
- Pages contain layout + event handling only; they call `generation.engine`, `generation.feedback`, `storage.datasets`,
  `chat.agent`. No `google.genai`, `psycopg`, `sqlglot`, `langfuse` imports in `ui/` (`make arch-check`).
- Session state keys are constants in `ui/state.py` (`SS_DDL_TEXT`, `SS_DATASET`, `SS_SELECTED_TABLE`, `SS_CHAT`,
  `SS_ACTIVE_DATASET_ID`, `SS_LLM_ENABLED`, …). Initialise with `state.init()` at the top of every page.
- Services are cached with `@st.cache_resource` (Gemini client, Langfuse init) — never cache datasets in
  `cache_data` (they are per-session; keep them in `session_state`).
- Long work runs inside `with st.status("Generating…", expanded=True) as status:` with per-table `status.write()`.
- Streaming answers: `st.write_stream(generator)`; tool results are rendered as they arrive (SQL in an expander,
  tables with `st.dataframe`, charts with `st.plotly_chart(fig, use_container_width=True)`).
- Previews: `st.dataframe(df.head(200))`, plus row count and validation summary (`st.success` / `st.warning`).
- Downloads: `st.download_button` with bytes from `generation.export`.
- DDL input has three paths — file upload (.sql/.txt/.ddl), sample-schema selectbox (the three `project-spec/*.ddl`),
  pasted text — because `AppTest` cannot drive `st.file_uploader`. UI tests use the selectbox / session_state.
- `AppTest`: `at = AppTest.from_file("src/genai_data_gen_project/ui/app.py", default_timeout=30)`; set
  `at.session_state[...]` before `at.run()`; multipage: `at.switch_page("...")` then `at.run()`; inject the fake
  backend by setting `at.session_state[SS_LLM_BACKEND] = FakeLLM(...)` (pages resolve the backend from state first).
- Errors: never show raw tracebacks — catch known exceptions (`DDLParseError`, `SqlRejected`, `LLMError`) and render
  `st.error(message)` with a hint; unknown exceptions → `st.exception` only when `settings.debug` is true.
