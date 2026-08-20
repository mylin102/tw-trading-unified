"""Single authority for the persistent runtime data root.

Both the trading engine and the dashboard resolve runtime data (logs, exports,
data) through this module so a deploy-directory change can never split the
ledgers again (2026-08-05 incident: dashboard read <repo>/logs while the
engine wrote <runtime>/logs).

Resolution order:
  1. $TRADING_RUNTIME_DIR  (set by PM2 ecosystem.config.js env for both apps)
  2. repo root fallback    (dev/backtest on Air4: identical to old behaviour)
"""
import os
import subprocess
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CANONICAL_RELEASE_ROOT = "/Users/myllin_mini/Documents/mylin102/tw-trading-unified-release15"


def enforce_runtime_identity(source_file: Optional[str] = None) -> None:
    """Fail closed when production code is launched from a temp worktree."""
    source_dir = (os.path.dirname(os.path.abspath(source_file))
                  if source_file else _REPO_ROOT)
    try:
        repo_root = os.path.realpath(subprocess.check_output(
            ["git", "-C", source_dir, "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL).strip())
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "RUNTIME_IDENTITY_REJECTED: cannot resolve repository root"
        ) from exc
    forbidden = ("/tmp", "/private/tmp")
    if any(repo_root == root or repo_root.startswith(root + os.sep)
           for root in forbidden):
        raise RuntimeError(
            f"RUNTIME_IDENTITY_REJECTED: source tree is temporary: {repo_root}")

    if os.environ.get("NODE_ENV") != "production":
        return

    expected_root = os.path.realpath(
        os.environ.get("TRADING_CANONICAL_ROOT", _CANONICAL_RELEASE_ROOT))
    cwd = os.path.realpath(os.getcwd())
    if repo_root != expected_root or cwd != expected_root:
        raise RuntimeError(
            "RUNTIME_IDENTITY_REJECTED: "
            f"repo_root={repo_root} cwd={cwd} expected={expected_root}")

    expected_sha = os.environ.get("LRC_RELEASE_SHA")
    if expected_sha:
        try:
            actual_sha = subprocess.check_output(
                ["git", "-C", repo_root, "rev-parse", "HEAD"],
                text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                "RUNTIME_IDENTITY_REJECTED: cannot resolve release SHA") from exc
        if actual_sha != expected_sha:
            raise RuntimeError(
                "RUNTIME_IDENTITY_REJECTED: "
                f"actual_sha={actual_sha} expected_sha={expected_sha}")


def runtime_root() -> str:
    return os.environ.get("TRADING_RUNTIME_DIR") or _REPO_ROOT


def runtime_path(*parts) -> str:
    return os.path.join(runtime_root(), *parts)


def runtime_logs(*parts) -> str:
    """Runtime log namespace: <runtime_root>/logs[/...].

    Both the trading engine and the dashboard resolve ALL log writes
    (market_data CSVs, backups, stocks, anomalous quotes, MTS events)
    through this — never into the release source tree. Production
    always sets TRADING_RUNTIME_DIR (PM2 env); dev/paper falls back to
    <repo>/logs explicitly."""
    return os.path.join(runtime_root(), "logs", *parts)
