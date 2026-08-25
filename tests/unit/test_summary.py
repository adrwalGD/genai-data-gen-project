"""F1.4 — compact schema summary for prompts."""

from genai_data_gen_project.schema.parser import parse_ddl
from genai_data_gen_project.schema.summary import schema_summary, table_summary


def test_library_summary_is_compact_and_complete(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["library"])
    text = schema_summary(schema)
    assert len(text) < 6000, len(text)
    blocks = text.split("\n\n")[1:]
    assert len(blocks) == 9 and all(len(b.splitlines()) <= 25 for b in blocks)
    assert text.startswith("9 tables: Authors, Publishers, Books,")
    order_line = next(line for line in text.splitlines() if line.startswith("Generation order"))
    assert order_line.index("Authors") < order_line.index("Books")
    assert (
        "Deferred FKs (filled after parents exist): "
        "Library_Branches.manager_id→Employees, Employees.department_id→Departments" in text
    )
    assert "TABLE Books (PK book_id)" in text
    assert "  book_id INTEGER PK identity" in text
    assert "  author_id INTEGER NOT NULL FK→Authors.author_id" in text
    assert "  isbn VARCHAR(20) NOT NULL UNIQUE" in text
    assert "  format ENUM(Hardcover|Paperback|E-book|Audiobook)" in text
    assert "  fine_amount DECIMAL(10,2) DEFAULT 0" in text
    assert "  loan_date TIMESTAMP NOT NULL" in text
    assert "  account_status ENUM(Active|Inactive|Suspended|Closed) DEFAULT 'Active'" in text


def test_lowercase_mode_matches_postgres_identifiers(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["library"])
    text = schema_summary(schema, lowercase=True)
    assert "TABLE library_branches (PK branch_id)" in text
    assert "  manager_id INTEGER FK→employees.employee_id" in text
    assert "Books" not in text


def test_checks_and_defaults_are_shown(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    text = table_summary(schema.table("Reviews"))
    assert text.splitlines()[0] == "TABLE Reviews (PK review_id)"
    assert "  rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5)" in text
    assert "  review_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP" in text
    assert text.count("CHECK(") == 1  # inline check is not repeated as a table-level line


def test_composite_fk_unique_and_table_checks() -> None:
    schema = parse_ddl(
        "CREATE TABLE p (a INT, b INT, PRIMARY KEY (a, b));"
        "CREATE TABLE c (id INT PRIMARY KEY, a INT, b INT, qty INT, FOREIGN KEY (a, b) REFERENCES p(a, b),"
        " CONSTRAINT uq UNIQUE (a, b), CONSTRAINT chk CHECK (qty > 0 AND qty < 10));"
    )
    text = schema_summary(schema, include_order=False)
    assert "TABLE p (PK a, b)" in text
    assert "  FK (a, b) → p(a, b)" in text
    assert "  UNIQUE (a, b)" in text
    assert "  CHECK(qty > 0 AND qty < 10)" in text
    assert "Generation order" not in text
