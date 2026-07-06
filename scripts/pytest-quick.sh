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
if python -m pytest --help 2>/dev/null | grep -q -- --numprocesses; then
    PYTEST_ARGS+=(-n "${WORKERS:-auto}")
fi

# Append any user-provided arguments (override defaults)
PYTEST_ARGS+=("$@")

set -x
python -m pytest "${PYTEST_ARGS[@]}"
