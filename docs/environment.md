# Environment — setup from scratch and troubleshooting

## Prerequisites (verified versions on the dev machine, 2026-08-25)
Python 3.14 (uv installs it), uv 0.11, Docker 29 + Compose v5, gcloud SDK 575, GNU make 4.4, jq.

## First run
```bash
gcloud auth application-default login   # griddynamics.com account; project gd-gcp-gridu-genai (see project-spec/gemini-access-instructions.md)
make setup        # uv sync --all-groups; creates .env from .env.example and injects LANGFUSE_* from project-spec/creds.local if present
make db-up        # postgres:17-alpine, waits for healthy
make check-env    # PASS/FAIL for Python, ADC file, Vertex call, Langfuse auth + trace, Postgres
make check        # offline quality gate
make run          # http://localhost:8501
```
Docker (full stack): `make docker-up` — mounts `~/.config/gcloud` read-only into the app container as ADC
(`GOOGLE_APPLICATION_CREDENTIALS=/gcloud/application_default_credentials.json`).

## Environment variables (`.env`, loaded by pydantic-settings; see `.env.example`)
| Var | Default | Notes |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | `gd-gcp-gridu-genai` | Vertex project |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | |
| `GEMINI_MODEL` | `gemini-2.5-flash` | 2.0-flash is retired (404) |
| `GEMINI_THINKING_BUDGET` | `0` | >0 enables thinking (slower) |
| `LLM_MAX_CONCURRENCY` | `4` | parallel Gemini calls |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | — | from `project-spec/creds.local`; tracing off when missing |
| `LANGFUSE_BASE_URL` | `https://cloud.langfuse.com` | EU region (US host rejects these keys) |
| `DATABASE_URL` | `postgresql://datagen:datagen@localhost:5432/datagen` | compose app uses host `postgres` |
| `DATA_DIR` | `data` | datasets registry |
| `DEFAULT_ROWS_PER_TABLE` / `MAX_ROWS_PER_TABLE` | `100` / `5000` | UI defaults/limits |
| `SQL_ROW_LIMIT` / `SQL_TIMEOUT_MS` | `500` / `15000` | read-only executor |

## Troubleshooting (symptom → cause → fix)
- `404 Publisher model ... gemini-2.0-flash-001 was not found` → model retired → use `gemini-2.5-flash` (default).
- `response.text is None` / empty structured output → thinking consumed the token budget → `thinking_budget=0` or raise `max_output_tokens`.
- `401 ... invalid_grant` / `Reauthentication is needed` → ADC expired → `gcloud auth application-default login`.
- `UserWarning: ... end user credentials ... without a quota project` → harmless; silence with
  `gcloud auth application-default set-quota-project gd-gcp-gridu-genai`.
- `429 RESOURCE_EXHAUSTED` → lower `LLM_MAX_CONCURRENCY`, smaller batches; client retries with backoff.
- `psycopg.OperationalError: connection refused` → `make db-up`; in Docker use host `postgres`.
- `Permission denied: 'data/datasets/...'` on a local `make run` → the directory was created root-owned by an old app
  image → `docker run --rm -v $PWD/data:/d alpine chown -R $(id -u):$(id -g) /d`; images built since F7.2 run as UID 1000.
- Langfuse `auth_check()` False → keys belong to EU project "My Project" → `LANGFUSE_BASE_URL=https://cloud.langfuse.com`.
- `openinference-instrumentation-google-genai` requires Python `<3.15` → stay on 3.14 until it updates.
- `gcloud auth list` shows no accounts but calls work → ADC file exists independently of gcloud CLI login; fine.
