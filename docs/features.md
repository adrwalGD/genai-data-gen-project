# Feature list (harness primitive)

Machine-readable: `scripts/features.py {list|next|validate|activate ID|pass ID --evidence "..."|set ID STATE}`.
Rules: states are `not_started | active | blocked | passing`; **at most one `active`**; `passing` requires
non-empty evidence (verification command actually run + key output / commit / trace id); `passing` is terminal.
Each entry: behavior (observable), verification (executable), state, evidence. Keep entries session-sized.

## M0 — Initialization

### F0.1 — Project scaffold and pinned dependencies
- milestone: M0
- state: passing
- behavior: `make setup` on a clean clone installs the pinned stack (google-genai, streamlit, langfuse, openinference-instrumentation-google-genai, psycopg, sqlglot, pandas, plotly, pydantic, pydantic-settings, faker; dev: pytest, ruff, mypy) into `.venv` and creates `.env` from `.env.example` (Langfuse keys injected from `project-spec/creds.local` when present).
- verification: `make setup && uv run python -c "import streamlit, google.genai, langfuse, psycopg, sqlglot, pandas, plotly, faker, pydantic_settings; print('deps ok')"`
- evidence: make setup → Installed 87 packages, uv.lock 89 packages, .env created with Langfuse keys injected; uv run python -c 'import streamlit, google.genai, langfuse, psycopg, sqlglot, pandas, plotly, faker, pydantic_settings' → deps ok (2026-08-25)

### F0.2 — Quality gates run green
- milestone: M0
- state: passing
- behavior: `make check` runs ruff (lint + format), mypy, executable architecture rules, offline unit tests and offline AppTest/e2e tests, and exits 0 with at least one real test in each of `tests/unit` and `tests/e2e`.
- verification: `make check`
- evidence: make check → ruff 'All checks passed!', 23 files formatted, mypy 'no issues found in 11 source files', arch-check OK (7 rules), tests/unit 5 passed, tests/e2e 3 passed (2026-08-25)

### F0.3 — PostgreSQL via docker compose
- milestone: M0
- state: passing
- behavior: `make db-up` starts Postgres 17 and waits until healthy; `make test-int` connects using `DATABASE_URL` and asserts the server version; `make db-down` stops it.
- verification: `make db-up && make test-int`
- evidence: make db-up → Container datagen-postgres Healthy (postgres:17-alpine); make test-int → 1 passed (PostgreSQL 17.11, scratch schema create/insert/count) (2026-08-25)

### F0.4 — Settings and observability skeleton with environment check
- milestone: M0
- state: passing
- behavior: `Settings` loads `.env`; `init_observability()` enables Langfuse + google-genai instrumentation only when keys exist; `make check-env` prints PASS/FAIL per check (Python, ADC file, Vertex `generate_content` with configured model, Langfuse `auth_check` + a flushed trace named `check_env`, Postgres connection) with a fix instruction for every FAIL and exits non-zero on failure.
- verification: `make check-env`
- evidence: make check-env → PASS python 3.14.5; PASS adc; PASS vertex gemini-2.5-flash → 'pong' in 2.2s; PASS langfuse trace check_env id=f97f543d4d25f121bccbef301a6de81a; PASS postgres 17.11; 'check-env: all PASS' (2026-08-25)

### F0.5 — Harness tooling (feature list, exit gate)
- milestone: M0
- state: passing
- behavior: `scripts/features.py validate` enforces the state rules; `make features` lists id/state/milestone; `make exit-check` runs `make check`, validates the feature list, fails on debug leftovers (`breakpoint()`, `pdb`, `TODO` without a feature id; stray `print(` in `src/` fails via `make check` → arch-check R4/ruff T20) and when PROGRESS.md was neither modified nor part of HEAD.
- verification: `make features && make exit-check`
- evidence: make features → 34 features listed with states; make exit-check → make check green + 'exit-check: OK' (features valid, PROGRESS.md touched, no debug leftovers) (2026-08-25)

## M1 — Schema engine

### F1.1 — DDL parser produces a complete IR for all sample schemas
- milestone: M1
- state: passing
- behavior: `schema.parser.parse_ddl(text)` returns a `Schema` IR for `library_mgm`, `restrurants`, `company_employee` DDLs with every table/column, types (INT, VARCHAR(n), TEXT, DATE, DATETIME, DECIMAL(p,s), BOOLEAN, ENUM values), nullability (`NULL`/`NOT NULL`), defaults (`0`, `'Active'`, `CURRENT_TIMESTAMP`, `TRUE`), AUTO_INCREMENT, UNIQUE, CHECK expressions, inline + table-level + `ALTER TABLE ADD CONSTRAINT` foreign keys; raises `DDLParseError` with line context on garbage input.
- verification: `make test K=parser`
- evidence: make test K=parser → 16 passed, 5 deselected in 0.29s (3 sample schemas: tables/columns/types/nullability incl. DATE NULL/defaults/AUTO_INCREMENT/UNIQUE/CHECK/ENUM/inline+table+ALTER FKs; MySQL+Postgres variants; 9 actionable error cases); make check green (2026-08-25)

### F1.2 — Generation order with cycle breaking
- milestone: M1
- state: passing
- behavior: `schema.order.generation_order(schema)` returns tables so that every non-nullable FK target precedes its source; cycles (library: Branches↔Employees↔Departments) are broken at nullable FKs which are reported as `deferred_fks` to fill in a second pass; a cycle made only of NOT NULL FKs raises `UnsatisfiableSchemaError`.
- verification: `make test K=order`
- evidence: make test K=order → 6 passed (restaurants/company DAGs; library cycles deferred at Library_Branches.manager_id and Employees.department_id; self-reference deferred; mixed cycle defers nullable side; NOT NULL cycle → UnsatisfiableSchemaError naming both FKs); make check green (2026-08-25)

### F1.3 — Postgres DDL emitter creates all sample schemas in a real database
- milestone: M1
- state: passing
- behavior: `schema.postgres_ddl.emit_tables(schema)` + `emit_foreign_keys(schema)` produce DDL that executes in Postgres 17 for all three samples inside a fresh schema (`ENUM`→`VARCHAR`+`CHECK`, `AUTO_INCREMENT`→identity, `DATETIME`→`TIMESTAMP`, identifiers lowercased and quoted so LLM SQL works quoted or unquoted, CHECK/ENUM/FK violations rejected by the database), and `information_schema` reflects every column.
- verification: `make test K=postgres_ddl && make test-int K=postgres_ddl`
- evidence: make test K=postgres_ddl → 19 passed (type map, identity/defaults/unique/enum-check column SQL, quoted-lowercase CHECK columns, FK naming/ON DELETE/dedupe, sequence resets, case-collision error); make test-int K=postgres_ddl → 4 passed (3 sample schemas create in PostgreSQL 17 with all columns/nullability/FK counts reflected; identity reset → next id 2; DB rejects ENUM/CHECK/FK/UNIQUE violations; unquoted/quoted/mixed-case names resolve); make check green (2026-08-25)

### F1.4 — Compact schema summary for prompts
- milestone: M1
- state: passing
- behavior: `schema.summary.schema_summary(schema)` renders tables, columns, types, PK/FK/UNIQUE/CHECK/ENUM/NULL info in ≤ 25 lines per table; the library schema summary is under 6,000 characters.
- verification: `make test K=summary`
- evidence: make test K=summary → 4 passed (library summary < 6000 chars, ≤ 25 lines/table, order + deferred FK lines, PK/identity/NOT NULL/UNIQUE/DEFAULT/FK→/ENUM(|)/CHECK rendering, lowercase Postgres mode, composite FK/UNIQUE/table CHECK); make check green (2026-08-25)

## M2 — Deterministic generation core

### F2.1 — Column recipes and offline heuristic planner
- milestone: M2
- state: passing
- behavior: `generation.recipes` defines typed recipes (int/decimal range, sequence, enum weighted, faker provider, pattern, date/datetime window, text pool ref, fk, derived, constant) and `generation.heuristics.plan(schema, rows_per_table)` assigns a valid recipe to every column of the three sample schemas without an LLM (emails→email, `*_date`→date windows, `zip_code`→postcode, `price/salary`→decimal ranges, ENUM→weighted, FK→fk).
- verification: `make test K=heuristics`
- evidence: make test K=heuristics → 16 passed (plans for 3 sample schemas complete + validate_plan clean; PK→sequence, FK→fk incl. deferred manager_id, ENUM weighted, CHECK bounds → rating 1..5, isbn13 unique, email pattern from first/last name, due_date after loan_date 14–28d, death_date after birth with null_ratio 0.85, available_quantity derived, id-like columns without FK → 1..100; validate_plan negative cases; recipe invariants + JSON schema); make check green (2026-08-25)

### F2.2 — Row expander honors every constraint class
- milestone: M2
- state: passing
- behavior: `generation.expander.expand(schema, plan, seed)` yields N rows per table with sequential PKs, FK values that exist in parent tables (incl. deferred cyclic FKs filled in a second pass), unique columns unique, NOT NULL respected, null ratios applied to nullable columns, ENUM/CHECK/VARCHAR-length/DECIMAL-scale satisfied, dates within windows, deterministic for a fixed seed.
- verification: `make test K=expander`
- evidence: make test K=expander → 10 passed (3 sample schemas × 150 rows: NOT NULL, int/Decimal scale+precision, ENUM, VARCHAR length, date/datetime types, unique sets, FK containment incl. deferred cyclic FKs; library semantics: sequential PKs, 300 unique isbn/emails, due_date 14–28d after loan, return after loan, death after birth, available ≤ quantity; restaurants CHECK-bounded ratings, boolean ratio, unique license; determinism by seed; self-reference never self; text pools + fallback; pattern tokens; unique exhaustion + empty-parent errors); make check green (2026-08-25)

### F2.3 — Validator reports every violation class
- milestone: M2
- state: not_started
- behavior: `generation.validator.validate(schema, tables)` returns `ValidationReport(ok, issues)` detecting PK duplicates, dangling FKs, NOT NULL violations, ENUM/CHECK violations, VARCHAR overflow, DECIMAL scale/precision overflow, invalid dates; clean on expander output; each negative case produces exactly one issue with examples.
- verification: `make test K=validator`
- evidence: —

### F2.4 — CSV/ZIP export and dataset persistence
- milestone: M2
- state: not_started
- behavior: `generation.export.to_csv_bytes/to_zip_bytes` produce one UTF-8 CSV per table (ISO-8601 dates, NULL as empty) and a ZIP of all; `storage.datasets.save/load/list/delete` persist `data/datasets/<id>/{manifest.json, schema.ddl, tables/*.csv}` and round-trip a dataset losslessly.
- verification: `make test K=export or datasets`
- evidence: —

### F2.5 — Postgres loader with post-load FK constraints
- milestone: M2
- state: not_started
- behavior: `storage.postgres.load_dataset(dataset)` creates schema `ds_<id>`, tables, COPYs rows, adds FK constraints, resets identity sequences; row counts match; loading the same dataset twice replaces it; works for all three samples at 1000 rows/table.
- verification: `make test-int K=loader`
- evidence: —

### F2.6 — Offline end-to-end smoke across all sample schemas
- milestone: M2
- state: not_started
- behavior: `scripts/e2e_smoke.py --schema all --rows 1000` generates with the heuristic planner, validates (must be clean), loads into Postgres, prints per-table counts and total time, exits 0; any violation or DB error exits 1 with an actionable message.
- verification: `make db-up && make e2e`
- evidence: —

## M3 — LLM-powered generation

### F3.1 — Gemini client wrapper (structured, streaming, tools, retries, tracing)
- milestone: M3
- state: not_started
- behavior: `llm.client.GeminiClient` exposes `generate_structured(schema_or_model, prompt, *, temperature, system)`, `stream_text(...)`, `generate_with_tools(...)`; uses Vertex ADC, settings model, thinking budget default 0; retries 429/5xx with exponential backoff (max 5); enforces `max_concurrency`; every call appears in Langfuse as a GENERATION when tracing is enabled.
- verification: `make test-llm K=client`
- evidence: —

### F3.2 — LLM planner merges over heuristics
- milestone: M3
- state: not_started
- behavior: `generation.planner.plan_with_llm(schema, instructions, rows_per_table, llm)` asks for a `GenerationPlan` via structured output and merges it over the heuristic plan (LLM may change strategies, ranges, enum weights, null ratios, per-table row counts, text-pool briefs); invalid LLM suggestions are dropped with a logged reason, never crash; instructions like "salaries 50k–90k, only Polish cities" are reflected in the plan.
- verification: `make test K=planner && make test-llm K=planner`
- evidence: —

### F3.3 — LLM text pools with parallel batches and deduplication
- milestone: M3
- state: not_started
- behavior: `generation.pools.fill_pools(plan, llm)` requests unique values for each text-pool brief in parallel batches, deduplicates, tops up short pools, and falls back to faker on failure; requesting 300 unique book titles returns ≥ 300 distinct strings ≤ VARCHAR length.
- verification: `make test K=pools && make test-llm K=pools`
- evidence: —

### F3.4 — Engine end-to-end with Gemini
- milestone: M3
- state: not_started
- behavior: `generation.engine.generate(ddl, instructions, rows_per_table, temperature, seed, llm)` returns a validated `Dataset`; `scripts/e2e_smoke.py --llm --schema restaurants --rows 200` completes with a clean validator report, and the run is visible in Langfuse as trace `data_generation` with nested GENERATIONs.
- verification: `make e2e-llm E2E_ARGS="--schema restaurants --rows 200"`
- evidence: —

## M4 — Feedback edits

### F4.1 — EditPlan model and deterministic applier
- milestone: M4
- state: not_started
- behavior: `generation.feedback.EditPlan` supports `set_values`, `regenerate_column`, `add_rows`, `delete_rows`, `update_pool`; `apply(plan, dataset, schema)` keeps PK uniqueness, FK integrity (cascade or re-sample), revalidates and returns the new dataset + report; deleting parent rows cascades or fails with a clear error per plan option.
- verification: `make test K=feedback`
- evidence: —

### F4.2 — Natural-language feedback becomes an EditPlan via Gemini
- milestone: M4
- state: not_started
- behavior: `generation.feedback.plan_edit(table, feedback, schema, llm)` turns "set all ratings below 3 to 3", "regenerate emails as firstname.lastname@example.org", "add 15 cancelled orders" into valid `EditPlan`s (structured output) that, once applied, satisfy the request and validate clean.
- verification: `make test-llm K=feedback`
- evidence: —

## M5 — UI: Data Generation

### F5.1 — App shell with sidebar navigation
- milestone: M5
- state: not_started
- behavior: `streamlit run src/genai_data_gen_project/ui/app.py` shows title "Data Assistant", sidebar pages "Data Generation" and "Talk to your data" via `st.navigation`; both pages render without errors with empty state and no LLM/DB configured.
- verification: `make test-ui K=shell`
- evidence: —

### F5.2 — Data Generation form, generation, and per-table preview
- milestone: M5
- state: not_started
- behavior: page offers DDL file upload (.sql/.txt/.ddl), a sample-schema selector and a paste box; instructions text area; advanced params (temperature 0–2, rows per table, seed, "use LLM" toggle); clicking Generate runs the engine with progress in `st.status`, then shows a table selector + `st.dataframe` preview + validation summary; with the fake/offline backend the AppTest drives selector → Generate → preview.
- verification: `make test-ui K=generation_page`
- evidence: —

### F5.3 — Per-table textual feedback with Submit
- milestone: M5
- state: not_started
- behavior: under the preview, a feedback text box + Submit applies an `EditPlan` to the selected table (LLM or fake backend), refreshes the preview and validation summary, and keeps an edit history visible.
- verification: `make test-ui K=feedback_ui`
- evidence: —

### F5.4 — Download and save dataset for Talk-to-data
- milestone: M5
- state: not_started
- behavior: "Download CSV (table)" and "Download ZIP (all)" buttons serve the exports; "Save dataset" persists to `data/datasets/<id>` and loads into Postgres schema `ds_<id>`, then shows the dataset id; saved datasets are listed on the Talk-to-data page.
- verification: `make test-ui K=save_dataset && make test-int K=loader`
- evidence: —

### F5.5 — Dockerized app + database
- milestone: M5
- state: not_started
- behavior: `make docker-up` builds the image (python:3.14-slim + uv, ADC directory mounted read-only, `data/` volume) and starts postgres + app; `curl -fsS localhost:8501/_stcore/health` returns `ok`; the app can generate offline data and save it to the compose Postgres.
- verification: `make docker-up && curl -fsS localhost:8501/_stcore/health`
- evidence: —

## M6 — UI: Talk to your data

### F6.1 — Read-only SQL guard and executor
- milestone: M6
- state: not_started
- behavior: `storage.sql_guard.guard(sql, limit)` accepts exactly one SELECT/UNION/CTE, rejects DML/DDL/multi-statement/`INTO`/`pg_sleep`-style calls with a reason, enforces a LIMIT cap; `storage.postgres.run_readonly(dataset_id, sql)` executes in a READ ONLY transaction with `statement_timeout` and `search_path=ds_<id>` returning columns + rows.
- verification: `make test K=sql_guard && make test-int K=readonly`
- evidence: —

### F6.2 — Chat agent with function calling and streamed answers
- milestone: M6
- state: not_started
- behavior: `chat.agent.Agent(dataset, llm).ask(question, history)` yields events (`tool_call(run_sql|render_chart)`, `tool_result`, `text_delta`, `final`) using a manual Gemini function-calling loop (≤ 6 tool rounds), system prompt with schema summary + sample values, and streams the final answer; "How many restaurants are there?" yields a `run_sql` call and an answer containing the true count.
- verification: `make test K=agent && make test-llm K=agent`
- evidence: —

### F6.3 — Chart specs to plotly figures
- milestone: M6
- state: not_started
- behavior: `chat.charts.ChartSpec` (bar/line/pie/scatter/histogram, x, y, color, title, agg) → `to_figure(spec, rows)` builds a plotly figure; invalid columns raise `ChartSpecError` with the available columns listed.
- verification: `make test K=charts`
- evidence: —

### F6.4 — Talk-to-data page
- milestone: M6
- state: not_started
- behavior: page has a dataset selector (saved datasets), chat history (`st.chat_message`), `st.chat_input`, streamed answers (`st.write_stream`), result tables, plotly charts, a "Show SQL" expander per turn, and a clear-conversation button; with a fake agent the AppTest drives one question to a table + chart.
- verification: `make test-ui K=talk_page`
- evidence: —

### F6.5 — Per-turn Langfuse traces
- milestone: M6
- state: not_started
- behavior: each question produces a Langfuse trace `talk_to_data_turn` with `session_id` = Streamlit session id, tags `[talk-to-data]`, nested GENERATION spans for every Gemini call and a span per tool execution; the trace id is shown in a small caption in the UI.
- verification: `make test-llm K=trace`
- evidence: —

## M7 — Hardening & presentation

### F7.1 — README and demo walkthrough
- milestone: M7
- state: not_started
- behavior: README covers purpose, architecture (diagram), prerequisites (gcloud ADC), setup (`make setup`), run (local + Docker), verification commands, a 5-minute demo script for the professor and screenshots; a fresh reader can run the app following only the README.
- verification: independent verifier follows README from a clean clone and reports PASS
- evidence: —

### F7.2 — Actionable errors and UX polish
- milestone: M7
- state: not_started
- behavior: DDL parse errors show line context; Vertex 429/5xx show a retry hint; empty query results and guard rejections are explained in the chat; long generations show per-table progress; no raw tracebacks reach the UI.
- verification: `make test-ui K=errors`
- evidence: —

### F7.3 — Final full gate and demo dataset
- milestone: M7
- state: not_started
- behavior: `make check-all` is green; a demo dataset (restaurants, 1000 rows/table, LLM-generated) is saved and queryable; git tag `v1.0` created.
- verification: `make check-all`
- evidence: —
