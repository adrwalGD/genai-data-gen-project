"""F1.3 — the emitted DDL must execute in PostgreSQL 17 for every sample schema and enforce constraints."""

import psycopg
import pytest
from psycopg import errors

from genai_data_gen_project.schema.parser import parse_ddl
from genai_data_gen_project.schema.postgres_ddl import emit_foreign_keys, emit_sequence_resets, emit_tables

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("name", ["library", "restaurants", "company"])
def test_sample_schemas_create_and_reflect(
    name: str, sample_ddl: dict[str, str], pg_conn: psycopg.Connection, scratch_schema: str
) -> None:
    schema = parse_ddl(sample_ddl[name])
    with pg_conn.cursor() as cur:
        for stmt in emit_tables(schema, scratch_schema) + emit_foreign_keys(schema, scratch_schema):
            cur.execute(stmt)  # type: ignore[arg-type]
        cur.execute(
            "SELECT table_name, column_name, is_nullable, data_type FROM information_schema.columns "
            "WHERE table_schema = %s ORDER BY table_name, ordinal_position",
            (scratch_schema,),
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT count(*) FROM information_schema.table_constraints "
            "WHERE table_schema = %s AND constraint_type = 'FOREIGN KEY'",
            (scratch_schema,),
        )
        fk_count = cur.fetchone()
    pg_conn.commit()
    reflected = {(t, c): (n, d) for t, c, n, d in rows}
    for table in schema.tables:
        for column in table.columns:
            key = (table.name.lower(), column.name.lower())
            assert key in reflected, f"missing {key}"
            nullable, _ = reflected[key]
            assert (nullable == "YES") is column.nullable, key
    assert len(reflected) == sum(len(t.columns) for t in schema.tables)
    assert fk_count is not None and fk_count[0] == sum(len(t.foreign_keys) for t in schema.tables)


def test_database_enforces_enum_check_and_fk(
    sample_ddl: dict[str, str], pg_conn: psycopg.Connection, scratch_schema: str
) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    with pg_conn.cursor() as cur:
        for stmt in emit_tables(schema, scratch_schema) + emit_foreign_keys(schema, scratch_schema):
            cur.execute(stmt)  # type: ignore[arg-type]
    pg_conn.commit()
    s = scratch_schema
    with pg_conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO "{s}"."restaurants" (restaurant_id, name, address, city, state, cuisine_type) '
            "VALUES (1, 'A', 'x', 'y', 'z', 'Italian')"
        )
        # after explicit ids the identity sequence must be moved past MAX(id) (the loader does this too)
        for stmt in emit_sequence_resets(schema, s):
            cur.execute(stmt)  # type: ignore[arg-type]
        cur.execute(
            f'INSERT INTO "{s}"."restaurants" (name, address, city, state, cuisine_type) '
            "VALUES (%s, %s, %s, %s, %s) RETURNING restaurant_id",
            ("B", "x", "y", "z", "Mexican"),
        )
        assert cur.fetchone() == (2,)  # next identity value after the reset
    pg_conn.commit()
    with pytest.raises(errors.CheckViolation), pg_conn.transaction():
        pg_conn.execute(
            f'INSERT INTO "{s}"."restaurants" (name, address, city, state, cuisine_type) '
            "VALUES ('C', 'x', 'y', 'z', 'Martian')"
        )
    with pytest.raises(errors.CheckViolation), pg_conn.transaction():
        pg_conn.execute(
            f'INSERT INTO "{s}"."customers" (customer_id, first_name, last_name, email) '
            "VALUES (1, 'a', 'b', 'e');"
            f'INSERT INTO "{s}"."reviews" (restaurant_id, customer_id, rating) VALUES (1, 1, 9)'
        )
    with pytest.raises(errors.ForeignKeyViolation), pg_conn.transaction():
        pg_conn.execute(
            f'INSERT INTO "{s}"."menu" (restaurant_id, item_name, price, category) '
            "VALUES (999, 'x', 1, 'Dessert')"
        )
    with pytest.raises(errors.UniqueViolation), pg_conn.transaction():
        pg_conn.execute(
            f"INSERT INTO \"{s}\".\"customers\" (first_name, last_name, email) VALUES ('a', 'b', 'dup'),"
            " ('c', 'd', 'dup')"
        )
    # unquoted, quoted and mixed-case spellings all resolve (lowercase identifiers)
    with pg_conn.cursor() as cur:
        cur.execute(f'SET LOCAL search_path = "{s}"')
        cur.execute("SELECT count(*) FROM Restaurants")
        cur.execute('SELECT count(*) FROM "restaurants"')
        cur.execute("SELECT count(*) FROM restaurants")
        assert cur.fetchone() == (2,)
    pg_conn.rollback()
