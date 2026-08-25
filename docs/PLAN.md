# Implementation Plan — milestones and gates

Each milestone has a **goal**, **deliverables**, and a **gate**: commands an independent verifier runs from a fresh
context. A milestone is closed only when the gate passes (docs/loop.md). Features inside a milestone are tracked in
`docs/features.md` and executed one at a time (WIP = 1). Order matters: every milestone leaves a runnable, tested
system so that the professor demo can be given from any closed milestone onward.

| # | Milestone | Why in this order | Gate (independent verifier) |
|---|-----------|-------------------|-----------------------------|
| M0 | Initialization | Lecture 6: infrastructure before features. Everything later relies on `make check`. | `make setup && make check && make db-up && make test-int && make check-env` all green from a clean clone |
| M1 | Schema engine | DDL → IR is the foundation of both generation and Postgres loading. Pure Python, offline. | `make check && make db-up && make test-int` (all 3 sample schemas create in Postgres) |
| M2 | Deterministic generation core | Proves integrity for 1000 rows/table before any LLM cost. Offline E2E. | `make e2e` (3 schemas × 1000 rows → validator clean → loaded with FKs) |
| M3 | LLM-powered generation | Adds realism + user instructions + temperature on top of a proven core. | `make test-llm K=generation` + `make e2e-llm` + Langfuse trace id recorded |
| M4 | Feedback edits | Spec: "modify the data through textual feedback". Needs M3 planner types. | `make test K=feedback` + `make test-llm K=feedback` (3 feedback styles) |
| M5 | UI — Data Generation tab | First visible deliverable; wires M1–M4. Dockerized run. | `make test-ui` + `make docker-up` → `curl :8501/_stcore/health` = ok + manual run notes |
| M6 | UI — Talk to your data | Phases 2–3: NL → SQL (function calling), tables, plots, streaming, traces. | `make test K=chat` + `make test-llm K=chat` + `make test-ui` |
| M7 | Hardening & presentation | README, error UX, demo dataset, final full gate. | `make check-all` green; README walkthrough reproduced by verifier |

## M0 — Initialization (no business code)
Deliverables: pinned `pyproject.toml` + `uv.lock`; `Makefile` (single command surface); `docker-compose.yml`
(Postgres 17 healthy, `app` profile); `Dockerfile` (python:3.14-slim + uv); `.env.example`; `config.py`
(pydantic-settings); `observability.py` (Langfuse + OpenInference, optional); `scripts/check_env.py` (actionable
PASS/FAIL for ADC→Vertex, Langfuse, Postgres); `scripts/features.py`, `scripts/arch_check.sh`,
`scripts/exit_check.sh`; first unit/integration/e2e tests; harness docs. Acceptance = "can start, can test, can see
progress, can pick next step" (lecture 6).

## M1 — Schema engine (`schema/`)
- IR (`models.py`): `Schema{tables}`, `Table{name, columns, primary_key, foreign_keys, checks, uniques}`,
  `Column{name, type: ColumnType, length, precision, scale, enum_values, nullable, default, auto_increment, unique, check}`.
- `parser.py`: sqlglot (`read="mysql"`, fallback `postgres`) → IR. Handles inline constraints, table-level FK/UNIQUE/
  CHECK/PK, `ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY`, comments, `NULL`/`NOT NULL`, `DEFAULT CURRENT_TIMESTAMP`.
- `order.py`: FK graph → generation order; cycles broken at nullable FKs (deferred "second-pass" FK fill list).
- `postgres_ddl.py`: IR → Postgres DDL (docs/db-rules.md); `emit_tables()` (no FKs) + `emit_foreign_keys()`.
- `summary.py`: compact schema text for prompts (table, columns, types, keys, checks) ≤ ~1.5k tokens for 7 tables.

## M2 — Deterministic generation core (`generation/`, `storage/`)
- `recipes.py`: `ColumnRecipe` union (int_range, decimal_range, sequence, enum_weighted, faker(provider, kwargs),
  pattern/template, date_window, datetime_window, text_pool(ref), fk(ref), derived(expr), constant, null_only) + `TablePlan`.
- `heuristics.py`: offline planner from names/types (`email`→faker.email, `*_date`→date_window, `zip_code`→postcode…).
- `expander.py`: rows for N with PK sequences, FK sampling honoring order & second-pass cyclic FKs, unique enforcement
  (retry/suffix), null ratios, ENUM/CHECK/VARCHAR/DECIMAL clamping, deterministic seed.
- `validator.py`: `ValidationReport{ok, issues[table, column, rule, count, examples]}` covering every constraint class.
- `export.py`: CSV per table (UTF-8, ISO dates) + ZIP; `storage/datasets.py`: `data/datasets/<id>/{manifest.json,
  schema.ddl, tables/*.csv}` list/load/delete; `storage/postgres.py`: create `ds_<id>` schema, COPY, add FKs, setval.
- `scripts/e2e_smoke.py`: `--schema all --rows 1000 [--llm]` → generate → validate → load → row counts → exit code.

## M3 — LLM-powered generation (`llm/`, `generation/planner.py`, `generation/pools.py`)
- `llm/client.py`: `GeminiClient(settings)` with `generate_structured(schema|model, prompt, temperature)`,
  `stream_text(...)`, `generate_with_tools(...)`; thinking budget default 0; retries with backoff on 429/5xx;
  bounded concurrency (`ThreadPoolExecutor(max_workers=settings.llm_max_concurrency)`); Langfuse spans.
- `planner.py`: schema summary + user instructions → `GenerationPlan` (structured), merged over heuristics
  (LLM may override strategies, ranges, weights, null ratios, row counts per table, text-pool briefs).
- `pools.py`: text-pool briefs → unique values in parallel batches (dedupe, size guarantees, fallbacks).
- `engine.py`: `generate(ddl, instructions, rows_per_table, temperature, seed, llm: LLMBackend | None)` → `Dataset`
  with `ValidationReport`; `LLMBackend` protocol + `FakeLLM` (tests) → UI tests stay offline.

## M4 — Feedback edits (`generation/feedback.py`)
- `EditPlan` (structured): ops `set_values(filter, column, value|recipe)`, `regenerate_column(column, recipe)`,
  `add_rows(n, overrides)`, `delete_rows(filter)`, `update_pool(column, brief)`; `apply(plan, dataset)` keeps PK/FK
  integrity (cascade-aware deletes, FK re-sampling), then revalidates. LLM turns free text → `EditPlan` with the
  table's schema + 5 sample rows as context.

## M5 — UI: Data Generation tab (`ui/`)
- `app.py` (`st.navigation`, sidebar: Data Generation, Talk to your data); `pages/data_generation.py`: DDL upload
  (.sql/.txt/.ddl) **or** sample-schema selector **or** pasted DDL; instructions text area; advanced params
  (temperature slider, rows per table, seed); Generate → `st.status` progress; per-table preview (`st.selectbox` +
  `st.dataframe`); per-table feedback text + Submit; validation report; Download CSV/ZIP; "Save dataset" (persist +
  load into Postgres) so it appears in Talk-to-data. Docker: `docker compose --profile app up`.

## M6 — UI: Talk to your data (`chat/`, `ui/pages/talk_to_data.py`)
- `storage/sql_guard.py` + read-only executor; `chat/tools.py` (`run_sql`, `render_chart`); `chat/agent.py` (system
  prompt with schema summary + sample values, manual function-calling loop, streamed final answer, bounded history);
  `chat/charts.py` (`ChartSpec` → plotly). Page: dataset selector, `st.chat_message` history, `st.chat_input`,
  `st.write_stream`, result tables, plots, "Show SQL" expander, Langfuse per-turn traces (session id = Streamlit
  session).

## M7 — Hardening & presentation
- README (setup, run, architecture diagram, demo script, screenshots), actionable errors (rate limit, bad DDL, empty
  result), final `make check-all`, saved demo dataset, tag `v1.0`.

## Risks and mitigations
- Vertex rate limits / 429 on parallel pool generation → bounded concurrency + exponential backoff + smaller batches.
- ADC token expiry → `make check-env` first; `docs/environment.md` re-login steps.
- Cyclic FKs (library schema) → nullable-FK cycle breaking + post-load FK constraints.
- Empty LLM text due to thinking → thinking budget 0 default (verified).
- `AppTest` cannot upload files → sample-schema selector/pasted DDL path used by UI tests.
- Python 3.14 wheel gaps → all deps verified importable on 3.14.4 (2026-08-25); pin `<` next-major bounds.
- Tools + JSON mode conflict in Gemini → chart via tool call, not response schema.
