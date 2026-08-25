"""F2.4 — CSV codec (quoted values, unquoted NULL, ISO dates, fixed decimals) and ZIP export."""

import io
import json
import zipfile
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from genai_data_gen_project.generation import export, heuristics
from genai_data_gen_project.generation.expander import expand
from genai_data_gen_project.schema.parser import parse_ddl
from genai_data_gen_project.storage import csvio
from genai_data_gen_project.storage.dataset import Dataset

DDL = (
    "CREATE TABLE t (id INT PRIMARY KEY, price DECIMAL(10,2), ok BOOLEAN, d DATE, ts DATETIME,"
    " note VARCHAR(50), ratio FLOAT);"
)


def frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": pd.Series([1, -2], dtype=object),
            "price": pd.Series([Decimal("12.50"), None], dtype=object),
            "ok": pd.Series([True, False], dtype=object),
            "d": pd.Series([date(2024, 2, 29), None], dtype=object),
            "ts": pd.Series([datetime(2024, 1, 2, 13, 45, 7, 123456), None], dtype=object),
            "note": pd.Series(['say "hi", ok', ""], dtype=object),
            "ratio": pd.Series([0.5, None], dtype=object),
        }
    )


def test_csv_text_format() -> None:
    table = parse_ddl(DDL).table("t")
    text = csvio.to_csv_text(frame(), table)
    lines = text.splitlines()
    assert lines[0] == '"id","price","ok","d","ts","note","ratio"'
    assert lines[1] == '"1","12.50","true","2024-02-29","2024-01-02 13:45:07","say ""hi"", ok","0.5"'
    assert lines[2] == '"-2",,"false",,,"",'  # NULL = unquoted empty, empty string stays quoted
    assert text.endswith("\n") and len(lines) == 3


def test_round_trip_is_typed_and_lossless() -> None:
    table = parse_ddl(DDL).table("t")
    original = frame()
    original.at[1, "note"] = None  # "" cannot survive the csv module's reader (documented)
    back = csvio.read_csv(csvio.to_csv_bytes(original, table), table)
    expected = original.copy()
    expected.at[0, "ts"] = datetime(2024, 1, 2, 13, 45, 7)  # microseconds dropped by design
    pd.testing.assert_frame_equal(back, expected)
    assert isinstance(back.at[0, "price"], Decimal) and isinstance(back.at[0, "id"], int)
    assert type(back.at[0, "d"]) is date and isinstance(back.at[0, "ts"], datetime)


def test_read_csv_errors_are_actionable() -> None:
    table = parse_ddl(DDL).table("t")
    with pytest.raises(csvio.CsvFormatError, match="header"):
        csvio.read_csv(b'"id","nope"\n"1","2"\n', table)
    with pytest.raises(csvio.CsvFormatError, match="cannot parse 'abc' as INT"):
        csvio.read_csv(b'"id","price","ok","d","ts","note","ratio"\n"abc",,,,,,\n', table)
    with pytest.raises(csvio.CsvFormatError, match="line 2 has 2 fields"):
        csvio.read_csv(b'"id","price","ok","d","ts","note","ratio"\n"1","2"\n', table)
    with pytest.raises(csvio.CsvFormatError, match="empty CSV"):
        csvio.read_csv(b"", table)


def test_expander_output_round_trips(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["restaurants"])
    tables = expand(schema, heuristics.plan(schema, 40), seed=4)
    for table in schema.tables:
        back = csvio.read_csv(csvio.to_csv_bytes(tables[table.name], table), table)
        pd.testing.assert_frame_equal(back, tables[table.name])


def test_zip_export_contains_everything(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["company"])
    tables = expand(schema, heuristics.plan(schema, 5), seed=1)
    ds = Dataset(id="abcdefghijkl", name="company", ddl=sample_ddl["company"], schema=schema, tables=tables,
                 report={"ok": True}, params={"seed": 1})  # fmt: skip
    data = export.to_zip_bytes(ds)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert names == {f"{t}.csv" for t in schema.table_names} | {"schema.ddl", "manifest.json"}
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["id"] == "abcdefghijkl" and manifest["tables"]["Employees"] == 5
        assert zf.read("schema.ddl").decode() == sample_ddl["company"]
        back = csvio.read_csv(zf.read("Projects.csv"), schema.table("Projects"))
        pd.testing.assert_frame_equal(back, tables["Projects"])
    assert export.to_csv_bytes(ds, "Companies") == csvio.to_csv_bytes(
        tables["Companies"], schema.table("Companies")
    )
    assert export.csv_filename(ds, "Companies") == "company_Companies.csv"
    assert export.zip_filename(ds) == "company_abcdefghijkl.zip"
