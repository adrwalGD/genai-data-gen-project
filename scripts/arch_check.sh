#!/usr/bin/env bash
# Executable architecture rules (docs/architecture.md, CLAUDE.md rules 1/2/7). Every violation prints
# what / why / fix so the reader can repair it without re-deriving the rule. Exit 1 on any violation.
set -uo pipefail
cd "$(dirname "$0")/.."
SRC=src/genai_data_gen_project
fail=0

violation() { # title, hits, why, fix
  printf 'ARCH VIOLATION: %s\n%s\n  why: %s\n  fix: %s\n\n' "$1" "$2" "$3" "$4"; fail=1
}

# R1 — the Gemini client is constructed only in llm/client.py (single traced, retried, configured entry point)
hits=$(grep -rn --include='*.py' -E 'genai\.Client\(' "$SRC" | grep -v "^$SRC/llm/client.py" || true)
[ -n "$hits" ] && violation "genai.Client() outside llm/client.py" "$hits" \
  "all Gemini traffic must share retries, thinking config, model from Settings and Langfuse instrumentation" \
  "import and use llm.client.GeminiClient (or the LLMBackend protocol) instead"

# R2 — the UI layer never touches infrastructure libraries
hits=$(grep -rn --include='*.py' -E '^\s*(from|import)\s+(google(\.genai)?\b|psycopg|sqlglot|langfuse|openinference)' "$SRC/ui" || true)
[ -n "$hits" ] && violation "infrastructure import inside ui/" "$hits" \
  "ui → chat|generation → storage|schema|llm; pages render state and call services only" \
  "move the logic into generation/, chat/ or storage/ and call it from the page"

# R3 — model ids live only in config.py
hits=$(grep -rn --include='*.py' -E "[\"']gemini-[0-9]" "$SRC" | grep -v "^$SRC/config.py" || true)
[ -n "$hits" ] && violation "hardcoded gemini model id" "$hits" \
  "models get retired (gemini-2.0-flash-001 → 404); the id must be changeable via GEMINI_MODEL" \
  "use Settings.gemini_model"

# R4 — no print() in the package (logging or Streamlit widgets)
hits=$(grep -rn --include='*.py' -E '^\s*print\(' "$SRC" || true)
[ -n "$hits" ] && violation "print() in src/" "$hits" \
  "prints are invisible in Streamlit and untraceable in Langfuse/logs" \
  "use logging.getLogger(__name__) or st.* output"

# R5 — psycopg (raw database access) only in storage/
hits=$(grep -rln --include='*.py' -E '^\s*(from|import)\s+psycopg' "$SRC" | grep -v "^$SRC/storage/" || true)
[ -n "$hits" ] && violation "psycopg imported outside storage/" "$hits" \
  "every query must pass storage.sql_guard / the loader; scattering DB access defeats the read-only guarantee" \
  "add a function to storage/postgres.py and call it"

# R6 — infrastructure modules never import service/UI layers (dependencies flow downward only)
hits=$(grep -rn --include='*.py' -E '(from|import)\s+genai_data_gen_project\.(generation|chat|ui)\b|from \.\.(generation|chat|ui)\b' \
       "$SRC/llm" "$SRC/schema" "$SRC/storage" "$SRC/config.py" "$SRC/observability.py" || true)
[ -n "$hits" ] && violation "upward import from infrastructure layer" "$hits" \
  "llm/, schema/, storage/, config, observability must stay reusable and testable in isolation" \
  "invert the dependency: pass data/callables down, or move the code up a layer"

# R7 — nothing imports ui except ui itself and the CLI launcher
hits=$(grep -rn --include='*.py' -E '(from|import)\s+genai_data_gen_project\.ui\b' "$SRC" | grep -vE "^$SRC/(ui/|cli\.py)" || true)
[ -n "$hits" ] && violation "ui imported from a non-UI module" "$hits" \
  "the UI is the top layer; importing it elsewhere creates cycles and drags Streamlit into tests" \
  "move shared code to a lower layer"

if [ "$fail" -eq 0 ]; then echo "arch-check: OK (7 rules)"; fi
exit "$fail"
