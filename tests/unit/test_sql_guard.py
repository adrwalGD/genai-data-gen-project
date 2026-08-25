"""F6.1 — the SQL guard: single read-only SELECT only, identifiers lowercased, LIMIT cap + 1."""

import pytest

from genai_data_gen_project.storage.sql_guard import SqlRejected, guard


def test_accepts_select_cte_and_union_and_caps_limit() -> None:
    g = guard("SELECT name, rating FROM Restaurants WHERE rating > 4 ORDER BY rating DESC", limit=500)
    assert g.sql.endswith("LIMIT 501") and g.limit == 500 and g.tables == ["restaurants"]
    cte = guard(
        'WITH top AS (SELECT restaurant_id, count(*) AS n FROM "Orders" GROUP BY 1) '
        "SELECT r.name, top.n FROM top JOIN restaurants r ON r.restaurant_id = top.restaurant_id;",
        limit=100,
    )
    assert (
        cte.sql.startswith("WITH top AS")
        and cte.sql.endswith("LIMIT 101")
        and cte.tables == ["orders", "restaurants", "top"]
    )
    union = guard("SELECT city FROM customers UNION SELECT city FROM restaurants", limit=10)
    assert union.sql.startswith(
        "SELECT * FROM (SELECT city FROM customers UNION SELECT city FROM restaurants) AS q"
    )
    assert union.sql.endswith("LIMIT 11")


def test_user_limits_are_kept_when_smaller_and_lowered_when_larger() -> None:
    assert guard("SELECT * FROM menu LIMIT 5", limit=500).sql.endswith("LIMIT 5")
    assert guard("SELECT * FROM menu LIMIT 5000", limit=500).sql.endswith("LIMIT 501")


def test_quoted_identifiers_are_lowercased() -> None:
    g = guard(
        'SELECT "Name" FROM "Library_Branches" lb JOIN "Employees" e ON e."Employee_ID" = lb."Manager_ID"',
        limit=10,
    )
    assert g.sql == (
        'SELECT "name" FROM "library_branches" AS lb JOIN "employees" AS e '
        'ON e."employee_id" = lb."manager_id" LIMIT 11'
    )
    assert g.tables == ["employees", "library_branches"]


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("", "empty query"),
        ("this is not sql", "not valid SQL|only read-only SELECT"),  # sqlglot may parse garbage leniently
        ("SELECT 1; SELECT 2", "exactly one statement"),
        (
            "INSERT INTO menu (item_name) VALUES ('x')",
            "only read-only SELECT queries are allowed (got INSERT)",
        ),
        ("UPDATE menu SET price = 1", "got UPDATE"),
        ("DELETE FROM menu", "got DELETE"),
        ("DROP TABLE menu", "got DROP"),
        ("CREATE TABLE x (id INT)", "got CREATE"),
        ("ALTER TABLE menu ADD COLUMN x INT", "got ALTER"),
        ("TRUNCATE menu", "got TRUNCATE"),
        ("GRANT ALL ON menu TO public", "read-only SELECT"),
        ("SELECT * INTO copy_of_menu FROM menu", "SELECT ... INTO"),
        ("SELECT * FROM menu FOR UPDATE", "FOR UPDATE"),
        ("SELECT pg_sleep(10)", "pg_sleep() is not allowed"),
        ("SELECT * FROM dblink('host=x', 'select 1') AS t(a int)", "dblink() is not allowed"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file() is not allowed"),
    ],
)
def test_rejections_are_explained(sql: str, fragment: str) -> None:
    pattern = fragment.replace("(", "\\(").replace(")", "\\)").replace("...", "\\.\\.\\.")
    with pytest.raises(SqlRejected, match=pattern):
        guard(sql, limit=100)


@pytest.mark.parametrize(
    "sql",
    [
        "WITH d AS (DELETE FROM orders RETURNING *) SELECT count(*) FROM d",
        "WITH i AS (INSERT INTO reviews (rating) VALUES (5) RETURNING review_id) SELECT * FROM i",
        "WITH u AS (UPDATE menu SET price = 0 RETURNING menu_id) SELECT * FROM u",
        "SELECT nextval('orders_order_id_seq')",
        "SELECT setval('orders_order_id_seq', 1)",
    ],
)
def test_data_modifying_ctes_and_sequence_functions_are_rejected(sql: str) -> None:
    with pytest.raises(SqlRejected, match="not allowed"):
        guard(sql, limit=10)
