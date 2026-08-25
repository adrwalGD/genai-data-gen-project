import psycopg
import pytest

pytestmark = pytest.mark.integration


def test_postgres_version_and_scratch_schema(pg_conn: psycopg.Connection, scratch_schema: str) -> None:
    with pg_conn.cursor() as cur:
        cur.execute("SELECT version()")
        row = cur.fetchone()
        assert row is not None and row[0].startswith("PostgreSQL 17"), row
        cur.execute(f'CREATE TABLE "{scratch_schema}".t (id INT PRIMARY KEY)')
        cur.execute(f'INSERT INTO "{scratch_schema}".t VALUES (1)')
        cur.execute(f'SELECT count(*) FROM "{scratch_schema}".t')
        count = cur.fetchone()
        assert count is not None and count[0] == 1
    pg_conn.rollback()
