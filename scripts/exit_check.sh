#!/usr/bin/env bash
# Session-end gate (CLAUDE.md rule 11, lecture 12). `make exit-check` runs `make check` first, then this script.
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0
problem() { printf 'EXIT-CHECK FAIL: %s\n  fix: %s\n\n' "$1" "$2"; fail=1; }

# 1. feature list obeys the state rules (≤1 active, passing has evidence, ...)
if ! out=$(python3 scripts/features.py validate 2>&1); then problem "docs/features.md is invalid:"$'\n'"$out" "fix the listed entries (scripts/features.py set/pass)"; fi

# 2. PROGRESS.md was touched this session: untracked (new), modified vs HEAD, or part of the HEAD commit
touched=0
if ! git ls-files --error-unmatch PROGRESS.md >/dev/null 2>&1; then touched=1
elif ! git diff --quiet HEAD -- PROGRESS.md; then touched=1
elif git show --name-only --format= HEAD | grep -qx 'PROGRESS.md'; then touched=1
fi
[ "$touched" -eq 1 ] || problem "PROGRESS.md was neither modified nor committed in HEAD" \
  "update Current State / Completed / In Progress / Known Issues / Next Steps / Session log before ending"

# 2b. PROGRESS.md "Active feature" line agrees with docs/features.md (the two state files must not drift)
active=$(python3 scripts/features.py active 2>/dev/null || echo "?")
progress_active=$(grep -oE '^- Active feature: (F[0-9]+\.[0-9]+|none)' PROGRESS.md | head -1 | sed -E 's/^- Active feature: //')
[ "$active" = "${progress_active:-?}" ] || problem "PROGRESS.md says active feature '${progress_active:-<missing>}' but docs/features.md says '$active'" \
  "set the '- Active feature:' line in PROGRESS.md to '$active' (or '- Active feature: none')"

# 3. debug leftovers
hits=$(grep -rn --include='*.py' -E 'breakpoint\(\)|import pdb|pdb\.set_trace|st\.write\("debug|console\.log' src scripts tests || true)
[ -n "$hits" ] && problem "debug leftovers:"$'\n'"$hits" "remove them"

# 4. TODO/FIXME must reference a feature id, e.g. TODO(F6.2): ...
hits=$(grep -rn --include='*.py' -E 'TODO|FIXME' src tests scripts | grep -vE '(TODO|FIXME)\(F[0-9]+\.[0-9]+\)' || true)
[ -n "$hits" ] && problem "TODO/FIXME without a feature id:"$'\n'"$hits" "write TODO(F<n>.<m>): ... or resolve it now"

# 5. stray scratch files in the repository root
hits=$(ls -1 | grep -E '\.(tmp|log|bak|orig)$|^debug_|^scratch|^tmp' || true)
[ -n "$hits" ] && problem "stray files in repo root: $hits" "delete them or use the scratchpad directory"

# 6. xfail/skip markers added as a shortcut must carry a reason
hits=$(grep -rn --include='*.py' -E '@pytest\.mark\.(xfail|skip)\(\)|@pytest\.mark\.(xfail|skip)$' tests || true)
[ -n "$hits" ] && problem "skip/xfail without reason:"$'\n'"$hits" "add reason=... referencing the feature id, or fix the test"

echo "git status (uncommitted changes must be committed or reverted before ending):"
git status --short | head -40
if [ "$fail" -eq 0 ]; then echo; echo "exit-check: OK — update PROGRESS.md if anything changed since, then commit."; fi
exit "$fail"
