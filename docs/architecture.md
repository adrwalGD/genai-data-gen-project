# Architecture

## Layers (dependencies flow strictly downward; `make arch-check` enforces the import rules)
```
ui/            Streamlit pages: render state, call services, never talk to Gemini/Postgres/sqlglot directly
chat/  generation/       use-case services: talk-to-data agent · generation engine, planner, feedback
storage/  schema/  llm/  infrastructure: datasets + Postgres + sql_guard · DDL→IR→DDL · Gemini client wrapper
config.py  observability.py   settings (.env) · Langfuse/OpenInference init + logging
```
Allowed imports: `ui → chat, generation, storage(datasets list/load only), schema(parse for preview), config`;
`chat → llm, storage, schema`; `generation → llm, schema, storage(export/datasets)`; `storage → schema, config`;
`llm → config, observability`. Nothing imports `ui`.

## Data flow — Phase 1 (Data Generation)
```
DDL text ──parse_ddl──▶ Schema IR ──heuristics.plan──▶ base TablePlans
                                   └─(LLM) planner.plan_with_llm(schema, instructions)──▶ merged GenerationPlan
GenerationPlan ──pools.fill_pools (LLM, parallel)──▶ text pools
GenerationPlan + pools ──expander.expand(seed)──▶ {table: DataFrame}  (dependency order, deferred cyclic FKs)
{table: DataFrame} ──validator.validate──▶ ValidationReport (must be ok)
Dataset ──export──▶ CSV/ZIP bytes      Dataset ──storage.datasets.save──▶ data/datasets/<id>/
Dataset ──storage.postgres.load_dataset──▶ schema ds_<id> (tables → COPY → FKs → setval)
feedback text ──feedback.plan_edit (LLM)──▶ EditPlan ──feedback.apply──▶ Dataset' ──validate──▶ report
```

## Data flow — Phases 2–3 (Talk to your data)
```
question ──Agent.ask──▶ Gemini (system prompt = schema_summary + sample values; tools = run_sql, render_chart)
   ├─ function_call run_sql(sql) ──sql_guard──▶ Postgres READ ONLY (search_path ds_<id>) ──▶ rows → function_response
   ├─ function_call render_chart(spec) ──ChartSpec──▶ plotly figure → function_response {"rendered": true}
   └─ final text streamed (generate_content_stream) → UI (st.write_stream)
Each turn = Langfuse trace `talk_to_data_turn` (session_id = Streamlit session id).
```

## Core types
- `schema.models.Schema / Table / Column / ForeignKey / CheckConstraint` — pydantic, dialect-neutral IR.
- `generation.recipes.ColumnRecipe` (discriminated union) + `TablePlan` + `GenerationPlan` — what to generate.
- `generation.engine.Dataset{id, schema, ddl, tables: dict[str, DataFrame], plan, report, created_at}`.
- `generation.validator.ValidationReport{ok, issues: list[Issue]}`.
- `generation.feedback.EditPlan{ops: list[EditOp]}`.
- `chat.charts.ChartSpec`; `chat.agent.AgentEvent` (tool_call | tool_result | text_delta | final | error).
- `llm.client.LLMBackend` protocol — real `GeminiClient` and `tests/fakes.py::FakeLLM` implement it.

## Conventions
- Pure modules (schema, generation.*, sql_guard, charts) have no I/O and are unit-tested offline.
- I/O lives in `storage/` and `llm/`; both take `Settings` explicitly (no hidden globals except `get_settings()` cache).
- Logging via `logging.getLogger(__name__)`; never `print` in `src/`.
- Streamlit session state keys are constants in `ui/state.py`.
- Every LLM prompt lives in the module that owns it as a module-level constant/function (`PLANNER_SYSTEM`, …).
