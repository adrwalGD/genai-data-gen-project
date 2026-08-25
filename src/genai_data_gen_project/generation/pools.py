"""LLM text pools (F3.3): realistic string values for `text_pool` columns, fetched in parallel batches.

For every TextPoolRecipe column the planner produced, `pool_requests()` sizes a pool (unique columns need at
least `rows` distinct values; others a sample), `fill_pools()` asks Gemini for values in batches of ≤ 50 via a
dynamic JSON schema, deduplicates, truncates to the VARCHAR length, tops up shortfalls with more batches and
finally with Faker, and never raises on model errors — a failed pool degrades to Faker with a note.
"""

from __future__ import annotations

import contextvars
import logging
import math
import random
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from faker import Faker

from ..llm.client import LLMBackend, LLMError
from ..schema.models import Schema
from ..schema.summary import table_summary
from .recipes import GenerationPlan, TextPoolRecipe

_log = logging.getLogger(__name__)

BATCH_SIZE = 50
MAX_UNIQUE_POOL = 1500  # beyond this Faker fills the rest (cost/latency guard)
SAMPLE_POOL = 80  # distinct values for non-unique columns
MAX_ROUNDS = 3  # extra top-up rounds when the model returns duplicates / too few values

POOL_SCHEMA = {
    "title": "TextPool",
    "type": "object",
    "properties": {"values": {"type": "array", "items": {"type": "string"}}},
    "required": ["values"],
}

POOL_SYSTEM = (
    "You generate realistic, diverse text values for one column of a synthetic SQL dataset. "
    "Return ONLY JSON with a `values` array of distinct strings. No numbering, no duplicates, no quotes "
    "around values, no explanations. Match the requested language/style exactly and respect the "
    "maximum length."
)


@dataclass
class PoolRequest:
    table: str
    column: str
    brief: str
    unique: bool
    size: int
    max_length: int | None
    examples: list[str] = field(default_factory=list)
    fallback_provider: str = "sentence"
    fallback_kwargs: dict[str, str | int | float | bool] = field(default_factory=dict)
    table_context: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.table, self.column)


@dataclass
class PoolResult:
    pools: dict[tuple[str, str], list[str]]
    notes: list[str]
    llm_values: int = 0
    fallback_values: int = 0


def pool_requests(
    plan: GenerationPlan, schema: Schema, *, max_unique: int = MAX_UNIQUE_POOL
) -> list[PoolRequest]:
    requests: list[PoolRequest] = []
    for tp in plan.tables:
        if not schema.has_table(tp.table):
            continue
        table = schema.table(tp.table)
        for cp in tp.columns:
            r = cp.recipe
            if not isinstance(r, TextPoolRecipe) or not table.has_column(cp.column):
                continue
            col = table.column(cp.column)
            size = min(tp.rows, max_unique) if r.unique else min(max(tp.rows, 1), SAMPLE_POOL)
            if size <= 0:
                continue
            requests.append(
                PoolRequest(
                    table=table.name,
                    column=col.name,
                    brief=r.brief,
                    unique=r.unique,
                    size=size,
                    max_length=col.max_length,
                    examples=list(r.examples),
                    fallback_provider=r.fallback_provider,
                    fallback_kwargs=dict(r.fallback_kwargs),
                    table_context=table_summary(table),
                )
            )
    return requests


def fill_pools(
    plan: GenerationPlan,
    schema: Schema,
    llm: LLMBackend | None,
    *,
    instructions: str | None = None,
    temperature: float = 0.9,
    max_workers: int = 4,
    seed: int = 0,
    progress: Callable[[str], None] | None = None,
) -> PoolResult:
    """Fill every text pool of the plan. With `llm=None` everything comes from Faker (offline mode)."""
    requests = pool_requests(plan, schema)
    result = PoolResult(pools={}, notes=[])
    if not requests:
        return result
    faker = Faker("en_US")
    faker.seed_instance(seed)
    if llm is None:
        for req in requests:
            result.pools[req.key] = _faker_values(faker, req, req.size)
            result.fallback_values += req.size
        result.notes.append(f"text pools filled by Faker for {len(requests)} column(s) (no LLM)")
        return result

    def work(req: PoolRequest) -> tuple[PoolRequest, list[str], list[str], str | None]:
        try:
            values, batch_notes = _llm_values(llm, req, instructions, temperature)
            return req, values, batch_notes, None
        except LLMError as e:
            return req, [], [], str(e)

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        # copy the caller's context per task so OpenTelemetry spans (Langfuse) nest under the current trace
        futures = [pool.submit(contextvars.copy_context().run, work, req) for req in requests]
        for future in futures:
            req, values, batch_notes, error = future.result()
            result.notes.extend(batch_notes)
            if error:
                result.notes.append(f"{req.table}.{req.column}: pool fell back to Faker — {error}")
            result.llm_values += min(len(values), req.size)  # counted before any Faker top-up
            if len(values) < req.size:
                shortfall = req.size - len(values)
                if values:
                    result.notes.append(
                        f"{req.table}.{req.column}: {shortfall} of {req.size} values from Faker"
                    )
                values = _extend_unique(values, _faker_values(faker, req, shortfall * 2), req.size)
                result.fallback_values += shortfall
            result.pools[req.key] = values[: req.size] if req.unique else values
            if progress:
                progress(f"{req.table}.{req.column}: {len(result.pools[req.key])} values")
    return result


# --- LLM side -------------------------------------------------------------------------------------
def _llm_values(
    llm: LLMBackend, req: PoolRequest, instructions: str | None, temperature: float
) -> tuple[list[str], list[str]]:
    """Distinct values from the model; a bad batch costs one batch, not the pool. Returns (values, notes)."""
    values: list[str] = []
    notes: list[str] = []
    rounds = 0
    failed_batches = 0
    while len(values) < req.size and rounds <= MAX_ROUNDS:
        remaining = req.size - len(values)
        ask = math.ceil(
            remaining * (1.3 if rounds else 1.0)
        )  # duplicates are common in top-up rounds: over-request
        for n in [min(BATCH_SIZE, ask - i) for i in range(0, ask, BATCH_SIZE)]:
            avoid = values if len(values) <= 40 else random.Random(len(values)).sample(values, 40)
            prompt = _prompt(req, n, instructions, avoid=avoid)
            try:
                data = _batch(llm, prompt, temperature)
            except LLMError as e:
                failed_batches += 1
                notes.append(f"{req.table}.{req.column}: a batch of {n} values failed ({e.message[:120]})")
                if failed_batches >= 3:
                    raise  # systematic failure → the caller falls back to Faker for this pool
                continue
            raw = data.get("values", []) if isinstance(data, dict) else []
            values = _extend_unique(
                values, [str(v) for v in raw if isinstance(v, str | int | float)], req.size, req.max_length
            )
            if len(values) >= req.size:
                break
        rounds += 1
    return values, notes


def _batch(llm: LLMBackend, prompt: str, temperature: float) -> Any:
    """One batch call with a single retry (the client retries transport errors; this covers bad JSON)."""
    try:
        return llm.generate_json(POOL_SCHEMA, prompt, system=POOL_SYSTEM, temperature=temperature)
    except LLMError as first:
        if first.retryable:
            raise
        _log.warning("pool batch failed (%s); retrying once at a lower temperature", first.message)
        return llm.generate_json(
            POOL_SCHEMA, prompt, system=POOL_SYSTEM, temperature=max(0.3, temperature - 0.3)
        )


def _prompt(req: PoolRequest, n: int, instructions: str | None, avoid: list[str]) -> str:
    parts = [
        f"Generate {n} distinct values for column `{req.column}` of table `{req.table}`.",
        f"What the values are: {req.brief}.",
        f"Table definition for context:\n{req.table_context}",
    ]
    if req.max_length:
        parts.append(f"Maximum length: {req.max_length} characters.")
    if req.examples:
        parts.append("Style examples (do not repeat them): " + "; ".join(req.examples[:5]))
    if instructions and instructions.strip():
        parts.append(f"User instructions for the whole dataset: {instructions.strip()}")
    if avoid:
        parts.append("Already used — do not repeat these or near-duplicates: " + "; ".join(avoid))
    return "\n".join(parts)


def _extend_unique(
    current: list[str], new: list[str], target: int, max_length: int | None = None
) -> list[str]:
    seen = set(current)
    out = list(current)
    for value in new:
        text = " ".join(value.split())
        if max_length:
            text = text[:max_length].rstrip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
        if len(out) >= target:
            break
    return out


def _faker_values(faker: Faker, req: PoolRequest, n: int) -> list[str]:
    provider: Callable[..., Any] = getattr(faker, req.fallback_provider, None) or faker.sentence
    values: list[str] = []
    seen: set[str] = set()
    attempts = 0
    while len(values) < n and attempts < n * 20 + 20:
        attempts += 1
        text = str(provider(**req.fallback_kwargs))
        if req.max_length:
            text = text[: req.max_length].rstrip()
        if text and text not in seen:
            seen.add(text)
            values.append(text)
    while len(values) < n:  # exhausted provider variety → deterministic numbered fallback
        candidate = f"{req.column} {len(values) + 1}"
        if req.max_length:
            candidate = candidate[: req.max_length]
        if candidate not in seen:
            seen.add(candidate)
            values.append(candidate)
    return values


def fetch_pool(
    llm: LLMBackend, request: PoolRequest, *, temperature: float = 0.9, seed: int = 0
) -> list[str]:
    """Values for one pool request right now (feedback edits); Faker fills any shortfall."""
    faker = Faker("en_US")
    faker.seed_instance(seed)
    try:
        values, _notes = _llm_values(llm, request, None, temperature)
    except LLMError as e:
        _log.warning("pool %s.%s fell back to Faker: %s", request.table, request.column, e)
        values = []
    if len(values) < request.size:
        values = _extend_unique(
            values, _faker_values(faker, request, (request.size - len(values)) * 2), request.size
        )
    return values
