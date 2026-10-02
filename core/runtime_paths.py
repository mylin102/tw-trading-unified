"""Single authority for the persistent runtime data root.

Both the trading engine and the dashboard resolve runtime data (logs, exports,
data) through this module so a deploy-directory change can never split the
ledgers again (2026-08-05 incident: dashboard read <repo>/logs while the
engine wrote <runtime>/logs).

Resolution order:
  1. $TRADING_RUNTIME_DIR  (set by PM2 ecosystem.config.js env for both apps)
  2. repo root fallback    (dev/backtest on Air4: identical to old behaviour)
"""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CANONICAL_RELEASE_ROOT = "/Users/myllin_mini/Documents/mylin102/tw-trading-unified-release15"


def check_runtime_identity(
    source_file: Optional[str] = None,
) -> tuple[bool, str, str, str]:
    """Return dashboard-safe identity evidence without weakening enforcement.

    Production trading code continues to call ``enforce_runtime_identity``;
    this non-raising view exists only so the dashboard can render a stale
    deployment warning instead of exposing release-dependent values.
    """
    source_dir = (os.path.dirname(os.path.abspath(source_file))
                  if source_file else _REPO_ROOT)
    try:
        repo_root = os.path.realpath(subprocess.check_output(
            ["git", "-C", source_dir, "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL).strip())
    except (OSError, subprocess.CalledProcessError):
        message = "RUNTIME_IDENTITY_REJECTED: cannot resolve repository root"
        return False, "<unresolved>", "<repository>", message
    forbidden = ("/tmp", "/private/tmp")
    if any(repo_root == root or repo_root.startswith(root + os.sep)
           for root in forbidden):
        message = f"RUNTIME_IDENTITY_REJECTED: source tree is temporary: {repo_root}"
        return False, repo_root, "<non-temporary repository>", message
    if os.environ.get("NODE_ENV") != "production":
        return True, repo_root, repo_root, ""

    expected_root = os.path.realpath(
        os.environ.get("TRADING_CANONICAL_ROOT", _CANONICAL_RELEASE_ROOT))
    cwd = os.path.realpath(os.getcwd())
    if repo_root != expected_root or cwd != expected_root:
        message = ("RUNTIME_IDENTITY_REJECTED: "
                   f"repo_root={repo_root} cwd={cwd} expected={expected_root}")
        return False, f"repo_root={repo_root};cwd={cwd}", expected_root, message

    expected_sha = os.environ.get("LRC_RELEASE_SHA")
    if not expected_sha:
        return True, "<unpinned>", "<unpinned>", ""
    try:
        actual_sha = subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        message = "RUNTIME_IDENTITY_REJECTED: cannot resolve release SHA"
        return False, "<unresolved>", expected_sha, message
    if actual_sha != expected_sha:
        message = ("RUNTIME_IDENTITY_REJECTED: "
                   f"actual_sha={actual_sha} expected_sha={expected_sha}")
        return False, actual_sha, expected_sha, message
    return True, actual_sha, expected_sha, ""


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


RUNTIME_STATE_SCHEMA = 1


def runtime_root() -> str:
    """Return the one absolute runtime data root.

    Production must provide TRADING_RUNTIME_DIR.  Development/tests may use
    the repository root, but never silently fall back to /tmp.
    """
    raw = os.environ.get("TRADING_RUNTIME_DIR")
    if not raw:
        if os.environ.get("NODE_ENV") == "production":
            raise RuntimeError("RUNTIME_ROOT_UNSET: TRADING_RUNTIME_DIR is required")
        raw = _REPO_ROOT
    return str(Path(raw).expanduser().resolve())


def runtime_path(*parts) -> str:
    return os.path.join(runtime_root(), *parts)


# 2026-10-02 Codex: bind the engine before restore; dashboard reads its published context.
_MTS_DATA_MODE: Optional[str] = None


def bind_mts_data_mode(mode: str) -> None:
    """Bind production MTS data to the startup profile, not a hot-reloaded flag."""
    global _MTS_DATA_MODE
    if mode not in ("live", "paper"):
        raise ValueError(f"Invalid MTS data mode: {mode}")
    if os.environ.get("NODE_ENV") == "production":
        _MTS_DATA_MODE = mode


def mts_data_mode() -> Optional[str]:
    """Preserve offline explicit path injection; production UI follows runtime truth."""
    if os.environ.get("NODE_ENV") != "production":
        return None
    if _MTS_DATA_MODE is not None:
        return _MTS_DATA_MODE
    try:
        with open(runtime_path("execution_context.json"), encoding="utf-8") as f:
            mode = json.load(f).get("requested_mode")
        return mode if mode in ("live", "paper") else None
    except (OSError, ValueError, AttributeError):
        return None


def mts_runtime_path(*parts) -> str:
    """MTS-only namespace; LIVE history stays in place, PAPER never falls back to it."""
    prefix = ("paper",) if mts_data_mode() == "paper" else ()
    return runtime_path(*prefix, *parts)


def mts_position_state_path() -> str:
    """Canonical MTS state path; production has no implicit override/fallback."""
    explicit = os.environ.get("MTS_STATE_PATH")
    if explicit:
        if os.environ.get("NODE_ENV") == "production":
            expected = os.path.realpath(runtime_path("data", "state", "mts_position_state.json"))
            actual = str(Path(explicit).expanduser().resolve())
            if actual != expected:
                raise RuntimeError(
                    f"RUNTIME_STATE_PATH_REJECTED: expected={expected} actual={actual}")
        if os.environ.get("NODE_ENV") != "production":
            return str(Path(explicit).expanduser().resolve())
    # 2026-10-02 Codex: PM2's canonical override identifies the file, not its mode.
    return mts_runtime_path("data", "state", "mts_position_state.json")


def runtime_id() -> str:
    """Stable identity for the resolved runtime root, optionally deployment-pinned."""
    explicit = os.environ.get("TRADING_RUNTIME_ID")
    if explicit:
        return explicit
    return "runtime-" + hashlib.sha256(runtime_root().encode("utf-8")).hexdigest()[:16]


def stamp_runtime_state(state: dict, existing: dict | None = None,
                        *, writer_identity: str = "mts_spread") -> dict:
    """Stamp state with identity metadata without changing lifecycle fields."""
    previous = existing or {}
    try:
        generation = int(previous.get("generation", 0)) + 1
    except (TypeError, ValueError):
        generation = 1
    state["runtime_id"] = runtime_id()
    state["runtime_root"] = runtime_root()
    state["writer_identity"] = f"{writer_identity}:{os.getpid()}"
    state["generation"] = generation
    state["runtime_state_schema"] = RUNTIME_STATE_SCHEMA
    return state


def validate_runtime_state(state: object) -> tuple[bool, str]:
    """Validate canonical state identity; never accept unknown identity."""
    if not isinstance(state, dict):
        return False, "STATE_UNAVAILABLE"
    if state.get("runtime_id") != runtime_id():
        return False, "IDENTITY_MISMATCH"
    if str(Path(str(state.get("runtime_root", ""))).expanduser().resolve()) != runtime_root():
        return False, "RUNTIME_STATE_SPLIT_BRAIN"
    if state.get("runtime_state_schema") != RUNTIME_STATE_SCHEMA:
        return False, "SCHEMA_MISMATCH"
    if not isinstance(state.get("writer_identity"), str) or not state.get("writer_identity"):
        return False, "IDENTITY_MISMATCH"
    if not isinstance(state.get("generation"), int) or isinstance(state.get("generation"), bool):
        return False, "SCHEMA_MISMATCH"
    return True, "OK"


def runtime_logs(*parts) -> str:
    """Runtime log namespace: <runtime_root>/logs[/...].

    Both the trading engine and the dashboard resolve ALL log writes
    (market_data CSVs, backups, stocks, anomalous quotes, MTS events)
    through this — never into the release source tree. Production
    always sets TRADING_RUNTIME_DIR (PM2 env); dev/paper falls back to
    <repo>/logs explicitly."""
    return os.path.join(runtime_root(), "logs", *parts)
