FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 STREAMLIT_SERVER_HEADLESS=true STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
WORKDIR /app

# dependency layer (cached until pyproject/lock change)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# application
COPY src ./src
COPY project-spec/library_mgm_schema.ddl project-spec/restrurants_schema.ddl project-spec/company_employee_schema.ddl ./project-spec/
RUN uv sync --frozen --no-dev

# run as a regular user (UID 1000 = the usual desktop user) so files written to the bind-mounted ./data
# stay writable for a local `make run` (F7.2: a root-owned data/datasets broke local saves)
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app && mkdir -p /app/data && chown -R app:app /app
USER app

EXPOSE 8501
CMD ["uv", "run", "--no-sync", "streamlit", "run", "src/genai_data_gen_project/ui/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
