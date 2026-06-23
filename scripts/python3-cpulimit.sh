#!/bin/bash
# 2026-06-23 Gemini CLI: Wrapper script to run python3 under run-cpulimit.py (50% CPU limit)
UNIFIED_DIR="/Users/mylin/Documents/mylin102/tw-trading-unified"
exec "$UNIFIED_DIR/scripts/run-cpulimit.py" python3 "$@"
