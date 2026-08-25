"""Compact, prompt-friendly schema summary (F1.4).

Two identifier styles: the original DDL spelling (for the generation planner, whose output is mapped back
onto the IR by name) and lowercase (the Postgres spelling used by talk-to-data). One line per column keeps
the whole library schema (9 tables) under 6k characters ≈ 1.5k tokens.
"""

from __future__ import annotations

from .models import Column, ColumnType, Schema, Table
from .order import UnsatisfiableSchemaError, generation_order


def type_label(col: Column) -> str:
    match col.type:
        case ColumnType.VARCHAR:
            return f"VARCHAR({col.length})" if col.length else "TEXT"
        case ColumnType.CHAR:
            return f"CHAR({col.length or 1})"
        case ColumnType.DECIMAL:
            return f"DECIMAL({col.precision or 10},{col.scale or 0})"
        case ColumnType.ENUM:
            return "ENUM(" + "|".join(col.enum_values) + ")"
        case ColumnType.DATETIME:
            return "TIMESTAMP"
        case _:
            return col.type.value.upper()


def _name(name: str, lowercase: bool) -> str:
    return name.lower() if lowercase else name


def column_line(table: Table, col: Column, *, lowercase: bool = False) -> str:
    parts = [_name(col.name, lowercase), type_label(col)]
    if col.primary_key:
        parts.append("PK")
    if col.auto_increment:
        parts.append("identity")
    if not col.nullable and not col.primary_key:
        parts.append("NOT NULL")
    if col.unique:
        parts.append("UNIQUE")
    if col.default is not None:
        parts.append(f"DEFAULT {col.default}")
    fk = table.fk_for_column(col.name)
    if fk is not None and len(fk.columns) == 1:
        parts.append(f"FK→{_name(fk.ref_table, lowercase)}.{_name(fk.ref_columns[0], lowercase)}")
    if col.check:
        parts.append(f"CHECK({col.check})")
    return "  " + " ".join(parts)


def table_summary(table: Table, *, lowercase: bool = False) -> str:
    header = f"TABLE {_name(table.name, lowercase)}"
    if table.primary_key:
        header += " (PK " + ", ".join(_name(c, lowercase) for c in table.primary_key) + ")"
    lines = [header] + [column_line(table, c, lowercase=lowercase) for c in table.columns]
    for fk in table.foreign_keys:
        if len(fk.columns) > 1:
            cols = ", ".join(_name(c, lowercase) for c in fk.columns)
            refs = ", ".join(_name(c, lowercase) for c in fk.ref_columns)
            lines.append(f"  FK ({cols}) → {_name(fk.ref_table, lowercase)}({refs})")
    for uq in table.unique_constraints:
        lines.append("  UNIQUE (" + ", ".join(_name(c, lowercase) for c in uq.columns) + ")")
    inline_checks = {c.check for c in table.columns if c.check}
    for chk in table.checks:
        if chk.expression not in inline_checks:
            lines.append(f"  CHECK({chk.expression})")
    return "\n".join(lines)


def schema_summary(schema: Schema, *, lowercase: bool = False, include_order: bool = True) -> str:
    """Whole-schema summary: table list, generation order (+ deferred FKs), then one block per table."""
    head = [f"{len(schema.tables)} tables: " + ", ".join(_name(t, lowercase) for t in schema.table_names)]
    if include_order:
        try:
            order = generation_order(schema)
        except UnsatisfiableSchemaError as e:
            head.append(f"Generation order: unsatisfiable ({e})")
        else:
            head.append(
                "Generation order (parents first): " + ", ".join(_name(t, lowercase) for t in order.tables)
            )
            if order.deferred:
                deferred = ", ".join(
                    f"{_name(d.table, lowercase)}.{','.join(_name(c, lowercase) for c in d.fk.columns)}"
                    f"→{_name(d.fk.ref_table, lowercase)}"
                    for d in order.deferred
                )
                head.append(f"Deferred FKs (filled after parents exist): {deferred}")
    blocks = [table_summary(t, lowercase=lowercase) for t in schema.tables]
    return "\n".join(head) + "\n\n" + "\n\n".join(blocks)
