"""DDL text → `Schema` IR using sqlglot (MySQL dialect first, PostgreSQL as fallback).

Verified AST shapes (sqlglot 30, see DECISIONS.md#2026-08-25-ddl-parsing-with-sqlglot):
- `exp.Create(kind="TABLE").this` is an `exp.Schema` whose expressions are `ColumnDef`s and table-level
  constraints (`PrimaryKey`, `ForeignKey`, `UniqueColumnConstraint`, `CheckColumnConstraint`, wrapped in
  `Constraint(this=name, expressions=[...])` when named; `IndexColumnConstraint` for indexes).
- Column constraints live in `ColumnDef.constraints[*].kind`: `PrimaryKeyColumnConstraint`,
  `AutoIncrementColumnConstraint`, `NotNullColumnConstraint(allow_null=True)` for a bare `NULL`,
  `UniqueColumnConstraint`, `DefaultColumnConstraint(this=expr)`, `CheckColumnConstraint(this=expr)`,
  `Reference(this=Schema(Table, cols))`, `CommentColumnConstraint`, `GeneratedAsIdentityColumnConstraint`.
- `exp.Alter.actions` holds `AddConstraint(expressions=[Constraint|ForeignKey|...])` or `ColumnDef`
  (ADD COLUMN).
"""

from __future__ import annotations

import logging
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .models import CheckConstraint, Column, ColumnType, ForeignKey, Schema, Table, UniqueConstraint

_log = logging.getLogger(__name__)

DIALECTS = ("mysql", "postgres")
OUT_DIALECT = "postgres"


class DDLParseError(ValueError):
    """Raised for DDL that cannot be parsed or is semantically incomplete (e.g. FK to an unknown table)."""


_TYPE_MAP: dict[str, ColumnType] = {
    "INT": ColumnType.INTEGER,
    "INTEGER": ColumnType.INTEGER,
    "UINT": ColumnType.INTEGER,
    "MEDIUMINT": ColumnType.INTEGER,
    "UMEDIUMINT": ColumnType.INTEGER,
    "SERIAL": ColumnType.INTEGER,
    "TINYINT": ColumnType.SMALLINT,
    "UTINYINT": ColumnType.SMALLINT,
    "SMALLINT": ColumnType.SMALLINT,
    "USMALLINT": ColumnType.SMALLINT,
    "SMALLSERIAL": ColumnType.SMALLINT,
    "BIGINT": ColumnType.BIGINT,
    "UBIGINT": ColumnType.BIGINT,
    "BIGSERIAL": ColumnType.BIGINT,
    "DECIMAL": ColumnType.DECIMAL,
    "NUMERIC": ColumnType.DECIMAL,
    "MONEY": ColumnType.DECIMAL,
    "FLOAT": ColumnType.FLOAT,
    "DOUBLE": ColumnType.FLOAT,
    "REAL": ColumnType.FLOAT,
    "BOOLEAN": ColumnType.BOOLEAN,
    "BIT": ColumnType.BOOLEAN,
    "VARCHAR": ColumnType.VARCHAR,
    "NVARCHAR": ColumnType.VARCHAR,
    "CHAR": ColumnType.CHAR,
    "NCHAR": ColumnType.CHAR,
    "TEXT": ColumnType.TEXT,
    "TINYTEXT": ColumnType.TEXT,
    "MEDIUMTEXT": ColumnType.TEXT,
    "LONGTEXT": ColumnType.TEXT,
    "DATE": ColumnType.DATE,
    "DATETIME": ColumnType.DATETIME,
    "TIMESTAMP": ColumnType.DATETIME,
    "TIMESTAMPTZ": ColumnType.DATETIME,
    "TIMESTAMPLTZ": ColumnType.DATETIME,
    "TIMESTAMPNTZ": ColumnType.DATETIME,
    "TIME": ColumnType.TIME,
    "TIMETZ": ColumnType.TIME,
    "ENUM": ColumnType.ENUM,
    "JSON": ColumnType.JSON,
    "JSONB": ColumnType.JSON,
    "UUID": ColumnType.UUID,
}
_SERIAL_TYPES = {"SERIAL", "SMALLSERIAL", "BIGSERIAL"}


def parse_ddl(text: str, dialect: str | None = None) -> Schema:
    """Parse DDL text into a `Schema`.

    Tries MySQL then PostgreSQL syntax unless `dialect` is given. Non-table statements are skipped and
    listed in `Schema.ignored_statements`. Raises `DDLParseError` with sqlglot's line/column context on
    syntax errors, and for semantic problems (no tables, duplicate tables/columns, unknown FK targets).
    """
    if not text or not text.strip():
        raise DDLParseError("DDL is empty — upload a .sql/.ddl file with CREATE TABLE statements")
    statements, used = _parse_statements(text, dialect)
    builder = _SchemaBuilder(used)
    for stmt in statements:
        builder.consume(stmt)
    return builder.build()


def _parse_statements(text: str, dialect: str | None) -> tuple[list[exp.Expression], str]:
    """Parse with the given dialect, else MySQL then PostgreSQL; return statements + dialect used."""
    errors: list[str] = []
    for d in (dialect,) if dialect else DIALECTS:
        try:
            parsed = sqlglot.parse(text, read=d)
        except ParseError as e:
            errors.append(f"[{d}] {_first_line(str(e))}")
            continue
        return [s for s in parsed if isinstance(s, exp.Expression)], d
    raise DDLParseError("Could not parse DDL — " + "; ".join(errors))


def _first_line(message: str) -> str:
    return message.strip().splitlines()[0][:300] if message.strip() else "unknown parse error"


class _SchemaBuilder:
    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.tables: list[Table] = []
        self.ignored: list[str] = []
        self.deferred_fks: list[tuple[str, ForeignKey]] = []

    # -- statements ------------------------------------------------------------------------------------------
    def consume(self, stmt: exp.Expression) -> None:
        if isinstance(stmt, exp.Create) and (stmt.args.get("kind") or "").upper() == "TABLE":
            self._create_table(stmt)
        elif isinstance(stmt, exp.Alter):
            self._alter_table(stmt)
        else:
            self.ignored.append(_short_sql(stmt))

    def _create_table(self, stmt: exp.Create) -> None:
        schema_expr = stmt.this
        if not isinstance(schema_expr, exp.Schema):
            self.ignored.append(_short_sql(stmt))
            return
        table_expr = schema_expr.this
        name = table_expr.name if isinstance(table_expr, exp.Table) else str(table_expr)
        if any(t.name.lower() == name.lower() for t in self.tables):
            raise DDLParseError(f"table {name!r} is defined twice")
        table = Table(name=name, columns=[])
        for item in schema_expr.expressions:
            if isinstance(item, exp.ColumnDef):
                table.columns.append(self._column(item, table))
            else:
                self._table_constraint(table, item, None)
        if not table.columns:
            raise DDLParseError(f"table {name!r} has no columns")
        self.tables.append(table)

    def _alter_table(self, stmt: exp.Alter) -> None:
        tname = stmt.this.name if isinstance(stmt.this, exp.Table) else str(stmt.this)
        table = self._find_table(tname)
        if table is None:
            raise DDLParseError(f"ALTER TABLE {tname!r}: table is not defined in this DDL")
        for action in stmt.args.get("actions") or []:
            if isinstance(action, exp.AddConstraint):
                for c in action.expressions:
                    self._table_constraint(table, c, None)
            elif isinstance(action, exp.ColumnDef):
                if table.has_column(action.this.name):
                    raise DDLParseError(f"ALTER TABLE {tname}: column {action.this.name!r} already exists")
                table.columns.append(self._column(action, table))
            else:
                self.ignored.append(_short_sql(stmt))

    # -- columns ---------------------------------------------------------------------------------------------
    def _column(self, cdef: exp.ColumnDef, table: Table) -> Column:
        name = cdef.this.name
        if table.has_column(name):
            raise DDLParseError(f"table {table.name!r}: column {name!r} is defined twice")
        kind = cdef.args.get("kind")
        col = self._typed_column(name, kind)
        for constraint in cdef.constraints:
            k = constraint.kind
            if isinstance(k, exp.PrimaryKeyColumnConstraint):
                col.primary_key, col.nullable = True, False
                if name not in table.primary_key:
                    table.primary_key.append(name)
            elif isinstance(k, exp.AutoIncrementColumnConstraint | exp.GeneratedAsIdentityColumnConstraint):
                col.auto_increment = True
            elif isinstance(k, exp.NotNullColumnConstraint):
                col.nullable = bool(k.args.get("allow_null"))
            elif isinstance(k, exp.UniqueColumnConstraint):
                col.unique = True
            elif isinstance(k, exp.DefaultColumnConstraint):
                col.default = None if isinstance(k.this, exp.Null) else k.this.sql(dialect=OUT_DIALECT)
            elif isinstance(k, exp.CheckColumnConstraint):
                col.check = k.this.sql(dialect=OUT_DIALECT)
                table.checks.append(_check(None, k.this))
            elif isinstance(k, exp.Reference):
                table.foreign_keys.append(_fk_from_reference(None, [name], k))
            elif isinstance(k, exp.CommentColumnConstraint):
                col.comment = k.this.name if isinstance(k.this, exp.Literal) else k.this.sql()
            else:
                _log.debug("ignoring column constraint %s on %s.%s", type(k).__name__, table.name, name)
        if col.primary_key:
            col.nullable = False
        return col

    def _typed_column(self, name: str, kind: exp.DataType | None) -> Column:
        if kind is None:
            return Column(name=name, type=ColumnType.UNKNOWN, raw_type="")
        dtype_name = kind.this.name if isinstance(kind.this, exp.DataType.Type) else str(kind.this)
        ctype = _TYPE_MAP.get(dtype_name.upper(), ColumnType.UNKNOWN)
        params = [p.this if isinstance(p, exp.DataTypeParam) else p for p in kind.expressions]
        col = Column(name=name, type=ctype, raw_type=kind.sql(dialect=self.dialect))
        if ctype is ColumnType.ENUM:
            col.enum_values = [str(p.this) if isinstance(p, exp.Literal) else p.sql() for p in params]
        elif ctype in {ColumnType.VARCHAR, ColumnType.CHAR}:
            col.length = _int_param(params, 0) or (1 if ctype is ColumnType.CHAR else None)
        elif ctype is ColumnType.DECIMAL:
            col.precision = _int_param(params, 0) or 10
            col.scale = _int_param(params, 1) or 0
        if dtype_name.upper() in _SERIAL_TYPES:
            col.auto_increment = True
        if ctype is ColumnType.UNKNOWN:
            _log.warning(
                "unknown column type %r for column %s — treated as TEXT by the emitter", dtype_name, name
            )
        return col

    # -- table-level constraints -----------------------------------------------------------------------------
    def _table_constraint(self, table: Table, item: exp.Expression, name: str | None) -> None:
        if isinstance(item, exp.Constraint):
            cname = item.this.name if item.this is not None else None
            for inner in item.expressions:
                self._table_constraint(table, inner, cname)
        elif isinstance(item, exp.PrimaryKey):
            cols = [_ident_name(e) for e in item.expressions]
            table.primary_key = cols
            for c in cols:
                col = _require_column(table, c, "PRIMARY KEY")
                col.primary_key, col.nullable = True, False
        elif isinstance(item, exp.ForeignKey):
            cols = [_ident_name(e) for e in item.expressions]
            ref = item.args.get("reference")
            if not isinstance(ref, exp.Reference):
                raise DDLParseError(
                    f"table {table.name!r}: FOREIGN KEY ({', '.join(cols)}) without REFERENCES"
                )
            table.foreign_keys.append(_fk_from_reference(name, cols, ref))
        elif isinstance(item, exp.UniqueColumnConstraint):
            cols = (
                [_ident_name(e) for e in item.this.expressions] if isinstance(item.this, exp.Schema) else []
            )
            if len(cols) == 1:
                _require_column(table, cols[0], "UNIQUE").unique = True
            elif cols:
                table.unique_constraints.append(UniqueConstraint(name=name, columns=cols))
        elif isinstance(item, exp.CheckColumnConstraint):
            table.checks.append(_check(name, item.this))
        elif isinstance(item, exp.IndexColumnConstraint):
            return  # plain indexes carry no data constraint
        else:
            _log.debug("ignoring table constraint %s on %s", type(item).__name__, table.name)

    # -- finish ----------------------------------------------------------------------------------------------
    def _find_table(self, name: str) -> Table | None:
        return next((t for t in self.tables if t.name.lower() == name.lower()), None)

    def build(self) -> Schema:
        if not self.tables:
            raise DDLParseError("no CREATE TABLE statements found in the DDL")
        for table in self.tables:
            self._resolve_fks(table)
        return Schema(tables=self.tables, source_dialect=self.dialect, ignored_statements=self.ignored)

    def _resolve_fks(self, table: Table) -> None:
        for fk in table.foreign_keys:
            parent = self._find_table(fk.ref_table)
            if parent is None:
                raise DDLParseError(
                    f"table {table.name!r}: FOREIGN KEY ({', '.join(fk.columns)}) references unknown table "
                    f"{fk.ref_table!r} — add its CREATE TABLE to the DDL"
                )
            fk.ref_table = parent.name
            fk.columns = [_require_column(table, c, "FOREIGN KEY").name for c in fk.columns]
            if not fk.ref_columns:
                fk.ref_columns = list(parent.primary_key)
            fk.ref_columns = [
                _require_column(parent, c, f"FOREIGN KEY from {table.name}").name for c in fk.ref_columns
            ]
            if len(fk.ref_columns) != len(fk.columns):
                raise DDLParseError(
                    f"table {table.name!r}: FOREIGN KEY ({', '.join(fk.columns)}) → {parent.name}"
                    f"({', '.join(fk.ref_columns)}) column count mismatch"
                )


# -- helpers -------------------------------------------------------------------------------------------------
def _ident_name(e: exp.Expression) -> str:
    if isinstance(e, exp.Ordered):
        e = e.this
    if isinstance(e, exp.Column):
        return e.name
    return e.name if hasattr(e, "name") else e.sql()


def _int_param(params: list[Any], index: int) -> int | None:
    if index >= len(params):
        return None
    p = params[index]
    try:
        return int(p.this) if isinstance(p, exp.Literal) else int(str(p))
    except TypeError, ValueError:
        return None


def _check(name: str | None, expression: exp.Expression) -> CheckConstraint:
    cols: list[str] = []
    for c in expression.find_all(exp.Column):
        if c.name not in cols:
            cols.append(c.name)
    return CheckConstraint(name=name, expression=expression.sql(dialect=OUT_DIALECT), columns=cols)


def _fk_from_reference(name: str | None, columns: list[str], ref: exp.Reference) -> ForeignKey:
    target = ref.this
    if isinstance(target, exp.Schema):
        ref_table = target.this.name if isinstance(target.this, exp.Table) else str(target.this)
        ref_cols = [_ident_name(e) for e in target.expressions]
    elif isinstance(target, exp.Table):
        ref_table, ref_cols = target.name, []
    else:
        raise DDLParseError(f"unsupported REFERENCES target: {ref.sql()}")
    on_delete = None
    for opt in ref.args.get("options") or []:
        text = opt.sql() if isinstance(opt, exp.Expression) else str(opt)
        if text.upper().startswith("ON DELETE"):
            on_delete = text[len("ON DELETE") :].strip().upper()
    return ForeignKey(
        name=name, columns=columns, ref_table=ref_table, ref_columns=ref_cols, on_delete=on_delete
    )


def _require_column(table: Table, name: str, context: str) -> Column:
    try:
        return table.column(name)
    except KeyError as e:
        raise DDLParseError(f"table {table.name!r}: {context} refers to unknown column {name!r}") from e


def _short_sql(stmt: exp.Expression) -> str:
    text = " ".join(stmt.sql().split())
    return text if len(text) <= 120 else text[:117] + "..."
