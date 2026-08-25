"""Integration fixtures: a live PostgreSQL from `make db-up`. Fails fast with a fix instruction when down."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest

from genai_data_gen_project.config import Settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def pg_conn(settings: Settings) -> Iterator[psycopg.Connection]:
    try:
        conn = psycopg.connect(settings.database_url, connect_timeout=5)
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.fail(
            f"Postgres not reachable at {settings.database_url}: {exc} — run `make db-up` "
            "(or set DATABASE_URL in .env)"
        )
    yield conn
    conn.close()


@pytest.fixture
def scratch_schema(pg_conn: psycopg.Connection) -> Iterator[str]:
    """A throwaway schema per test, always dropped."""
    name = f"test_{uuid.uuid4().hex[:10]}"
    with pg_conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{name}"')
    pg_conn.commit()
    try:
        yield name
    finally:
        pg_conn.rollback()  # leave any failed transaction before cleaning up
        with pg_conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        pg_conn.commit()
