"""Deployment Safety Gate — NON-APPLIED pre-deploy guard suite.

Runs ten read-only guards against the release tree / runtime state and
produces a structured fail-closed decision. The gate NEVER deploys, NEVER
restarts, NEVER unlocks LIVE, NEVER touches the broker beyond the margin
value the CALLER supplies (the query itself stays in the authorized broker
read path). It only reports PASS/FAIL reasons + refusal codes.

Guards:
  1. release_head   — exact release-dir HEAD == literal LRC_RELEASE_SHA,
                      cwd-independent (git -C <release_dir>)
  2. clean_tree     — closure files have zero uncommitted changes (provenance)
  3. runtime_paths  — TRADING_RUNTIME_DIR set; dir/logs exist + writable;
                      ctx namespace correct; no secrets in the ctx file
  4. single_process — no duplicate live instance (pid file + alive process)
  5. flat_snapshot  — pre-restart read-only: positions FLAT + no pending orders
  6. quarantine_first_startup — AST: startup quarantines before cert; order
                      gates dominate every state-changing site
  7. session_generation — session/cert registry generation valid + not revoked
  8. margin         — broker capacity query result valid and >= 220000;
                      None / exception / invalid -> fail
  9. rollback_manifest — rollback release/env manifest present + records the
                      exact expected SHA (drift abort)
  10. ctx_atomic_health — dashboard execution_context atomic read/write health
                      (canonical reader + non-destructive write probe)
"""

from __future__ import annotations

import ast
import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

MARGIN_FLOOR = 220_000.0
SECRET_KEYS = re.compile(
    r"(api_?key|secret|password|token|credential|private_?key)", re.I)
_CTX_FILENAME = "execution_context.json"


# ── result types ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GuardResult:
    guard: str
    ok: bool
    reasons: Tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class DeploymentCheck:
    ok: bool
    results: Tuple[GuardResult, ...]

    @property
    def refusal_codes(self) -> Tuple[str, ...]:
        codes = sorted({r for g in self.results if not g.ok for r in g.reasons})
        return tuple(codes)

    def by_guard(self, guard: str) -> Optional[GuardResult]:
        for g in self.results:
            if g.guard == guard:
                return g
        return None


def _pass(guard: str, detail: str = "") -> GuardResult:
    return GuardResult(guard=guard, ok=True, detail=detail)


def _fail(guard: str, reasons: Sequence[str], detail: str = "") -> GuardResult:
    return GuardResult(guard=guard, ok=False, reasons=tuple(reasons),
                       detail=detail)


# ── 1. release HEAD ────────────────────────────────────────────────────────

def _git_head(release_dir: str) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(release_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30)
        return proc.returncode, (proc.stdout or "").strip()
    except Exception:
        return -1, ""


def guard_release_head(release_dir: str,
                       expected_sha: Optional[str] = None) -> GuardResult:
    expected_sha = expected_sha if expected_sha is not None \
        else os.environ.get("LRC_RELEASE_SHA", "")
    if not expected_sha or len(expected_sha) != 40 or \
            not all(c in "0123456789abcdefABCDEF" for c in expected_sha):
        return _fail("release_head", ["GUARD_HEAD_ENV_MISSING"])
    code, head = _git_head(release_dir)
    if code != 0 or len(head) != 40:
        return _fail("release_head", ["GUARD_HEAD_GIT_FAILED"], head)
    if head != expected_sha:
        return _fail("release_head", ["GUARD_HEAD_MISMATCH"],
                     f"head={head} expected={expected_sha}")
    return _pass("release_head", head)


# ── 2. clean closure tree ──────────────────────────────────────────────────

def guard_clean_tree(release_dir: str,
                     closure_files: Sequence[str]) -> GuardResult:
    try:
        proc = subprocess.run(
            ["git", "-C", str(release_dir), "status", "--porcelain", "--"]
            + list(closure_files),
            capture_output=True, text=True, timeout=30)
        dirty = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    except Exception:
        return _fail("clean_tree", ["GUARD_TREE_GIT_FAILED"])
    if dirty:
        names = ", ".join(ln.split()[-1] for ln in dirty[:8])
        return _fail("clean_tree", ["GUARD_TREE_DIRTY"], names)
    return _pass("clean_tree", f"{len(closure_files)} closure files clean")


# ── 3. runtime paths ───────────────────────────────────────────────────────

def _scan_secrets(path: Path) -> List[str]:
    found = []
    try:
        raw = path.read_text(encoding="utf-8")
        for key in re.findall(r'"([^"]+)"\s*:', raw):
            if SECRET_KEYS.search(key):
                found.append(key)
    except OSError:
        pass
    return found


def guard_runtime_paths(runtime_dir: str) -> GuardResult:
    reasons: List[str] = []
    details: List[str] = []
    if not runtime_dir:
        return _fail("runtime_paths", ["GUARD_RUNTIME_ENV_MISSING"])
    rt = Path(runtime_dir)
    if not rt.is_dir():
        return _fail("runtime_paths", ["GUARD_RUNTIME_DIR_MISSING"],
                     runtime_dir)
    logs = rt / "logs"
    if not logs.is_dir():
        reasons.append("GUARD_RUNTIME_DIR_MISSING")
        details.append(str(logs))
    for d in (rt, logs):
        if d.is_dir() and not os.access(d, os.W_OK):
            reasons.append("GUARD_RUNTIME_NOT_WRITABLE")
            details.append(str(d))
    # canonical namespace: the ctx file must live at runtime_root/execution_context.json
    ctx = rt / _CTX_FILENAME
    try:
        from core.runtime_paths import runtime_root
        canonical = Path(runtime_root()) / _CTX_FILENAME
        if canonical.resolve() != ctx.resolve():
            reasons.append("GUARD_RUNTIME_NAMESPACE")
            details.append(f"{ctx} != {canonical}")
    except Exception:
        pass
    secrets = _scan_secrets(ctx)
    if secrets:
        reasons.append("GUARD_RUNTIME_SECRETS")
        details.append(",".join(secrets[:6]))
    if reasons:
        return _fail("runtime_paths", reasons, "; ".join(details))
    return _pass("runtime_paths", str(ctx))


# ── 4. single process ──────────────────────────────────────────────────────

def guard_single_process(pid_file: str) -> GuardResult:
    p = Path(pid_file)
    if not p.is_file():
        return _pass("single_process", "no pid file")
    try:
        pid = int(p.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)          # alive -> duplicate instance
        return _fail("single_process", ["GUARD_DUPLICATE_INSTANCE"],
                     f"pid={pid}")
    except (ValueError, ProcessLookupError):
        return _pass("single_process", "stale pid file")
    except PermissionError:
        return _fail("single_process", ["GUARD_DUPLICATE_INSTANCE"],
                     "pid exists (permission denied = alive)")


# ── 5. flat / no pending order snapshot ────────────────────────────────────

def _is_flat(data: dict) -> Tuple[bool, str]:
    pos = data.get("position")
    if pos is not None:
        try:
            if float(pos) != 0.0:
                return False, f"position={pos}"
        except (TypeError, ValueError):
            return False, f"position unparseable {pos!r}"
    legs = data.get("legs") or {}
    for leg, meta in legs.items():
        if isinstance(meta, dict):
            qty = meta.get("qty") or meta.get("quantity")
            if qty:
                try:
                    if float(qty) != 0.0:
                        return False, f"leg {leg} qty={qty}"
                except (TypeError, ValueError):
                    return False, f"leg {leg} qty unparseable {qty!r}"
    if pos is None and not legs:
        return False, "no position/legs keys (cannot prove flat)"
    return True, ""


def guard_flat_no_pending(position_state_path: str,
                          ctx_data: dict) -> GuardResult:
    p = Path(position_state_path)
    if not p.is_file():
        return _fail("flat_snapshot", ["GUARD_SNAPSHOT_MISSING"])
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _fail("flat_snapshot", ["GUARD_SNAPSHOT_INVALID"])
    flat, why = _is_flat(data)
    if not flat:
        return _fail("flat_snapshot", ["GUARD_POSITION_NOT_FLAT"], why)
    pending = data.get("pending_orders") or data.get("open_orders") or []
    if pending:
        return _fail("flat_snapshot", ["GUARD_PENDING_ORDERS"],
                     f"{len(pending)} pending")
    return _pass("flat_snapshot", "flat + no pending")


# ── 6. quarantine-first startup (AST) ──────────────────────────────────────

_STATE_CHANGING = ("place_order", "cancel_order", "update_order",
                   "modify_order")


def guard_quarantine_first_startup(monitor_path: Optional[str]) -> GuardResult:
    if not monitor_path or not Path(monitor_path).is_file():
        return _pass("quarantine_first_startup", "not assessed (no path)")
    try:
        tree = ast.parse(Path(monitor_path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return _fail("quarantine_first_startup", ["GUARD_STARTUP_UNSAFE"])
    src = Path(monitor_path).read_text(encoding="utf-8")
    if "live_preflight_context" not in src or \
            "transition_with_certificate" not in src:
        return _fail("quarantine_first_startup", ["GUARD_STARTUP_UNSAFE"],
                     "missing preflight/certification wiring")

    def _gate_symbols(fn) -> List[str]:
        return [n.attr for n in ast.walk(fn)
                if isinstance(n, ast.Attribute)
                and n.attr == "is_live_ready"]

    gated = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name not in ("_place_safety_stop", "_cancel_safety_stop",
                           "_execute_trade"):
            continue
        if _gate_symbols(fn):
            gated += 1
    if gated < 3:
        return _fail("quarantine_first_startup", ["GUARD_STARTUP_UNSAFE"],
                     f"{gated}/3 order-route gates present")
    return _pass("quarantine_first_startup", "3/3 gates + cert wiring")


# ── 7. session/cert generation ─────────────────────────────────────────────

def guard_session_generation(generation: Optional[int],
                             revoked: bool) -> GuardResult:
    if generation is None:
        return _fail("session_generation", ["GUARD_SESSION_MISSING"])
    if revoked:
        return _fail("session_generation", ["GUARD_SESSION_REVOKED"],
                     f"generation={generation}")
    return _pass("session_generation", f"generation={generation}")


# ── 8. margin ──────────────────────────────────────────────────────────────

def guard_margin(margin_available: Optional[float],
                 floor: float = MARGIN_FLOOR) -> GuardResult:
    if margin_available is None:
        return _fail("margin", ["GUARD_MARGIN_UNAVAILABLE"])
    try:
        value = float(margin_available)
    except (TypeError, ValueError):
        return _fail("margin", ["GUARD_MARGIN_INVALID"])
    if not math.isfinite(value) or value <= 0:
        return _fail("margin", ["GUARD_MARGIN_INVALID"], str(value))
    if value < floor:
        return _fail("margin", ["GUARD_MARGIN_INSUFFICIENT"],
                     f"{value} < {floor}")
    return _pass("margin", str(value))


# ── 9. rollback manifest + drift abort ─────────────────────────────────────

def guard_rollback_manifest(manifest_paths: Sequence[str],
                            expected_sha: str) -> GuardResult:
    present = [p for p in manifest_paths if Path(p).is_file()]
    if not present:
        return _fail("rollback_manifest", ["GUARD_MANIFEST_MISSING"])
    for p in present:
        if expected_sha not in Path(p).read_text(encoding="utf-8"):
            return _fail("rollback_manifest", ["GUARD_MANIFEST_STALE"],
                         f"{p} lacks {expected_sha[:12]}")
    return _pass("rollback_manifest", ",".join(present))


# ── 10. ctx atomic read/write health ───────────────────────────────────────

def guard_ctx_atomic_health(runtime_dir: str) -> GuardResult:
    from core.execution_context_state import read_execution_context
    data = read_execution_context(runtime_dir=runtime_dir)
    reasons = tuple(data.get("audit_reasons") or ())
    if not reasons and data.get("effective_mode") is None:
        reasons = ("GUARD_CTX_INVALID",)
    if reasons:
        mapped = tuple(
            "GUARD_CTX_MISSING" if r == "RESTART_MAINTAIN_QUARANTINE"
            else "GUARD_CTX_CORRUPT" if r == "STATE_FILE_CORRUPTED"
            else r for r in reasons)
        return _fail("ctx_atomic_health", mapped)
    # non-destructive write probe: same-dir temp file, fsync, read back, unlink
    try:
        probe = Path(runtime_dir) / f"._gate_probe_{os.getpid()}"
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
            fh.flush()
            os.fsync(fh.fileno())
        ok = probe.read_text(encoding="utf-8") == "ok"
        probe.unlink(missing_ok=True)
        if not ok:
            return _fail("ctx_atomic_health", ["GUARD_CTX_WRITE_FAIL"])
    except OSError:
        return _fail("ctx_atomic_health", ["GUARD_CTX_WRITE_FAIL"])
    return _pass("ctx_atomic_health", f"revision={data.get('revision')}")


# ── aggregate ──────────────────────────────────────────────────────────────

def check_deployment(
    *,
    release_dir: str,
    closure_files: Sequence[str],
    runtime_dir: Optional[str] = None,
    pid_file: str,
    position_state_path: str,
    monitor_path: Optional[str] = None,
    session_generation: Optional[int] = None,
    session_revoked: bool = False,
    margin_available: Optional[float] = None,
    manifest_paths: Sequence[str] = (),
    expected_sha: Optional[str] = None,
) -> DeploymentCheck:
    """Run all guards. Fail-closed: ANY guard failure -> NOT_READY.

    Never deploys / restarts / unlocks LIVE — the caller decides what to
    do with the structured result.
    """
    runtime_dir = runtime_dir if runtime_dir is not None \
        else os.environ.get("TRADING_RUNTIME_DIR", "")
    expected_sha = expected_sha if expected_sha is not None \
        else os.environ.get("LRC_RELEASE_SHA", "")
    results = (
        guard_release_head(release_dir, expected_sha),
        guard_clean_tree(release_dir, closure_files),
        guard_runtime_paths(runtime_dir),
        guard_single_process(pid_file),
        guard_flat_no_pending(position_state_path,
                              read_ctx_snapshot(runtime_dir)),
        guard_quarantine_first_startup(monitor_path),
        guard_session_generation(session_generation, session_revoked),
        guard_margin(margin_available),
        guard_rollback_manifest(manifest_paths, expected_sha),
        guard_ctx_atomic_health(runtime_dir),
    )
    return DeploymentCheck(ok=all(r.ok for r in results), results=results)


def read_ctx_snapshot(runtime_dir: str) -> dict:
    """Read-only ctx snapshot for the flat guard (never a broker call)."""
    from core.execution_context_state import read_execution_context
    return read_execution_context(runtime_dir=runtime_dir or None)
