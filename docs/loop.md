# The work loop, definition of done, and gate verification

## One iteration (WIP = 1)
1. **Pick**: `uv run python scripts/features.py next` (resumes the `active` feature if any). Activate it:
   `uv run python scripts/features.py activate F1.1`.
2. **Contract**: re-read the feature's *behavior* and *verification*. If the behavior is ambiguous, decide, and write
   the decision into DECISIONS.md *before* coding. Do not widen the scope.
3. **Implement** the smallest change that satisfies the behavior. Tests first when the module is pure (schema,
   generation, guard, charts). Put knowledge next to code (module docstrings, `CONSTRAINTS` notes) when non-obvious.
4. **Verify — three layers, in order, no skipping** (lecture 9):
   - L1 static: `make lint typecheck arch-check`
   - L2 runtime: the feature's verification command (unit / integration / llm) — must actually run now
   - L3 system: `make check` (+ `make e2e` when the change touches generation/storage; `make test-ui` for UI)
5. **Evidence**: `uv run python scripts/features.py pass F1.1 --evidence "make test K=parser → 18 passed; commit abc1234"`.
   Evidence must name the command, the key output line(s), and a commit hash or Langfuse trace id where relevant.
6. **Commit** one atomic change: `feat(schema): DDL parser → IR for sample schemas — needed by generation and loader`.
7. **Record**: update PROGRESS.md (Current State, Completed, Next Steps). Then loop.

Never: start a second feature while one is `active`; mark `passing` from memory; refactor "while at it"; leave a
failing `make check` for "next time"; silence a test to get green; pipe a gate command (`make check | tail`
hides the exit code — redirect to a log and test `$?`, or use `set -euo pipefail`). `features.py pass` refuses
unless `.harness/check.ok` (touched by a green `make check`) is newer than every source file.

## Definition of done (per feature)
- Verification command executed in this session and green; L1–L3 green.
- No debug leftovers; no new `TODO` without a feature id (`TODO(F6.2): …`).
- Evidence recorded in docs/features.md; PROGRESS.md updated; committed.

## Milestone gates — independent verifier (generator/evaluator separation)
The implementer never grades its own milestone. When the last feature of a milestone is `passing`:
1. Spawn a fresh-context verifier with the Agent tool (`general-purpose`) using the prompt template below.
2. It runs the gate commands from `docs/PLAN.md` exactly, reads nothing from the conversation, and reports
   `PASS`/`FAIL` with verbatim command output and any deviation between the feature *behaviors* and observed reality.
3. On `FAIL`: open a fix feature (or reopen the offending feature to `active` — the only allowed backward move,
   recorded in PROGRESS.md → Known Issues), fix, re-run the verifier.
4. On `PASS`: record `Gate M<n>: PASS <date> <commit>` in PROGRESS.md → Completed.

Verifier prompt template:
```
You are an independent verifier for milestone M<n> of the repository at <path>. Do not modify files.
Read docs/PLAN.md (M<n> row + section) and docs/features.md (M<n> entries). Run the gate commands exactly as
written, from the repository root. For each feature, judge whether the *behavior* text is actually observable
(run the verification command; inspect outputs). Report: PASS or FAIL, the commands you ran with their key
output lines, and a list of discrepancies (behavior claimed vs observed). Be skeptical; default to FAIL when unsure.
```

## Session boundaries
- Start: PROGRESS.md → `make features` → `make check` (fix first if red) → resume/pick.
- End: `make exit-check` → PROGRESS.md → commit. If mid-feature: leave the code compiling and tests green (stub
  or revert), state exactly where you stopped in PROGRESS.md → In Progress ("F2.2: expander handles PK/FK/unique;
  null ratios + decimal clamping pending — see tests/unit/test_expander.py::test_null_ratio (xfail)").

## Diagnostic loop when something fails repeatedly
Attribute the failure to a harness layer (task spec · context · environment · verification · state), fix that layer
(clarify the feature text, add a doc next to the code, fix the environment script, add a test, update PROGRESS.md),
then retry. "The model isn't good enough" is not a diagnosis.
