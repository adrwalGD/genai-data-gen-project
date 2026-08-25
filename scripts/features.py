#!/usr/bin/env python3
"""Feature-list tooling for docs/features.md — the harness primitive (CLAUDE.md rule 8, docs/loop.md).

Usage:
  features.py list                                 id | state | milestone | title (+ summary)
  features.py next                                 active feature id, else first not_started, else "none"
  features.py validate                             enforce the rules; exit 1 on violation
  features.py activate ID                          not_started|blocked -> active (WIP = 1)
  features.py pass ID --evidence "cmd → output; commit abc"   active -> passing (evidence mandatory)
  features.py set ID STATE [--evidence "..."]      explicit transition (block, or reopen passing -> active)

Only the standard library is used so the script runs anywhere (`python3 scripts/features.py`).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEATURES_FILE = ROOT / "docs" / "features.md"
STATES = ("not_started", "active", "blocked", "passing")
REQUIRED_FIELDS = ("milestone", "state", "behavior", "verification", "evidence")
HEADER_RE = re.compile(r"^### (F\d+\.\d+) — (.+)$")
FIELD_RE = re.compile(r"^- (milestone|state|behavior|verification|evidence): ?(.*)$")
EMPTY_EVIDENCE = {"", "—", "-", "n/a", "none"}


@dataclass
class Feature:
    id: str
    title: str
    header_line: int
    fields: dict[str, str] = field(default_factory=dict)
    field_lines: dict[str, int] = field(default_factory=dict)

    @property
    def state(self) -> str:
        return self.fields.get("state", "")

    @property
    def evidence(self) -> str:
        return self.fields.get("evidence", "").strip()


def parse(lines: list[str]) -> list[Feature]:
    feats: list[Feature] = []
    cur: Feature | None = None
    for i, line in enumerate(lines):
        m = HEADER_RE.match(line)
        if m:
            cur = Feature(m.group(1), m.group(2).strip(), i)
            feats.append(cur)
            continue
        if line.startswith("## "):
            cur = None
            continue
        if cur is None:
            continue
        f = FIELD_RE.match(line)
        if f:
            cur.fields[f.group(1)] = f.group(2).strip()
            cur.field_lines[f.group(1)] = i
    return feats


def load() -> tuple[list[str], list[Feature]]:
    lines = FEATURES_FILE.read_text(encoding="utf-8").splitlines()
    return lines, parse(lines)


def save(lines: list[str]) -> None:
    FEATURES_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate(feats: list[Feature]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    active = [f.id for f in feats if f.state == "active"]
    if len(active) > 1:
        errors.append(f"more than one active feature (WIP must be 1): {', '.join(active)}")
    for f in feats:
        if f.id in seen:
            errors.append(f"{f.id}: duplicate id")
        seen.add(f.id)
        for name in REQUIRED_FIELDS:
            if name not in f.fields:
                errors.append(f"{f.id}: missing field '- {name}:'")
        if f.state not in STATES:
            errors.append(f"{f.id}: invalid state '{f.state}' (allowed: {', '.join(STATES)})")
        if f.state == "passing" and f.evidence in EMPTY_EVIDENCE:
            errors.append(
                f"{f.id}: passing without evidence — record the verification command + output + commit"
            )
        if f.state == "blocked" and f.evidence in EMPTY_EVIDENCE:
            errors.append(f"{f.id}: blocked without a reason in evidence")
        if not f.fields.get("verification"):
            errors.append(f"{f.id}: empty verification command")
    if not feats:
        errors.append("no features found — expected '### F<n>.<m> — title' sections")
    return errors


def find(feats: list[Feature], fid: str) -> Feature:
    for f in feats:
        if f.id == fid:
            return f
    sys.exit(f"error: unknown feature id {fid!r} (see `features.py list`)")


def set_field(lines: list[str], feat: Feature, name: str, value: str) -> None:
    idx = feat.field_lines.get(name)
    if idx is None:
        sys.exit(f"error: {feat.id} has no '- {name}:' line to update")
    lines[idx] = f"- {name}: {value}"


def cmd_list(feats: list[Feature]) -> int:
    width = max(len(f.title) for f in feats) if feats else 10
    print(f"{'id':<6} {'state':<12} {'ms':<4} title")
    for f in feats:
        print(f"{f.id:<6} {f.state:<12} {f.fields.get('milestone', '?'):<4} {f.title[:width]}")
    counts = {s: sum(1 for f in feats if f.state == s) for s in STATES}
    print(f"\n{len(feats)} features — " + ", ".join(f"{k}: {v}" for k, v in counts.items()))
    return 0


def cmd_next(feats: list[Feature]) -> int:
    active = [f for f in feats if f.state == "active"]
    if active:
        print(active[0].id)
        return 0
    for f in feats:
        if f.state == "not_started":
            print(f.id)
            return 0
    print("none")
    return 0


def cmd_validate(feats: list[Feature]) -> int:
    errors = validate(feats)
    if errors:
        print("docs/features.md is INVALID:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"docs/features.md OK ({len(feats)} features)")
    return 0


def transition(
    fid: str, new_state: str, evidence: str | None, *, allowed_from: tuple[str, ...] | None
) -> int:
    lines, feats = load()
    feat = find(feats, fid)
    if allowed_from is not None and feat.state not in allowed_from:
        sys.exit(f"error: {fid} is '{feat.state}', expected one of {allowed_from} for this transition")
    if new_state == "active":
        others = [f.id for f in feats if f.state == "active" and f.id != fid]
        if others:
            sys.exit(f"error: {others[0]} is already active — finish or block it first (WIP = 1)")
    if new_state == "passing" and (evidence or feat.evidence) in EMPTY_EVIDENCE:
        sys.exit("error: --evidence is required to mark a feature passing")
    if feat.state == "passing" and new_state != "passing":
        print(
            f"warning: reopening {fid} from passing → {new_state}; record why in PROGRESS.md → Known Issues"
        )
    set_field(lines, feat, "state", new_state)
    if evidence:
        set_field(lines, feat, "evidence", evidence)
    errors = validate(parse(lines))
    if errors:
        sys.exit("error: transition would make the file invalid:\n  " + "\n  ".join(errors))
    save(lines)
    print(f"{fid}: {feat.state} → {new_state}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("next")
    sub.add_parser("validate")
    a = sub.add_parser("activate")
    a.add_argument("id")
    ps = sub.add_parser("pass")
    ps.add_argument("id")
    ps.add_argument("--evidence", required=True)
    st = sub.add_parser("set")
    st.add_argument("id")
    st.add_argument("state", choices=STATES)
    st.add_argument("--evidence")
    args = p.parse_args(argv)

    if args.cmd in {"list", "next", "validate"}:
        _, feats = load()
        return {"list": cmd_list, "next": cmd_next, "validate": cmd_validate}[args.cmd](feats)
    if args.cmd == "activate":
        return transition(args.id, "active", None, allowed_from=("not_started", "blocked"))
    if args.cmd == "pass":
        return transition(args.id, "passing", args.evidence, allowed_from=("active",))
    return transition(args.id, args.state, args.evidence, allowed_from=None)


if __name__ == "__main__":
    sys.exit(main())
