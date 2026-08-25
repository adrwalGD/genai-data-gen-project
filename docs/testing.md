# Testing standards and definition of done

## Layers (run in this order; do not advance while a lower layer is red)
| Layer | What | Command | Network |
|---|---|---|---|
| L1 static | ruff lint+format, mypy, architecture rules | `make lint typecheck arch-check` | no |
| L2 unit | pure modules; fakes for LLM; < 60 s total | `make test` (`tests/unit`) | no |
| L2 ui | Streamlit `AppTest` with `FakeLLM`, no DB unless marked | `make test-ui` (`tests/e2e`) | no |
| L2 integration | Postgres via compose | `make db-up && make test-int` (`tests/integration`, `@pytest.mark.integration`) | DB |
| L2 llm | real Gemini on Vertex, small prompts | `make test-llm` (`@pytest.mark.llm`, needs `RUN_LLM_TESTS=1`) | Vertex |
| L3 system | full pipeline: DDL → generate → validate → load (→ query) | `make e2e` / `make e2e-llm` | DB (+Vertex) |
| Gate | everything | `make check` (offline) · `make check-all` | all |

## Rules
- Unit tests never touch the network or the filesystem outside `tmp_path`. Gemini is replaced by
  `tests/fakes.py::FakeLLM` (implements `llm.client.LLMBackend`; returns canned structured outputs keyed by the
  Pydantic model / schema title, and scripted tool calls for the agent).
- `llm` tests are skipped unless `RUN_LLM_TESTS=1`; they assert *shape and constraints* (valid plan, counts, enum
  membership), never exact wording. Keep them ≤ 3 calls each; temperature 0 where possible.
- `integration` tests fail fast with `pytest.fail("Postgres not reachable at <url> — run `make db-up`")` if the DB is
  down; each test uses a throwaway schema (`test_<uuid>`) and drops it.
- Every bug fix adds a regression test at the lowest layer that reproduces it.
- Negative tests are mandatory for the validator (one issue per violation class) and the SQL guard (each rejected class).
- Test names read as behavior: `test_expander_fills_deferred_cyclic_fks_in_second_pass`.

## Evidence format (docs/features.md → evidence)
`<command> → <key output line(s)>[; langfuse trace <id>]` — the commit message carries the feature id (`feat(schema): F1.1 …`), so the hash is recoverable via `git log --grep F1.1`.
e.g. `make test K=parser → 16 passed in 0.29s; make check green`

## Error messages are for agents too (lecture 10)
Failures thrown by scripts/tests must say what failed, why, and the fix: `"Postgres not reachable at
postgresql://...: connection refused — run `make db-up` (or set DATABASE_URL)"`.
