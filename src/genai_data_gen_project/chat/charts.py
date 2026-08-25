"""Chart specs → plotly figures (F6.3). The agent's `render_chart` tool yields a spec; the UI renders it."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from pydantic import BaseModel

ChartType = Literal["bar", "line", "pie", "scatter", "histogram"]


class ChartSpecError(ValueError):
    """The spec does not fit the data; the message lists the available columns."""


class ChartSpec(BaseModel):
    chart_type: ChartType
    x: str
    y: str | None = None
    color: str | None = None
    title: str = ""

    @classmethod
    def from_tool_args(cls, args: dict[str, Any]) -> ChartSpec:
        return cls.model_validate(
            {k: v for k, v in args.items() if k in cls.model_fields and v not in (None, "")}
        )


def frame_from_rows(columns: list[str], rows: list[list[Any]]) -> pd.DataFrame:
    """DataFrame with plot-friendly types (Decimal → float, ISO strings kept, dates as datetimes)."""
    converted = [[_plot_value(v) for v in row] for row in rows]
    return pd.DataFrame(converted, columns=columns)


def _plot_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return value


def to_figure(spec: ChartSpec, columns: list[str], rows: list[list[Any]]) -> go.Figure:
    """Build a plotly figure; raises ChartSpecError with the available columns when the spec does not fit."""
    if not rows:
        raise ChartSpecError("the query returned no rows to plot")
    frame = frame_from_rows(columns, rows)
    for name, label in ((spec.x, "x"), (spec.y, "y"), (spec.color, "color")):
        if name and name not in frame.columns:
            raise ChartSpecError(
                f"{label} column {name!r} is not in the result (available: {', '.join(columns)})"
            )
    needs_y = spec.chart_type in {"bar", "line", "pie", "scatter"}
    if needs_y and not spec.y:
        raise ChartSpecError(
            f"{spec.chart_type} charts need a numeric y column (available: {', '.join(columns)})"
        )
    if spec.y and not pd.api.types.is_numeric_dtype(frame[spec.y]):
        frame[spec.y] = pd.to_numeric(frame[spec.y], errors="coerce")
        if frame[spec.y].isna().all():
            raise ChartSpecError(f"y column {spec.y!r} is not numeric (available: {', '.join(columns)})")
    kwargs: dict[str, Any] = {"title": spec.title or None}
    if spec.color and spec.chart_type != "pie":
        kwargs["color"] = spec.color
    match spec.chart_type:
        case "bar":
            fig = px.bar(frame, x=spec.x, y=spec.y, **kwargs)
        case "line":
            ordered = frame.sort_values(spec.x)
            fig = px.line(ordered, x=spec.x, y=spec.y, markers=True, **kwargs)
        case "pie":
            fig = px.pie(frame, names=spec.x, values=spec.y, title=spec.title or None)
        case "scatter":
            fig = px.scatter(frame, x=spec.x, y=spec.y, **kwargs)
        case _:
            fig = px.histogram(frame, x=spec.x, **kwargs)
    fig.update_layout(margin={"l": 40, "r": 20, "t": 50 if spec.title else 20, "b": 40}, height=420)
    return fig
