# Single command surface for humans and agents. `make help` lists targets. Recipes use '>' instead of TAB.
.RECIPEPREFIX := >
.DEFAULT_GOAL := help
SHELL := /bin/bash
UV ?= uv
COMPOSE ?= docker compose
PY := $(UV) run python
K ?=
PYTEST_K := $(if $(K),-k "$(K)",)
E2E_ARGS ?= --schema all --rows 1000

.PHONY: help setup env lint fmt typecheck arch-check test test-ui test-int test-llm check check-all features \
        db-up db-down db-reset db-shell check-env run e2e e2e-llm docker-up docker-down exit-check

help: ## list targets
> @grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

setup: env ## install dependencies (uv sync) and create .env
> $(UV) sync --all-groups

env: ## create .env from .env.example (once) and inject LANGFUSE_* from project-spec/creds.local
> @if [ ! -f .env ]; then cp .env.example .env && echo "created .env from .env.example"; fi
> @if [ -f project-spec/creds.local ]; then \
>   grep -vE '^LANGFUSE_(PUBLIC|SECRET)_KEY=' .env > .env.tmp && grep -E '^LANGFUSE_(PUBLIC|SECRET)_KEY=' project-spec/creds.local >> .env.tmp && mv .env.tmp .env && echo "injected Langfuse keys from project-spec/creds.local"; \
>   else echo "project-spec/creds.local not found — fill LANGFUSE_* in .env manually (tracing is optional)"; fi

lint: ## ruff lint + format check
> $(UV) run ruff check src tests scripts
> $(UV) run ruff format --check src tests scripts

fmt: ## auto-format and auto-fix
> $(UV) run ruff format src tests scripts
> $(UV) run ruff check --fix src tests scripts

typecheck: ## mypy
> $(UV) run mypy

arch-check: ## executable architecture rules (imports, model literals, prints)
> bash scripts/arch_check.sh

test: ## unit tests (offline, < 60 s)   e.g. make test K=parser
> $(UV) run pytest tests/unit -q -x --timeout=60 $(PYTEST_K)

test-ui: ## Streamlit AppTest / offline e2e tests
> $(UV) run pytest tests/e2e -q -x --timeout=180 $(PYTEST_K)

test-int: ## integration tests (needs: make db-up)
> $(UV) run pytest tests/integration -q -x --timeout=600 -m integration $(PYTEST_K)

test-llm: ## real Gemini tests (needs ADC)
> RUN_LLM_TESTS=1 $(UV) run pytest tests -q -x --timeout=900 -m llm $(PYTEST_K)

check: lint typecheck arch-check test test-ui ## fast offline gate — run before every commit

check-all: check test-int test-llm e2e ## full gate (DB + Vertex)

features: ## feature list states
> $(PY) scripts/features.py list

db-up: ## start postgres and wait until healthy
> $(COMPOSE) up -d --wait postgres

db-down: ## stop containers (keeps data)
> $(COMPOSE) --profile app down

db-reset: ## stop and delete the database volume
> $(COMPOSE) --profile app down -v

db-shell: ## psql into the dev database
> $(COMPOSE) exec postgres psql -U datagen -d datagen

check-env: ## verify ADC→Vertex, Langfuse, Postgres with actionable messages
> $(PY) scripts/check_env.py

run: ## run the Streamlit app locally
> $(UV) run streamlit run src/genai_data_gen_project/ui/app.py

e2e: ## offline end-to-end smoke: generate → validate → load   (E2E_ARGS="--schema all --rows 1000")
> $(PY) scripts/e2e_smoke.py $(E2E_ARGS)

e2e-llm: ## end-to-end smoke with Gemini
> $(PY) scripts/e2e_smoke.py --llm $(E2E_ARGS)

docker-up: ## build and run postgres + app in docker (app on :8501)
> $(COMPOSE) --profile app up --build -d --wait

docker-down: ## stop the docker stack
> $(COMPOSE) --profile app down

exit-check: check ## session-end gate: check + feature list valid + no debug leftovers + PROGRESS.md touched
> bash scripts/exit_check.sh
