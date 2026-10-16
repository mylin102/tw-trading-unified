#!/bin/bash
# Install the agent-provenance commit-msg hook into this repo's .git/hooks/.
# Run from the repo root:  bash scripts/install_commit_msg_hook.sh
set -e
HOOK_SRC="$(cd "$(dirname "$0")/.." && pwd)/scripts/commit-msg.hook"
GIT_DIR="$(git rev-parse --git-dir)"
install -m 755 "$HOOK_SRC" "$GIT_DIR/hooks/commit-msg"
echo "installed: $GIT_DIR/hooks/commit-msg"
echo "next: export AGENT_NAME=hermes|codex|gemini|antigravity|human before every commit"
