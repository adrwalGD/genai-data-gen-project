"""End-to-end checks of the harness itself: the feature list tooling and the entry docs stay consistent."""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "features.py"), *args], capture_output=True, text=True
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
