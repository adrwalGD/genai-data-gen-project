"""F1.2 — generation order: parents first, self-references and nullable cycle FKs deferred."""

import pytest

from genai_data_gen_project.schema.models import Schema
from genai_data_gen_project.schema.order import UnsatisfiableSchemaError, generation_order
from genai_data_gen_project.schema.parser import parse_ddl


def assert_parents_precede_children(schema: Schema) -> None:
    order = generation_order(schema)
    assert sorted(order.tables) == sorted(schema.table_names)
    for table in schema.tables:
        for fk in table.foreign_keys:
            if order.is_deferred(table.name, fk):
                continue
            assert order.position(fk.ref_table) < order.position(table.name), (
                table.name,
                fk.columns,
                fk.ref_table,
            )


def test_restaurants_is_a_dag(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    order = generation_order(schema)
    assert order.deferred == []
    assert order.position("Restaurants") < order.position("Menu") < order.position("Order_Items")
    assert order.position("Customers") < order.position("Orders") < order.position("Order_Items")
    assert_parents_precede_children(schema)


def test_company_is_a_dag_with_two_fks_to_employees(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["company"])
    order = generation_order(schema)
    assert order.deferred == []
    assert order.position("Employees") < order.position("Performance_Reviews")
    assert max(order.position("Employees"), order.position("Projects")) < order.position("Employee_Projects")
    assert_parents_precede_children(schema)


def test_library_cycles_are_broken_at_nullable_fks(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["library"])
    order = generation_order(schema)
    deferred = {(d.table, tuple(d.fk.columns), d.fk.ref_table, d.reason) for d in order.deferred}
    assert deferred == {
        ("Library_Branches", ("manager_id",), "Employees", "cycle"),
        ("Employees", ("department_id",), "Departments", "cycle"),
    }
    assert order.deferred_for("Employees")[0].ref_table == "Departments"
    assert order.position("Library_Branches") < order.position("Employees") < order.position("Departments")
    assert order.position("Book_Inventory") < order.position("Book_Loans")
    assert_parents_precede_children(schema)
    # deterministic
    assert generation_order(schema) == order


def test_self_reference_is_deferred() -> None:
    schema = parse_ddl(
        "CREATE TABLE e (id INT PRIMARY KEY, manager_id INT NOT NULL, "
        "FOREIGN KEY (manager_id) REFERENCES e(id));"
    )
    order = generation_order(schema)
    assert order.tables == ["e"]
    assert [(d.table, d.reason) for d in order.deferred] == [("e", "self-reference")]


def test_mixed_cycle_defers_only_the_nullable_side() -> None:
    schema = parse_ddl(
        "CREATE TABLE a (id INT PRIMARY KEY, b_id INT NOT NULL, FOREIGN KEY (b_id) REFERENCES b(id));"
        "CREATE TABLE b (id INT PRIMARY KEY, a_id INT, FOREIGN KEY (a_id) REFERENCES a(id));"
    )
    order = generation_order(schema)
    assert order.tables == ["b", "a"]
    assert [(d.table, d.fk.columns) for d in order.deferred] == [("b", ["a_id"])]


def test_not_null_cycle_is_unsatisfiable() -> None:
    schema = parse_ddl(
        "CREATE TABLE a (id INT PRIMARY KEY, b_id INT NOT NULL, FOREIGN KEY (b_id) REFERENCES b(id));"
        "CREATE TABLE b (id INT PRIMARY KEY, a_id INT NOT NULL, FOREIGN KEY (a_id) REFERENCES a(id));"
    )
    with pytest.raises(UnsatisfiableSchemaError) as exc_info:
        generation_order(schema)
    message = str(exc_info.value)
    assert "a.b_id → b (NOT NULL)" in message and "b.a_id → a (NOT NULL)" in message
    assert "nullable" in message
