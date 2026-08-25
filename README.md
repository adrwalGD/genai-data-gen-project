# GenAI Data Assistant — synthetic data generation & talk-to-your-data

Grid Dynamics GenAI practice project. Generates constraint-valid synthetic data from an SQL DDL with Gemini
(Vertex AI) and lets you query it in natural language (text, tables, plots). Streamlit UI, PostgreSQL, Docker,
Langfuse observability.

Status: under construction — see `PROGRESS.md`. Agent harness: `CLAUDE.md`. Plan: `docs/PLAN.md`.

## Quick start
```bash
gcloud auth application-default login   # griddynamics.com account, project gd-gcp-gridu-genai
make setup && make db-up && make check-env
make run                                 # http://localhost:8501
```
Full instructions: `docs/environment.md`.
