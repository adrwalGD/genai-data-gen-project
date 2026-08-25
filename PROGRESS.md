# Project Progress

Read this first in every session. Update it before every session ends (see CLAUDE.md → Session protocol).
Feature-level state lives in `docs/features.md` (machine-readable, `make features`). This file is the narrative
summary plus anything that does not fit a feature entry.

## Current State
- Milestone: **M2 Deterministic generation core** — F2.1 active. M1 features passing (gate verification running). M0 gate: PASS.
- Latest commit: `dbf2e1a` (feat(generation): F2.2 row expander)
- `make check`: green (ruff, mypy 11 files, arch-check 7 rules, unit 5 passed, e2e 3 passed)
- `make test-int`: 1 passed (PostgreSQL 17.11 via compose) · `make check-env`: all PASS (Vertex 2.2 s, Langfuse trace `f97f543d…`)
- Active feature: F2.3 (validator reporting every violation class)

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
- [x] M1 F1.1 DDL parser → IR (16 tests) · F1.2 generation order + cycle breaking (6) · F1.3 Postgres DDL emitter
      (19 unit + 4 integration: 3 sample schemas created in PostgreSQL 17, constraints enforced) · F1.4 prompt summary (4).
- [x] Harness: CLAUDE.md (88 lines), docs/{PLAN,features,loop,architecture,gemini-rules,db-rules,testing,ui-rules,environment}.md,
      DECISIONS.md, Makefile, scripts/{features.py,arch_check.sh,exit_check.sh,check_env.py}.

## In Progress
- (none)

## Blocked / Known Issues
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
0. M1 gate → independent verifier on the M1 commit; record `Gate M1`.
1. M2: F2.1 recipes + heuristic planner → F2.2 expander → F2.3 validator → F2.4 export/datasets → F2.5 loader →
   F2.6 `scripts/e2e_smoke.py` (`make e2e`, 3 schemas × 1000 rows, clean validator, loaded with FKs).
2. M3 LLM planner/pools/engine; M4 feedback; M5/M6 UI; M7 hardening (docs/PLAN.md).

## Session log (newest first)
- **2026-08-25 (session 1, cont.)** — M0 committed (`38572e8`); independent M0 gate verifier launched; F1.1 DDL
  parser → IR passing (16 tests) and committed; harness hardened after the piped-gate incident; F1.2 generation
  order with cycle breaking passing (6 tests) and committed.
- **2026-08-25 (session 1)** — Research + harness authoring + M0. Verified Vertex/Gemini features, Langfuse host,
  deps on Python 3.14, sqlglot coverage of sample DDLs. Wrote the harness (CLAUDE.md, PROGRESS.md, DECISIONS.md,
  docs/*), Makefile, pyproject/uv.lock, compose + Dockerfile, config/observability modules, harness scripts, first
  unit/integration/e2e tests. `make check`, `make test-int`, `make check-env`, `make exit-check` all green.
