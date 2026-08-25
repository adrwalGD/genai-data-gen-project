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
from genai_data_gen_project.generation import engine  # noqa: E402
from genai_data_gen_project.generation.expander import ExpansionError  # noqa: E402
from genai_data_gen_project.llm.client import GeminiClient, LLMError  # noqa: E402
from genai_data_gen_project.schema.order import UnsatisfiableSchemaError  # noqa: E402
from genai_data_gen_project.schema.parser import DDLParseError  # noqa: E402
from genai_data_gen_project.storage import csvio, datasets, postgres  # noqa: E402
from genai_data_gen_project.storage.dataset import Dataset  # noqa: E402

SAMPLES = {
    "library": ROOT / "project-spec" / "library_mgm_schema.ddl",
    "restaurants": ROOT / "project-spec" / "restrurants_schema.ddl",
    "company": ROOT / "project-spec" / "company_employee_schema.ddl",
}


class StageFailed(Exception):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def run_one(
    name: str, ddl_path: Path, rows: int, seed: int, keep: bool, settings: Settings, llm: GeminiClient | None
) -> bool:
    mode = f"Gemini {llm.model}" if llm else "offline"
    print(f"\n=== {name}: {ddl_path.name} — {rows} rows/table, seed {seed}, {mode} ===")
    started = time.perf_counter()
    timings: dict[str, float] = {}
    engine_timings: dict[str, float] = {}
    root: Path | None = None
    dataset: Dataset | None = None

    def stage(label: str) -> float:
        now = time.perf_counter()
        timings[label] = now - started - sum(timings.values())
        return now

    try:
        try:
            ddl = ddl_path.read_text(encoding="utf-8")
        except OSError as e:
            raise StageFailed("read", f"{e} — check the DDL path {ddl_path}") from e
        request = engine.GenerationRequest(
            ddl=ddl, rows_per_table=rows, seed=seed, name=f"smoke-{name}", temperature=0.7,
            extra_params={"source": "scripts/e2e_smoke.py"},
        )  # fmt: skip
        try:
            dataset = engine.generate(request, llm, settings, progress=lambda msg: print(f"  … {msg}"))
        except DDLParseError as e:
            raise StageFailed("parse", f"{e} — fix the DDL file {ddl_path}") from e
        except (ExpansionError, UnsatisfiableSchemaError) as e:
            raise StageFailed("expand", f"{e} — adjust the schema/plan (generation/heuristics.py)") from e
        except LLMError as e:
            raise StageFailed("llm", f"{e}") from e
        stage("generate")
        engine_timings.update(dict(dataset.params.get("timings_s", {})))
        for note in dataset.params.get("notes", []):
            print(f"  note: {note}")
        report = engine.report_of(dataset)
        if report is None or not report.ok:
            issues = report.issues[:20] if report else []
            lines = "\n".join(f"    {issue}" for issue in issues)
            raise StageFailed("validate", f"generated data violates the schema:\n{lines}")
        if dataset.params.get("trace_id"):
            print(f"  langfuse trace: {dataset.params['trace_id']}")
        schema = dataset.schema
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
        print("  engine:  " + ", ".join(f"{k} {v:.1f}s" for k, v in engine_timings.items()))
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
    llm = GeminiClient(settings) if args.llm else None
    outcomes = [run_one(name, path, args.rows, args.seed, args.keep, settings, llm) for name, path in targets]
    print()
    if all(outcomes):
        print(f"e2e smoke: all {len(outcomes)} schema(s) passed")
        return 0
    failed = [name for (name, _), ok in zip(targets, outcomes, strict=True) if not ok]
    print(f"e2e smoke: FAILED for {', '.join(failed)} — see messages above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
