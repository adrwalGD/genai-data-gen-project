"""Generation engine (F3.4): one call from DDL text to a validated `Dataset`.

Pipeline: parse → heuristic plan → (Gemini planner overrides) → (Gemini text pools) → deterministic expand
→ validate → Dataset with provenance (plan, report, params incl. timings and Langfuse trace id). With
`llm=None` everything runs offline. LLM failures degrade with a note instead of failing the run (integrity
never depends on the model — CLAUDE.md rule 10). Every run is a Langfuse trace `data_generation` when enabled.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..llm.client import LLMBackend, LLMError
from ..observability import current_trace_id, flush, traced
from ..schema.models import Schema
from ..schema.parser import parse_ddl
from ..storage.dataset import Dataset, default_dataset_name, new_dataset_id
from . import heuristics, planner, pools
from .consistency import check_consistency
from .expander import expand
from .recipes import GenerationPlan
from .validator import ValidationReport, validate

_log = logging.getLogger(__name__)
Progress = Callable[[str], None]


@dataclass
class GenerationRequest:
    ddl: str
    instructions: str | None = None
    rows_per_table: int | dict[str, int] = 100
    temperature: float = 0.7
    seed: int = 0
    name: str | None = None
    session_id: str | None = None
    extra_params: dict[str, Any] = field(default_factory=dict)


def generate(
    request: GenerationRequest,
    llm: LLMBackend | None,
    settings: Settings | None = None,
    *,
    progress: Progress | None = None,
) -> Dataset:
    """Run the whole pipeline. DDLParseError / ExpansionError propagate; LLM problems become notes."""
    settings = settings or get_settings()
    report_progress = progress or (lambda _msg: None)
    with traced(
        "data_generation",
        session_id=request.session_id,
        tags=["data-generation", "llm" if llm else "offline"],
        rows_per_table=request.rows_per_table,
        temperature=request.temperature,
        seed=request.seed,
        model=getattr(llm, "model", None),
    ):
        timings: dict[str, float] = {}
        notes: list[str] = []
        clock = time.perf_counter()

        def lap(stage: str) -> None:
            nonlocal clock
            now = time.perf_counter()
            timings[stage] = round(now - clock, 3)
            clock = now

        report_progress("Parsing DDL")
        schema = parse_ddl(request.ddl)
        notes.extend(f"schema: {n}" for n in schema.notes)
        lap("parse")

        report_progress("Planning (heuristics)")
        plan = heuristics.plan(schema, request.rows_per_table, default_rows=settings.default_rows_per_table)
        if llm is not None:
            report_progress("Planning with Gemini")
            plan = _plan_with_llm(schema, plan, request, llm, notes, settings)
        lap("plan")

        report_progress("Text pools" + (" with Gemini" if llm else " (Faker)"))
        pool_result = pools.fill_pools(
            plan,
            schema,
            llm,
            instructions=request.instructions,
            temperature=min(max(request.temperature, 0.3), 1.2),
            max_workers=settings.llm_max_concurrency,
            seed=request.seed,
            progress=lambda msg: report_progress(f"pool {msg}"),
        )
        notes.extend(f"pools: {n}" for n in pool_result.notes)
        lap("pools")

        report_progress("Generating rows")
        tables = expand(schema, plan, seed=request.seed, pools=pool_result.pools)
        lap("expand")

        report_progress("Validating")
        report = validate(schema, tables, expected_rows={t.table: t.rows for t in plan.tables})
        report.issues.extend(check_consistency(schema, plan, tables))
        report.ok = not report.issues
        lap("validate")

        dataset = Dataset(
            id=new_dataset_id(),
            name=request.name or default_dataset_name(schema),
            ddl=request.ddl,
            schema=schema,
            tables=tables,
            plan=plan.model_dump(mode="json"),
            report=report.model_dump(mode="json"),
            instructions=request.instructions,
            params={
                "rows_per_table": request.rows_per_table,
                "temperature": request.temperature,
                "seed": request.seed,
                "llm": llm is not None,
                "model": getattr(llm, "model", None),
                "pool_values": {"llm": pool_result.llm_values, "fallback": pool_result.fallback_values},
                "timings_s": timings,
                "notes": notes + plan.notes,
                "trace_id": current_trace_id(),
                **request.extra_params,
            },
        )
        _log.info(
            "generated dataset %s: %s rows, ok=%s, llm=%s, timings=%s",
            dataset.id, dataset.total_rows, report.ok, llm is not None, timings,
        )  # fmt: skip
    flush()
    return dataset


def _plan_with_llm(
    schema: Schema,
    base: GenerationPlan,
    request: GenerationRequest,
    llm: LLMBackend,
    notes: list[str],
    settings: Settings,
) -> GenerationPlan:
    try:
        merged, _output = planner.plan_with_llm(
            schema,
            request.instructions,
            request.rows_per_table,
            llm,
            temperature=min(max(request.temperature * 0.6, 0.1), 0.8),
            base=base,
            max_rows=settings.max_rows_per_table,
        )
        return merged
    except LLMError as e:
        notes.append(f"planner: Gemini failed ({e}); heuristic plan used")
        _log.warning("LLM planner failed, using heuristics: %s", e)
        return base


def report_of(dataset: Dataset) -> ValidationReport | None:
    return None if dataset.report is None else ValidationReport.model_validate(dataset.report)


def plan_of(dataset: Dataset) -> GenerationPlan | None:
    return None if dataset.plan is None else GenerationPlan.model_validate(dataset.plan)
