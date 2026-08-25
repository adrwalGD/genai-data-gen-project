# CLAUDE.md — agent harness for genai-data-gen-project

Conversational data assistant built for a Grid Dynamics GenAI practice: (1) generate constraint-valid,
realistic synthetic data from an uploaded SQL DDL; (2) "Talk to your data" — answer natural-language questions
over that data with text, tables and plots. Spec: `project-spec/PROJECT.md`. Delivery: Streamlit UI + PostgreSQL
in Docker, Gemini via Vertex AI (Google GenAI SDK), Langfuse observability. Audience: the professor demo.

## Hard constraints (MUST / MUST NOT) — read these first
1. Gemini MUST be called only through `src/genai_data_gen_project/llm/client.py` (Vertex AI, ADC auth, project
   `gd-gcp-gridu-genai`). MUST NOT use API keys, `google.generativeai`, or `vertexai.generative_models`.
2. Model id comes from `Settings.gemini_model` (default `gemini-2.5-flash`). `gemini-2.0-flash-001` is retired here
   (verified 404). MUST NOT hardcode `gemini-*` literals outside `config.py`.
3. Machine-consumed LLM output MUST use structured output (`response_json_schema` / Pydantic). User-facing answers
   MUST stream. Text-to-SQL MUST go through function calling (`run_sql`, `render_chart`). See docs/gemini-rules.md.
4. Any SQL that originates from a user or the LLM MUST pass `storage/sql_guard.py` (single SELECT, read-only
   transaction, statement timeout, LIMIT cap). MUST NOT interpolate user text into SQL anywhere else.
5. Secrets live only in `.env` and `project-spec/creds.local` (both gitignored). MUST NOT commit, log or print them.
6. `tests/unit` MUST stay offline and finish in < 60 s. Gemini tests are `@pytest.mark.llm`; Postgres tests are
   `@pytest.mark.integration`. Never add network calls to unit tests.
7. Layer rule (docs/architecture.md): `ui → chat | generation → storage | schema | llm → config | observability`.
   UI pages MUST NOT import `google.genai`, `psycopg`, `sqlglot` or `langfuse`. `make arch-check` enforces this.
8. WIP = 1. Exactly one feature is `active` in `docs/features.md`. A feature becomes `passing` only after its
   verification command was executed in this session and the output is recorded as evidence.
9. MUST NOT refactor, rename or "clean up" outside the active feature. Write the urge into PROGRESS.md → Known Issues.
10. Data integrity (PK unique, FK existence, NOT NULL, ENUM/CHECK, VARCHAR length, decimal scale) MUST be enforced
    in code. The LLM supplies realism and semantics — never integrity.
11. MUST NOT end a session without `make exit-check` green and PROGRESS.md updated (Session protocol below).
12. Keep this file ≤ 200 lines. Detail goes to `docs/` and is linked from here.

## Stack (pinned in pyproject.toml / uv.lock)
Python 3.14 · uv · Streamlit 1.62 · google-genai 2.19 (Vertex AI, `us-central1`) · Langfuse 4.14 +
openinference-instrumentation-google-genai (EU host `https://cloud.langfuse.com`) · PostgreSQL 17 (docker compose) ·
psycopg 3 · sqlglot 30 · pandas 3 · plotly 6 · pydantic 2 + pydantic-settings · faker · pytest 9 · ruff · mypy.

## Repository map
```
CLAUDE.md            this file (router)             PROGRESS.md      current state — read first, update last
DECISIONS.md         design decision log            docs/PLAN.md     milestones M0–M7 + gates
docs/features.md     feature list (harness primitive: id/behavior/verification/state/evidence)
docs/loop.md         work loop, definition of done, gate-verifier protocol
docs/architecture.md layers, modules, data flow     docs/gemini-rules.md   verified Gemini/Langfuse API usage
docs/db-rules.md     DDL→Postgres rules, SQL guard  docs/testing.md        test layers, markers, evidence
docs/ui-rules.md     Streamlit conventions/AppTest  docs/environment.md    setup, env vars, troubleshooting
src/genai_data_gen_project/
  config.py observability.py           settings (.env) · Langfuse init + logging
  llm/        Gemini client wrapper (structured, stream, tools, retries)
  schema/     DDL → IR (sqlglot) · dependency order · Postgres DDL emitter
  generation/ planner · recipes · expander · validator · feedback edits · export · engine
  storage/    dataset persistence · Postgres loader · sql_guard · read-only executor
  chat/       talk-to-data agent (function calling) · tools · charts
  ui/         app.py (st.navigation) · pages/ · state.py · components.py
tests/unit tests/integration tests/e2e   scripts/ (check_env, e2e_smoke, features, arch_check, exit_check)
project-spec/        PROJECT.md, sample DDLs (*.ddl), sample-ui.png, creds.local (gitignored)
```

## Commands — the Makefile is the only entry point (`make help` lists all)
| Task | Command |
|---|---|
| Install + create `.env` | `make setup` (injects Langfuse keys from `project-spec/creds.local` if present) |
| Verify environment (ADC→Vertex, Langfuse, Postgres) | `make db-up && make check-env` |
| Fast offline gate (before every commit) | `make check` = lint + typecheck + arch-check + unit + AppTest |
| Postgres integration tests | `make db-up && make test-int` |
| Real Gemini tests | `make test-llm` (sets `RUN_LLM_TESTS=1`) |
| End-to-end smoke (3 schemas → validate → load) | `make e2e` (offline) · `make e2e-llm` (with Gemini) |
| Everything | `make check-all` |
| Run the app | `make run` (local) · `make docker-up` (compose: postgres + app on :8501) |
| Feature list | `make features` · `uv run python scripts/features.py next|activate ID|pass ID --evidence "..."` |
| Session end gate | `make exit-check` |
| Filter tests | `make test K=parser` |

## Where to look before changing something
- Gemini call shapes, thinking budget, retries, Langfuse spans → docs/gemini-rules.md (all snippets were verified live).
- DDL type mapping, cyclic FKs, dataset schemas `ds_<id>`, read-only SQL → docs/db-rules.md.
- What counts as "done", evidence format, fake-LLM strategy for UI tests → docs/testing.md and docs/loop.md.
- Why something is the way it is → DECISIONS.md (append, never rewrite history).

## Session protocol
**Start (≤ 3 min to executable state):** 1) read PROGRESS.md; 2) `make features` — resume the `active` feature, else
take `scripts/features.py next`; 3) `make check` (and `make check-env` if the environment may have changed);
4) if `make check` is red, making it green is the first task — nothing else starts before that.

**Work loop (docs/loop.md):** activate one feature → implement the smallest change that satisfies its behavior →
run its verification command → paste evidence (command + key output lines, trace ids, commit) → `make check` →
commit (`type(scope): what — why`) → mark `passing` → update PROGRESS.md → next feature. Milestone gates are run by an
independent fresh-context verifier (Agent tool) that only reports PASS/FAIL with output; the milestone closes on PASS.

**End:** `make exit-check` → update PROGRESS.md (Current State, Completed, In Progress, Known Issues, Next Steps,
Session log) → commit. Leave no half-applied change: finish it, or revert it and record why in PROGRESS.md.
