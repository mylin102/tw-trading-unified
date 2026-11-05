#!/usr/bin/env bash
#
# pytest-quick.sh — Fast local test runner
# ==========================================
#
# Usage:
#   ./scripts/pytest-quick.sh                          # run all tests (no coverage)
#   ./scripts/pytest-quick.sh -k "squeeze"             # keyword filter
#   ./scripts/pytest-quick.sh tests/strategies/        # specific directory
#   ./scripts/pytest-quick.sh -x -k "orb" --tb=long    # pass any pytest args
#
# Run with coverage (slower, for before-push verification):
#   pytest --cov=.
#
# Environment:
#   MAX_FAILURES  — stop after N failures (e.g. MAX_FAILURES=5; implies -x is removed)
#   NO_X=1        — disable the default -x (stop-first-failure) behaviour
#   WORKERS       — parallel workers for xdist (default: auto)
#
set -euo pipefail

cd "$(git rev-parse --show-toplevel 2>/dev/null || echo "$(dirname "$0")/..")"

# 2026-11-05 Hermes Agent: always use the project interpreter. Running the
# system/`python` (which loads ~/Library/Python/3.12 site-packages) makes a
# third-party top-level `tests` package shadow this repo's tests/ and pulls in
# an incompatible scipy, so collection and results differ by interpreter.
# PYTEST_PYTHON overrides; otherwise venv/ then .venv/ then python3.
if [[ -n "${PYTEST_PYTHON:-}" ]]; then
    PY_BIN="$PYTEST_PYTHON"
elif [[ -x venv/bin/python ]]; then
    PY_BIN="venv/bin/python"
elif [[ -x .venv/bin/python ]]; then
    PY_BIN=".venv/bin/python"
else
    PY_BIN="python3"
fi
echo "interpreter: $PY_BIN"

echo "=== 🚀 pytest-quick: fast feedback loop (no coverage) ==="
echo ""

# Assemble pytest args: fast defaults then user overrides
PYTEST_ARGS=(
    -v                     # verbose
    --tb=short             # concise tracebacks
    --no-header            # less noise
    -W ignore::DeprecationWarning
    -W ignore::UserWarning
)

# Stop on first failure, unless MAX_FAILURES is set
if [[ -n "${MAX_FAILURES:-}" ]]; then
    PYTEST_ARGS+=(--maxfail="$MAX_FAILURES")
elif [[ "${NO_X:-}" != "1" ]]; then
    PYTEST_ARGS+=(-x)
fi

# xdist parallel if available
if "$PY_BIN" -m pytest --help 2>/dev/null | grep -q -- --numprocesses; then
    PYTEST_ARGS+=(-n "${WORKERS:-auto}")
fi

# Append any user-provided arguments (override defaults)
PYTEST_ARGS+=("$@")

set -x
"$PY_BIN" -m pytest "${PYTEST_ARGS[@]}"
