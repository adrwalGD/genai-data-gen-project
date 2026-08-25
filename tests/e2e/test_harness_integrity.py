"""End-to-end checks of the harness itself: the feature list tooling and the entry docs stay consistent."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "features.py"), *args],
        capture_output=True,
        text=True,
        env=full_env,
    )


def test_feature_list_validates_and_has_at_most_one_active() -> None:
    result = run("validate")
    assert result.returncode == 0, result.stdout + result.stderr


def test_next_feature_is_resolvable() -> None:
    result = run("next")
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout.strip()
    assert out.startswith("F") or out == "none", out


def test_claude_md_stays_a_router() -> None:
    lines = (REPO / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 200, f"CLAUDE.md has {len(lines)} lines; move detail into docs/ (rule 12)"
    text = "\n".join(lines)
    for required in ("Hard constraints", "Session protocol", "docs/features.md", "make check"):
        assert required in text


def test_pass_requires_a_fresh_make_check_marker(tmp_path: Path) -> None:
    """Pass-state gating (lecture 8): a feature cannot be declared passing without a fresh `make check`."""
    copy = tmp_path / "features.md"
    shutil.copy(REPO / "docs" / "features.md", copy)
    marker = tmp_path / "check.ok"
    env = {"FEATURES_FILE": str(copy), "CHECK_MARKER": str(marker)}
    active = run("next", env=env).stdout.strip()
    if not active.startswith("F"):
        return  # nothing left to activate — the gate is exercised on real features in normal sessions
    activate = run("activate", active, env=env)
    if activate.returncode != 0:  # `next` may return an already-active feature
        assert "already active" in activate.stderr or "expected one of" in activate.stderr, activate.stderr
    refused = run("pass", active, "--evidence", "fake", env=env)
    assert refused.returncode != 0 and "make check" in refused.stderr, refused.stderr
    marker.touch()  # a marker newer than every watched file → fresh
    accepted = run("pass", active, "--evidence", "fake evidence for gate test", env=env)
    assert accepted.returncode == 0, accepted.stderr
    assert "passing" in copy.read_text(encoding="utf-8").split(f"### {active}")[1][:400]
