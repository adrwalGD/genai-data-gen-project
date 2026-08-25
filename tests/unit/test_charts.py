"""F6.3 — chart specs become plotly figures; bad specs explain what is available."""

from datetime import date
from decimal import Decimal

import pytest

from genai_data_gen_project.chat.charts import ChartSpec, ChartSpecError, frame_from_rows, to_figure

COLUMNS = ["city", "revenue", "orders", "day"]
ROWS = [
    ["Kraków", Decimal("120.50"), 3, date(2025, 1, 2)],
    ["Gdańsk", Decimal("80.00"), 1, date(2025, 1, 1)],
    ["Warszawa", Decimal("200.25"), 5, date(2025, 1, 3)],
]


@pytest.mark.parametrize(
    ("spec", "trace_type"),
    [
        (ChartSpec(chart_type="bar", x="city", y="revenue", title="Revenue by city"), "bar"),
        (ChartSpec(chart_type="line", x="day", y="orders", title="Orders per day"), "scatter"),
        (ChartSpec(chart_type="pie", x="city", y="revenue", title="Share"), "pie"),
        (
            ChartSpec(chart_type="scatter", x="orders", y="revenue", color="city", title="Orders vs revenue"),
            "scatter",
        ),
        (ChartSpec(chart_type="histogram", x="revenue", title="Revenue distribution"), "histogram"),
    ],
)
def test_each_chart_type_renders(spec: ChartSpec, trace_type: str) -> None:
    fig = to_figure(spec, COLUMNS, ROWS)
    assert fig.data and fig.data[0].type == trace_type
    assert fig.layout.title.text == spec.title and fig.layout.height == 420


def test_line_charts_are_sorted_by_x_and_decimals_become_floats() -> None:
    fig = to_figure(ChartSpec(chart_type="line", x="day", y="revenue"), COLUMNS, ROWS)
    ys = list(fig.data[0].y)
    assert ys == [80.0, 120.5, 200.25]  # sorted by day: Jan 1, Jan 2, Jan 3
    frame = frame_from_rows(COLUMNS, ROWS)
    assert frame["revenue"].dtype.kind == "f" and str(frame["day"].dtype).startswith("datetime64")


def test_spec_from_tool_args_ignores_unknown_and_empty_fields() -> None:
    spec = ChartSpec.from_tool_args(
        {"chart_type": "bar", "x": "city", "y": "revenue", "title": "", "extra": 1, "color": None}
    )
    assert spec == ChartSpec(chart_type="bar", x="city", y="revenue", title="")
    with pytest.raises(ValueError):
        ChartSpec.from_tool_args({"chart_type": "sunburst", "x": "city"})


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        (
            ChartSpec(chart_type="bar", x="town", y="revenue"),
            "x column 'town' is not in the result (available: city, revenue, orders, day)",
        ),
        (ChartSpec(chart_type="bar", x="city"), "bar charts need a numeric y column"),
        (ChartSpec(chart_type="bar", x="city", y="city"), "y column 'city' is not numeric"),
        (
            ChartSpec(chart_type="scatter", x="orders", y="revenue", color="nope"),
            "color column 'nope' is not in the result",
        ),
    ],
)
def test_bad_specs_explain_available_columns(spec: ChartSpec, fragment: str) -> None:
    with pytest.raises(ChartSpecError, match=fragment.replace("(", "\\(").replace(")", "\\)")):
        to_figure(spec, COLUMNS, ROWS)


def test_empty_result_is_an_error() -> None:
    with pytest.raises(ChartSpecError, match="no rows"):
        to_figure(ChartSpec(chart_type="bar", x="city", y="revenue"), COLUMNS, [])
