"""Tools the talk-to-data agent may call (F6.2): `run_sql` (guarded, read-only) and `render_chart` (F6.3).

Tool results are what Gemini sees: keep them compact (first rows only) and, on failure, return the error with
a hint so the model can correct its query instead of giving up (docs/db-rules.md).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..config import Settings, get_settings
from ..llm.client import function_declaration
from ..storage import postgres
from ..storage.sql_guard import SqlRejected

MODEL_ROW_PREVIEW = 40  # rows the model gets to see per query (the UI shows the full capped result)
Executor = Callable[[str], postgres.QueryResult]

RUN_SQL = function_declaration(
    "run_sql",
    "Run ONE read-only PostgreSQL SELECT against the current dataset and return its rows. Use exact "
    "lowercase table and column names from the schema. Aggregate in SQL (count, sum, avg, group by, "
    "order by, limit).",
    {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "A single SELECT statement (PostgreSQL dialect)"}
        },
        "required": ["sql"],
    },
)
RENDER_CHART = function_declaration(
    "render_chart",
    "Render a chart from the rows returned by the LAST run_sql call. Call it only after run_sql, when the "
    "user asks for a plot/chart/graph or a visual comparison over categories or time.",
    {
        "type": "object",
        "properties": {
            "chart_type": {"type": "string", "enum": ["bar", "line", "pie", "scatter", "histogram"]},
            "x": {"type": "string", "description": "column for the x axis / categories / pie labels"},
            "y": {"type": "string", "description": "numeric column for values (omit for histogram of x)"},
            "color": {"type": "string", "description": "optional column to split series by"},
            "title": {"type": "string"},
        },
        "required": ["chart_type", "x", "title"],
    },
)
TOOLS = [RUN_SQL, RENDER_CHART]


@dataclass
class ToolOutcome:
    name: str
    args: dict[str, Any]
    response: dict[str, Any]  # what the model receives
    result: postgres.QueryResult | None = None  # full result for the UI (run_sql)
    error: str | None = None


def make_executor(dataset_id: str, settings: Settings | None = None) -> Executor:
    settings = settings or get_settings()

    def run(sql_text: str) -> postgres.QueryResult:
        return postgres.run_readonly(dataset_id, sql_text, settings)

    return run


def dispatch(
    name: str, args: dict[str, Any], executor: Executor, last_result: postgres.QueryResult | None
) -> ToolOutcome:
    if name == "run_sql":
        sql_text = str(args.get("sql", ""))
        try:
            result = executor(sql_text)
        except SqlRejected as e:
            return ToolOutcome(
                name, args, {"error": str(e), "hint": "send exactly one read-only SELECT"}, error=str(e)
            )
        except postgres.SqlError as e:
            return ToolOutcome(
                name, args, {"error": e.message, "hint": e.hint, "sqlstate": e.sqlstate}, error=str(e)
            )
        return ToolOutcome(name, args, _result_for_model(result), result=result)
    if name == "render_chart":
        if last_result is None or not last_result.rows:
            return ToolOutcome(
                name,
                args,
                {"error": "no query result to plot", "hint": "call run_sql first"},
                error="no data",
            )
        missing = [
            c for c in (args.get("x"), args.get("y"), args.get("color")) if c and c not in last_result.columns
        ]
        if missing:
            return ToolOutcome(
                name,
                args,
                {
                    "error": f"unknown column(s) {missing}",
                    "hint": f"available columns: {last_result.columns}",
                },
                error=f"unknown columns {missing}",
            )
        return ToolOutcome(name, args, {"rendered": True, "rows": last_result.row_count, "spec": dict(args)})
    return ToolOutcome(name, args, {"error": f"unknown tool {name!r}"}, error=f"unknown tool {name!r}")


def _result_for_model(result: postgres.QueryResult) -> dict[str, Any]:
    rows = [[_jsonable(v) for v in row] for row in result.rows[:MODEL_ROW_PREVIEW]]
    payload: dict[str, Any] = {
        "columns": result.columns,
        "rows": rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
    }
    if result.row_count > MODEL_ROW_PREVIEW:
        payload["note"] = f"showing the first {MODEL_ROW_PREVIEW} of {result.row_count} rows"
    return payload


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
