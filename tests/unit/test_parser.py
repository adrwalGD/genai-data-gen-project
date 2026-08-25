"""F1.1 — DDL parser produces a complete IR for the sample schemas and common variants."""

import pytest

from genai_data_gen_project.schema.models import ColumnType
from genai_data_gen_project.schema.parser import DDLParseError, parse_ddl

# --- sample schemas -----------------------------------------------------------------------------------------


def test_library_schema_tables_and_alter_fk(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["library"])
    assert schema.source_dialect == "mysql"
    assert schema.table_names == [
        "Authors", "Publishers", "Books", "Library_Branches", "Library_Members",
        "Book_Inventory", "Book_Loans", "Employees", "Departments",
    ]  # fmt: skip
    books = schema.table("Books")
    assert books.primary_key == ["book_id"]
    assert books.column("book_id").auto_increment and not books.column("book_id").nullable
    isbn = books.column("isbn")
    assert (isbn.type, isbn.length, isbn.unique, isbn.nullable) == (ColumnType.VARCHAR, 20, True, False)
    fmt = books.column("format")
    assert fmt.type is ColumnType.ENUM and fmt.enum_values == [
        "Hardcover",
        "Paperback",
        "E-book",
        "Audiobook",
    ]
    assert fmt.max_length == len("Audiobook")
    assert [(fk.columns, fk.ref_table, fk.ref_columns) for fk in books.foreign_keys] == [
        (["author_id"], "Authors", ["author_id"]),
        (["publisher_id"], "Publishers", ["publisher_id"]),
    ]
    # FK added later via ALTER TABLE ... ADD CONSTRAINT
    branches = schema.table("Library_Branches")
    (fk,) = branches.foreign_keys
    assert fk.name == "FK_Library_Branches_ManagerID"
    assert (fk.columns, fk.ref_table, fk.ref_columns) == (["manager_id"], "Employees", ["employee_id"])
    assert branches.fk_is_nullable(fk) is True
    # self-referencing cycle Departments.manager_id -> Employees, Employees.department_id -> Departments
    assert schema.dependencies_of("Departments") == ["Employees"]
    assert set(schema.dependencies_of("Employees")) == {"Departments", "Library_Branches"}
    assert {t.name for t, _ in schema.references_of("Employees")} == {"Library_Branches", "Departments"}


def test_library_types_defaults_and_nullability(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["library"])
    loans = schema.table("Book_Loans")
    fine = loans.column("fine_amount")
    assert (fine.type, fine.precision, fine.scale, fine.default) == (ColumnType.DECIMAL, 10, 2, "0")
    assert loans.column("loan_status").default == "'Checked Out'"
    assert loans.column("loan_date").type is ColumnType.DATETIME and not loans.column("loan_date").nullable
    assert loans.column("return_date").nullable is True
    assert loans.column("due_date").type is ColumnType.DATE
    members = schema.table("Library_Members")
    assert members.column("account_status").default == "'Active'"
    assert members.column("biography" if members.has_column("biography") else "address").type in (
        ColumnType.TEXT,
        ColumnType.VARCHAR,
    )
    assert schema.table("Authors").column("biography").type is ColumnType.TEXT


def test_restaurants_schema_checks_booleans_and_nullable_dates(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    assert len(schema.tables) == 7
    reviews = schema.table("Reviews")
    rating = reviews.column("rating")
    assert rating.check is not None and ">= 1" in rating.check and "<= 5" in rating.check
    assert [c.columns for c in reviews.checks] == [["rating"]]
    menu = schema.table("Menu")
    assert menu.column("available").type is ColumnType.BOOLEAN and menu.column("available").default == "TRUE"
    customers = schema.table("Customers")
    reg = customers.column("registration_date")
    assert reg.type is ColumnType.DATETIME and reg.default == "CURRENT_TIMESTAMP"
    drivers = schema.table("Delivery_Drivers")
    assert drivers.column("termination_date").nullable is True  # explicit `DATE NULL`
    assert drivers.column("license_number").unique is True
    assert schema.table("Restaurants").column("rating").scale == 2
    order_items = schema.table("Order_Items")
    assert {fk.ref_table for fk in order_items.foreign_keys} == {"Orders", "Menu"}


def test_company_schema_self_reference_and_unique_sets(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["company"])
    assert len(schema.tables) == 7
    reviews = schema.table("Performance_Reviews")
    assert [fk.ref_table for fk in reviews.foreign_keys] == ["Employees", "Employees"]
    employees = schema.table("Employees")
    assert employees.unique_column_sets == [["employee_id"], ["email"]]
    assert employees.column("salary").precision == 10
    assert employees.column("employment_status").enum_values[0] == "Full-time"
    # Departments.manager_id is a plain INT (comment only) -> no FK
    assert schema.table("Departments").fk_for_column("manager_id") is None
    assert schema.table("Departments").fk_for_column("company_id") is not None
    assert schema.ignored_statements == []


# --- variants -----------------------------------------------------------------------------------------------

VARIANT_MYSQL = """
CREATE TABLE IF NOT EXISTS `customers` (customer_id INT PRIMARY KEY AUTO_INCREMENT, email VARCHAR(100));
CREATE TABLE IF NOT EXISTS `orders` (
  id SERIAL PRIMARY KEY,
  code CHAR(2) NOT NULL DEFAULT 'PL',
  qty INT UNSIGNED DEFAULT 1,
  big BIGINT,
  ratio DOUBLE,
  created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  active BOOLEAN DEFAULT TRUE,
  ended DATE NULL,
  price NUMERIC(10,2) CHECK (price > 0),
  customer_id INT NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
  note VARCHAR(50) COMMENT 'free text',
  nothing VARCHAR(5) DEFAULT NULL,
  CONSTRAINT uq_code UNIQUE (code, qty),
  CONSTRAINT chk_qty CHECK (qty BETWEEN 1 AND 100),
  UNIQUE KEY (big),
  INDEX idx_c (customer_id),
  FOREIGN KEY (customer_id) REFERENCES customers (customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX ix_email ON customers(email);
"""


def test_mysql_variants() -> None:
    schema = parse_ddl(VARIANT_MYSQL)
    orders = schema.table("orders")
    assert orders.primary_key == ["id"]
    idc = orders.column("id")
    assert idc.type is ColumnType.INTEGER and idc.auto_increment is True
    assert orders.column("code").length == 2 and orders.column("code").default == "'PL'"
    assert orders.column("qty").type is ColumnType.INTEGER and orders.column("qty").default == "1"
    assert orders.column("big").type is ColumnType.BIGINT and orders.column("big").unique is True
    assert orders.column("ratio").type is ColumnType.FLOAT
    assert orders.column("created").type is ColumnType.DATETIME
    assert orders.column("active").default == "TRUE"
    assert orders.column("ended").nullable is True
    price = orders.column("price")
    assert (price.type, price.precision, price.scale, price.check) == (ColumnType.DECIMAL, 10, 2, "price > 0")
    assert orders.column("note").comment == "free text"
    assert orders.column("nothing").default is None
    assert [u.columns for u in orders.unique_constraints] == [["code", "qty"]]
    named = [c for c in orders.checks if c.name == "chk_qty"]
    assert len(named) == 1 and named[0].columns == ["qty"] and "BETWEEN" in named[0].expression
    # inline REFERENCES + table-level FOREIGN KEY on the same column -> two FK entries, first with ON DELETE
    assert [(fk.ref_table, fk.on_delete) for fk in orders.foreign_keys] == [
        ("customers", "CASCADE"),
        ("customers", None),
    ]
    assert schema.ignored_statements and schema.ignored_statements[0].startswith("CREATE INDEX")


VARIANT_POSTGRES = """
CREATE TABLE "Dept" ("id" SERIAL PRIMARY KEY, title TEXT NOT NULL);
CREATE TABLE "Emp" (
  "id" SERIAL,
  "name" TEXT NOT NULL,
  dept_id INTEGER REFERENCES "Dept",
  status VARCHAR(10) CHECK (status IN ('a','b')),
  ts TIMESTAMPTZ DEFAULT now(),
  amount NUMERIC(12, 2) DEFAULT 0.00,
  PRIMARY KEY ("id", "name")
);
DROP TABLE IF EXISTS old;
ALTER TABLE "Emp" ADD COLUMN extra INT;
ALTER TABLE "Emp" ADD CONSTRAINT fk_dept FOREIGN KEY (dept_id) REFERENCES "Dept" ("id") ON DELETE SET NULL;
"""


def test_postgres_variants_composite_pk_and_reference_without_columns() -> None:
    schema = parse_ddl(VARIANT_POSTGRES)
    emp = schema.table("Emp")
    assert emp.primary_key == ["id", "name"]
    assert emp.column("id").primary_key and emp.column("name").primary_key and not emp.column("name").nullable
    assert emp.has_column("extra")
    fks = emp.foreign_keys
    assert fks[0].ref_columns == ["id"]  # REFERENCES "Dept" without columns -> parent PK
    assert fks[1].name == "fk_dept" and fks[1].on_delete == "SET NULL"
    assert emp.column("status").check == "status IN ('a', 'b')"
    assert emp.column("ts").type is ColumnType.DATETIME
    assert emp.column("amount").default == "0.00"
    assert any(s.startswith("DROP TABLE") for s in schema.ignored_statements)


def test_explicit_dialect_is_respected() -> None:
    schema = parse_ddl("CREATE TABLE t (id INT PRIMARY KEY)", dialect="postgres")
    assert schema.source_dialect == "postgres"


# --- errors -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ddl", "fragment"),
    [
        ("", "DDL is empty"),
        ("this is not sql at all;", "Could not parse DDL"),
        ("CREATE TABLE t (id INT, name VARCHAR(10) NOT NULL", "Could not parse DDL"),
        ("SELECT 1;", "no CREATE TABLE statements"),
        ("CREATE TABLE a (id INT PRIMARY KEY); CREATE TABLE a (id INT);", "defined twice"),
        ("CREATE TABLE a (id INT PRIMARY KEY, id VARCHAR(2));", "column 'id' is defined twice"),
        (
            "CREATE TABLE b (id INT PRIMARY KEY, a_id INT, FOREIGN KEY (a_id) REFERENCES a(id));",
            "unknown table 'a'",
        ),
        (
            "CREATE TABLE a (id INT PRIMARY KEY); CREATE TABLE b (id INT, FOREIGN KEY (x) REFERENCES a(id));",
            "unknown column 'x'",
        ),
        ("ALTER TABLE nope ADD CONSTRAINT c FOREIGN KEY (x) REFERENCES a(id);", "not defined"),
        ("CREATE TABLE a (id INT PRIMARY KEY, s VARCHAR(5) DEFAULT 'abc);", "Could not parse DDL"),
        ("CREATE TABLE `a (id INT PRIMARY KEY);", "Could not parse DDL"),
    ],
)  # fmt: skip
def test_errors_are_actionable(ddl: str, fragment: str) -> None:
    with pytest.raises(DDLParseError) as exc_info:
        parse_ddl(ddl)
    assert fragment in str(exc_info.value)


def test_dialect_fallback_and_unknown_types_are_reported_in_notes() -> None:
    schema = parse_ddl("CREATE TABLE a (id INT PRIMARY KEY, name VARCHR(100));")
    assert schema.source_dialect == "postgres"
    assert any(n.startswith("parsed as postgres after [mysql]") for n in schema.notes)
    assert any("unknown type 'VARCHR(100)'" in n for n in schema.notes)
    assert parse_ddl("CREATE TABLE a (id INT PRIMARY KEY);").notes == []
