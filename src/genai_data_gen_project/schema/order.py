"""Table generation order with cycle breaking (F1.2).

Rules (docs/db-rules.md, DECISIONS.md#2026-08-25-postgres-translation-rules-and-per-dataset-schemas):
- Parents are generated before children so FK values can be sampled from existing rows.
- Self-referencing FKs are always *deferred*: filled in a second pass from the table's own rows.
- Cycles are broken by deferring FKs whose columns are all nullable (they are left NULL in the first pass and
  filled in the second pass once the parent rows exist). Deterministic: ties follow DDL declaration order.
- A cycle made only of NOT NULL FKs cannot be generated → `UnsatisfiableSchemaError` naming the tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import ForeignKey, Schema


class UnsatisfiableSchemaError(ValueError):
    """The FK graph has a cycle that only NOT NULL foreign keys participate in."""


@dataclass(frozen=True)
class DeferredFK:
    table: str
    fk: ForeignKey
    reason: str  # "self-reference" | "cycle"


@dataclass
class GenerationOrder:
    tables: list[str]
    deferred: list[DeferredFK] = field(default_factory=list)

    def position(self, table: str) -> int:
        lowered = [t.lower() for t in self.tables]
        return lowered.index(table.lower())

    def deferred_for(self, table: str) -> list[ForeignKey]:
        return [d.fk for d in self.deferred if d.table.lower() == table.lower()]

    def is_deferred(self, table: str, fk: ForeignKey) -> bool:
        return any(d.table.lower() == table.lower() and d.fk is fk for d in self.deferred)


def generation_order(schema: Schema) -> GenerationOrder:
    """Topologically order tables; defer self-references and nullable FKs that close cycles."""
    names = [t.name for t in schema.tables]
    deferred: list[DeferredFK] = []
    # child -> parent -> FKs (self-references are deferred up-front and never enter the graph)
    edges: dict[str, dict[str, list[ForeignKey]]] = {n: {} for n in names}
    for table in schema.tables:
        for fk in table.foreign_keys:
            parent = schema.table(fk.ref_table).name
            if parent == table.name:
                deferred.append(DeferredFK(table.name, fk, "self-reference"))
                continue
            edges[table.name].setdefault(parent, []).append(fk)

    done: list[str] = []
    remaining = list(names)
    while remaining:
        ready = [n for n in remaining if all(p in done for p in edges[n])]
        if ready:
            done.append(ready[0])
            remaining.remove(ready[0])
            continue
        # Stalled: every remaining table waits on another remaining table → pick the table whose blocking FKs
        # are all nullable, preferring the fewest blocking parents, then declaration order.
        candidates: list[tuple[int, int, str]] = []
        for n in remaining:
            blocking = {p: fks for p, fks in edges[n].items() if p not in done}
            table = schema.table(n)
            if all(table.fk_is_nullable(fk) for fks in blocking.values() for fk in fks):
                candidates.append((len(blocking), remaining.index(n), n))
        if not candidates:
            raise UnsatisfiableSchemaError(_describe_cycle(schema, remaining, edges, done))
        _, _, chosen = min(candidates)
        for parent in [p for p in edges[chosen] if p not in done]:
            for fk in edges[chosen].pop(parent):
                deferred.append(DeferredFK(chosen, fk, "cycle"))
    return GenerationOrder(tables=done, deferred=deferred)


def _describe_cycle(
    schema: Schema, remaining: list[str], edges: dict[str, dict[str, list[ForeignKey]]], done: list[str]
) -> str:
    parts: list[str] = []
    for n in remaining:
        for parent, fks in edges[n].items():
            if parent in done:
                continue
            for fk in fks:
                nullable = schema.table(n).fk_is_nullable(fk)
                parts.append(
                    f"{n}.{','.join(fk.columns)} → {parent} ({'nullable' if nullable else 'NOT NULL'})"
                )
    return (
        "cannot order tables for generation: cycle among "
        + ", ".join(remaining)
        + " via NOT NULL foreign keys: "
        + "; ".join(parts)
        + " — make at least one FK column in the cycle nullable"
    )
