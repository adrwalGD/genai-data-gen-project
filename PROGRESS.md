# Project Progress

Read this first in every session. Update it before every session ends (see CLAUDE.md → Session protocol).
Feature-level state lives in `docs/features.md` (machine-readable, `make features`). This file is the narrative
summary plus anything that does not fit a feature entry.

## Current State
- Milestone: **M6 Talk to your data** — F6.1–F6.2 passing, F6.3 active. M4+M5 gate verifier running. Gates M0–M3: PASS.
- Latest commit: see `git log --oneline -1` (not duplicated here — it drifted twice)
- `make check`: green (counts in the command output; do not hand-copy them here)
- `make test-int`: 1 passed (PostgreSQL 17.11 via compose) · `make check-env`: all PASS (Vertex 2.2 s, Langfuse trace `f97f543d…`)
- Active feature: F6.3 (chart specs to plotly figures)

## Completed
- [x] Spec analysed (`project-spec/PROJECT.md`, 3 sample DDLs, sample UI); harness-engineering lectures 1–14 read.
- [x] Environment facts verified live (2026-08-25): ADC works for Vertex AI; `gemini-2.0-flash-001` returns 404 (retired);
      `gemini-2.5-flash` works incl. structured output (Pydantic + dynamic JSON schema with nullable/enum), streaming,
      function calling (manual + AFC via chats); Langfuse keys are for the EU host and a trace with nested
      GENERATION observation was confirmed via API; full dependency stack imports on Python 3.14; sqlglot parses all
      three sample DDLs (MySQL dialect) including ENUM, CHECK, DEFAULT, AUTO_INCREMENT, table FKs, ALTER TABLE ADD CONSTRAINT.
- [x] M0 F0.1 scaffold + pinned deps (uv.lock, 89 packages) · F0.2 quality gates (`make check`) · F0.3 Postgres compose +
      integration test · F0.4 Settings/observability + `make check-env` · F0.5 feature tooling + `make exit-check`.
- [x] **Gate M0: PASS** 2026-08-25 at `38572e8` — independent verifier on a clean clone: setup/check/db-up/test-int/
      check-env green in 37 s (Vertex 1.5 s, Langfuse trace `1470c49ccc74e0d140c43c961f7b2c83`); 5 harness findings applied.
- [x] **Gate M1: PASS** 2026-08-25 at `de4040a` — independent verifier: IR matched all 186 columns/25 FKs of the 3
      DDLs by independent parsing, DDL executed in PG 17.11 with constraint counts as derived, summary claims hold; 6
      discrepancies applied (TokenError caught, compose project name, fallback/unknown-type notes, docs corrected).
- [x] M1 F1.1 DDL parser → IR (16 tests) · F1.2 generation order + cycle breaking (6) · F1.3 Postgres DDL emitter
      (19 unit + 4 integration: 3 sample schemas created in PostgreSQL 17, constraints enforced) · F1.4 prompt summary (4).
- [x] **Gate M2: PASS** 2026-08-25 at `9b01563` — independent verifier: gate 51 s, 1000 rows/table × 3 schemas clean, DB
      enforces FKs/ENUMs, failure paths exit 1 with fix hints; findings applied (K-filter quoting, exit-check drift, smoke
      traceback, email suffix) and F2.7 consistency rules queued.
- [x] M2 F2.1 recipes + heuristic planner · F2.2 expander · F2.3 validator · F2.4 CSV/ZIP + dataset registry ·
      F2.5 Postgres loader (1000 rows/table × 3 schemas with FKs) · F2.6 `make e2e` offline smoke green.
- [x] M3 F3.1 Gemini client wrapper · F3.2 LLM planner overrides · F3.3 LLM text pools · F3.4 engine + `make e2e-llm`
      (restaurants 200 rows/table with Gemini, Langfuse trace `data_generation`).
- [x] F2.7 consistency rules: expression language with parent lookups + conditionals, aggregate recipes, heuristics for
      subtotal/total/status/registration-date relations, consistency checker in the engine report.
- [x] M4 F4.1 EditPlan model + deterministic applier (filters in the expression language, cascade/SET NULL deletes,
      derived + aggregate recomputation, revalidation, edit history).
- [x] M4 F4.2 natural-language feedback → EditPlan with Gemini (flat draft model, corrective retry incl. dropped-filter
      guard, pool values from Gemini); 3 feedback styles verified live.
- [x] M5 F5.1 Streamlit shell: st.navigation sidebar (Data Generation, Talk to your data), state.py, script pages, AppTest.
- [x] M5 F5.2 Data Generation page: upload/sample/paste schema with parse feedback, instructions, temperature/rows/seed/
      Gemini toggle, Generate with st.status progress, per-table preview + validation summary + details.
- [x] M5 F5.3 per-table feedback: quick-edit box + Submit → Gemini EditPlan applied, preview refreshed, history with ops.
- [x] M5 F5.4 download CSV (table) / ZIP (all) + Save dataset (registry + PostgreSQL, graceful DB-down warning) → visible
      on Talk to your data.
- [x] M5 F5.5 Docker: `make docker-up` builds and runs postgres + app (ADC mount, data volume, healthcheck); health ok;
      in-container generation + save into the compose PostgreSQL verified.
- [x] **Gate M3: PASS** (re-check 2026-08-25 at `5f557d3`): pools yield ×2, batch isolation, 5x row cap all confirmed.
- [x] M6 F6.1 read-only SQL guard (single SELECT/UNION/CTE, denylist, lowercase quoted identifiers, LIMIT cap+1) and
      run_readonly (READ ONLY txn, statement_timeout, search_path ds_<id>, hinted SqlError).
- [x] M6 F6.2 chat agent: manual Gemini function-calling loop (run_sql, render_chart), corrections from SQL errors,
      streamed final answer, history recap, event stream for the UI; live tests (count, top-5 chart).
- [x] **Gate M4: PASS** and **Gate M5: PASS** (2026-08-25 at `f2fd579`): live cross-table feedback applied twice cleanly,
      hand-built typo → EditError with parent columns; all AppTests green; container healthy with DDLs/ADC/DB reachable.
- [x] Harness: CLAUDE.md (88 lines), docs/{PLAN,features,loop,architecture,gemini-rules,db-rules,testing,ui-rules,environment}.md,
      DECISIONS.md, Makefile, scripts/{features.py,arch_check.sh,exit_check.sh,check_env.py}.

## In Progress
- (none)

## Blocked / Known Issues
- UX nits for F7.2 (from the M5 verifier): clear the feedback box after a successful Submit; cache the all-tables ZIP
  bytes per dataset instead of recomputing on every rerun; rebuild the app image after code changes (`make docker-up`).
- **Gate M4: FAIL (narrow)** (2026-08-25 at `5f557d3`): gate commands green, but the feedback 'make all Italian
  restaurants' dishes cost between 30 and 40' → Gemini wrote `parent(restaurant_id).cuisine` (column is cuisine_type) →
  raw KeyError; fix: validate parent attributes in filters (EditError → corrective retry), wrap filter evaluation,
  show parent tables in the prompt, accept `fk.col` as an after_column alias. Fixed; M4 re-verified PASS.
- Lesson (2026-08-25, #3): `get_settings()` is lru-cached, so a test's monkeypatched env was ignored once another test
  had populated the cache — a save-dataset e2e test loaded into the dev Postgres. Autouse fixture now clears the cache.
- **Gate M3: FAIL** (2026-08-25, verifier on `efb0cbe`): (1) Gemini `constant` override `salary='confidential'` crashed generation
  with raw InvalidOperation — now coerced/validated and dropped with a reason, and `_conform` raises ExpansionError;
  (2) text-pool GENERATIONs were orphan Langfuse traces — contextvars now copied into pool workers; (3) text_pool
  overrides dropped `unique`/fallback — inherited; (4) localization — `locale` on faker recipes + prompt rules;
  (5) row counts uncapped — clamped to `MAX_ROWS_PER_TABLE`. Re-verification on `3414453`: all five fixed; narrow FAIL for
  a flaky 300-titles yield and one bad batch discarding a whole pool → fixed (batch isolation, over-request, 5x relative
  row cap). Final M3 verdict is folded into the M4 gate run (budget: the org spend limit tripped once).
- Remaining realism gap (offline mode only): Faker catch_phrase/bs names for pools; the LLM pools cover it when Gemini is on.
- Lesson (2026-08-25, #2): commit `fd9bc71` claimed all M1-verifier fixes but an edit script had aborted midway and the
  chain kept going (exit code unchecked); completed honestly in the follow-up commit. Rule added to docs/loop.md.
- Lesson (2026-08-25): a piped `make check | tail` hid a mypy failure and F1.1 was marked passing on a red gate;
  fixed by amending the commit after a green run and by mechanical gating (`.harness/check.ok` marker required
  by `features.py pass`; see DECISIONS.md). Never pipe gate commands.
- `gemini-2.5-*` models return empty `.text` when `max_output_tokens` is small and thinking is enabled → always pass
  `ThinkingConfig(thinking_budget=0)` (or a deliberate budget) — to be encoded in `llm/client.py` defaults (F3.1).
- `gcloud auth list` shows no account but the ADC file (`~/.config/gcloud/application_default_credentials.json`,
  type `authorized_user`) works. If Vertex calls start failing with 401/invalid_grant, re-run
  `gcloud auth application-default login` (see docs/environment.md).
- `openinference-instrumentation-google-genai` declares `requires_python <3.15` → `requires-python` pinned `<3.15`.
- Streamlit `AppTest` cannot drive `st.file_uploader` → UI must also offer a sample-schema selector / pasted DDL
  text so E2E tests can inject a schema (docs/ui-rules.md).
- `scripts/` are linted but not type-checked by mypy (`packages = ["genai_data_gen_project"]`); acceptable for M0.
- `[project.scripts] genai-data-gen` points at `cli.py`, which imports `ui/app.py` that does not exist until F5.1.

## Next Steps
1. M6 Talk to your data: F6.3 charts → F6.4 page → F6.5 traces → M6 gate.
2. M7 hardening: README/demo script, error UX (incl. verifier nits), `make check-all`, demo dataset, v1.0 tag.
3. M5/M6 UI; M7 hardening (docs/PLAN.md).

## Session log (newest first)
- **2026-08-25 (session 1, cont.)** — M0 committed (`38572e8`); independent M0 gate verifier launched; F1.1 DDL
  parser → IR passing (16 tests) and committed; harness hardened after the piped-gate incident; F1.2 generation
  order with cycle breaking passing (6 tests) and committed.
- **2026-08-25 (session 1)** — Research + harness authoring + M0. Verified Vertex/Gemini features, Langfuse host,
  deps on Python 3.14, sqlglot coverage of sample DDLs. Wrote the harness (CLAUDE.md, PROGRESS.md, DECISIONS.md,
  docs/*), Makefile, pyproject/uv.lock, compose + Dockerfile, config/observability modules, harness scripts, first
  unit/integration/e2e tests. `make check`, `make test-int`, `make check-env`, `make exit-check` all green.
