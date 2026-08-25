#!/usr/bin/env python3
"""End-to-end smoke (F2.6; LLM mode in F3.4): DDL → plan → expand → validate → save/load → PostgreSQL.

Usage:
  e2e_smoke.py [--schema all|library|restaurants|company|PATH] [--rows N] [--seed S] [--keep] [--llm]
Exit code 0 only when every stage succeeds for every schema; otherwise 1 with an actionable message.
`--keep` leaves the dataset in data/datasets and in PostgreSQL (default: temporary + dropped afterwards).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from genai_data_gen_project.config import Settings  # noqa: E402
from genai_data_gen_project.generation import heuristics  # noqa: E402
from genai_data_gen_project.generation.expander import ExpansionError, expand  # noqa: E402
from genai_data_gen_project.generation.validator import validate  # noqa: E402
from genai_data_gen_project.schema.parser import DDLParseError, parse_ddl  # noqa: E402
from genai_data_gen_project.storage import csvio, datasets, postgres  # noqa: E402
from genai_data_gen_project.storage.dataset import Dataset, new_dataset_id  # noqa: E402

SAMPLES = {
    "library": ROOT / "project-spec" / "library_mgm_schema.ddl",
    "restaurants": ROOT / "project-spec" / "restrurants_schema.ddl",
    "company": ROOT / "project-spec" / "company_employee_schema.ddl",
}


class StageFailed(Exception):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def run_one(name: str, ddl_path: Path, rows: int, seed: int, keep: bool, settings: Settings) -> bool:
    print(f"\n=== {name}: {ddl_path.name} — {rows} rows/table, seed {seed} ===")
    started = time.perf_counter()
    timings: dict[str, float] = {}
    root: Path | None = None
    dataset: Dataset | None = None

    def stage(label: str) -> float:
        now = time.perf_counter()
        timings[label] = now - started - sum(timings.values())
        return now

    try:
        try:
            schema = parse_ddl(ddl_path.read_text(encoding="utf-8"))
        except (DDLParseError, OSError) as e:
            raise StageFailed("parse", f"{e} — fix the DDL file {ddl_path}") from e
        stage("parse")
        for note in schema.notes:
            print(f"  note: {note}")
        plan = heuristics.plan(schema, rows)
        stage("plan")
        try:
            tables = expand(schema, plan, seed=seed)
        except ExpansionError as e:
            raise StageFailed("expand", f"{e} — adjust the plan/recipes (generation/heuristics.py)") from e
        stage("expand")
        report = validate(schema, tables, expected_rows=dict.fromkeys(schema.table_names, rows))
        stage("validate")
        if not report.ok:
            lines = "\n".join(f"    {issue}" for issue in report.issues[:20])
            raise StageFailed("validate", f"generated data violates the schema:\n{lines}")
        dataset = Dataset(
            id=new_dataset_id(),
            name=f"smoke-{name}",
            ddl=ddl_path.read_text(encoding="utf-8"),
            schema=schema,
            tables=tables,
            plan=plan.model_dump(mode="json"),
            report=report.model_dump(mode="json"),
            params={"rows_per_table": rows, "seed": seed, "llm": False, "source": "scripts/e2e_smoke.py"},
        )
        root = settings.datasets_dir if keep else Path(tempfile.mkdtemp(prefix="smoke-datasets-"))
        try:
            datasets.save(dataset, root)
            reloaded = datasets.load(dataset.id, root)
        except (OSError, csvio.CsvFormatError, DDLParseError) as e:
            raise StageFailed("save/load", f"{e} — check the dataset directory {root}") from e
        if reloaded.row_counts() != dataset.row_counts():
            raise StageFailed(
                "save/load",
                f"row counts changed on reload: {reloaded.row_counts()} vs {dataset.row_counts()}",
            )
        stage("save+load")
        try:
            result = postgres.load_dataset(dataset, settings)
        except postgres.LoadError as e:
            raise StageFailed("postgres", str(e)) from e
        stage("postgres")
        if result.row_counts != dataset.row_counts():
            raise StageFailed(
                "postgres",
                f"PostgreSQL counts {result.row_counts} differ from generated {dataset.row_counts()}",
            )
        width = max(len(t) for t in schema.table_names)
        for table, count in result.row_counts.items():
            print(f"  {table:<{width}}  {count:>6} rows")
        print(f"  FK constraints: {result.fk_constraints} · schema {result.schema_name}")
        total = time.perf_counter() - started
        print(
            "  timings: " + ", ".join(f"{k} {v:.1f}s" for k, v in timings.items()) + f" · total {total:.1f}s"
        )
        print(f"OK   {name}: {dataset.total_rows} rows across {len(schema.tables)} tables")
        return True
    except StageFailed as e:
        print(f"FAIL {name} at stage {e.stage}: {e}")
        return False
    finally:
        if not keep and dataset is not None:
            try:
                postgres.drop_dataset(dataset.id, settings)
            except postgres.LoadError as e:
                print(f"  cleanup warning: {e}")
        if not keep and root is not None:
            shutil.rmtree(root, ignore_errors=True)
        if keep and dataset is not None:
            print(f"  kept: data/datasets/{dataset.id} and PostgreSQL schema {dataset.pg_schema}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--schema", default="all", help="all | library | restaurants | company | path to a DDL file"
    )
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--keep", action="store_true", help="keep the dataset on disk and in PostgreSQL")
    parser.add_argument("--llm", action="store_true", help="use Gemini for planning and text pools (F3.4)")
    args = parser.parse_args()
    if args.llm:
        print("LLM mode is not wired yet — it arrives with F3.4 (generation.engine). Run without --llm.")
        return 2
    if args.schema == "all":
        targets = list(SAMPLES.items())
    elif args.schema in SAMPLES:
        targets = [(args.schema, SAMPLES[args.schema])]
    else:
        path = Path(args.schema)
        if not path.exists():
            print(
                f"schema {args.schema!r} is neither a sample name ({', '.join(SAMPLES)}) nor an existing file"
            )
            return 2
        targets = [(path.stem, path)]
    settings = Settings()
    outcomes = [run_one(name, path, args.rows, args.seed, args.keep, settings) for name, path in targets]
    print()
    if all(outcomes):
        print(f"e2e smoke: all {len(outcomes)} schema(s) passed")
        return 0
    failed = [name for (name, _), ok in zip(targets, outcomes, strict=True) if not ok]
    print(f"e2e smoke: FAILED for {', '.join(failed)} — see messages above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
