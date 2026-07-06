#!/bin/bash
# 2026-06-30 Gemini CLI: Wrapper script to run python3 under macOS taskpolicy background (forces E-cores)
UNIFIED_DIR="/Users/mylin/Documents/mylin102/tw-trading-unified"
exec taskpolicy -c background python3 "$@"
