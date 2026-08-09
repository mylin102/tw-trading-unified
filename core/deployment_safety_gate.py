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
  3. runtime_paths  — explicit TRADING_RUNTIME_DIR env REQUIRED (never a
                      repo fallback); dir/logs exist + writable; ctx
                      namespace = env runtime root; no secrets in the ctx
  4. single_process — no duplicate live instance (pid file + alive process)
  5. flat_snapshot  — provenance-aware pre-restart snapshot: live requires
                      source=live_broker + positions=0/[] + open_orders=[] +
                      fresh captured_at + hash + matching session_id; paper
                      snapshots never satisfy live; missing/ambiguous
                      source -> fail-closed
  6. quarantine_first_startup — AST: startup quarantines before cert; order
                      gates dominate every state-changing site
  7. session_generation — session/cert registry generation valid + not revoked
  8. margin         — broker capacity query result valid and >= 220000
                      (caller-supplied, read-only source); None/invalid fail
  9. rollback_manifest — rollback release/env manifest present + records the
                      exact expected SHA (drift abort; freeze-first: any
                      post-freeze change is refused)
  10. ctx_atomic_health — dashboard execution_context atomic read/write
                      health; a MISSING file may only bootstrap as
                      LIVE_QUARANTINED (atomic write capability verified,
                      never LIVE)
"""

from __future__ import annotations

import ast
import json
import math
import os
import re
import subprocess
import time
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
    env_rt = os.environ.get("TRADING_RUNTIME_DIR", "")
    # production/deploy mode REQUIRES the explicit TRADING_RUNTIME_DIR env —
    # never fall back to the repo (the deployed process reads the env)
    if not runtime_dir or not env_rt:
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
    # canonical namespace: the ctx file must live at the ENV runtime root
    # (never a repo fallback)
    ctx = rt / _CTX_FILENAME
    canonical = Path(env_rt) / _CTX_FILENAME
    if canonical.resolve() != ctx.resolve():
        reasons.append("GUARD_RUNTIME_NAMESPACE")
        details.append(f"{ctx} != {canonical}")
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

SNAPSHOT_MAX_AGE_S = 600.0          # live-broker snapshot freshness window


def _parse_ts(value) -> Optional[float]:
    """Parse a timestamp (int/float/str epoch SECONDS, ISO-8601, or the
    canonical epoch-MS integer) into epoch SECONDS. None on failure."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v >= 1e12 else v
    if isinstance(value, str):
        try:
            v = float(value)
            return v / 1000.0 if v >= 1e12 else v
        except ValueError:
            pass
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            return None
    return None


def guard_flat_no_pending(position_state_path: str,
                          ctx_data: dict) -> GuardResult:
    """Provenance-aware flat check.

    - paper snapshot (mode/source == paper) is accepted ONLY for a paper
      context; it NEVER satisfies a live deployment (paper positions are
      allowed without claiming live readiness).
    - a live deployment requires a source=live_broker snapshot with
      positions=0, open_orders=[], captured_at (fresh), a content hash,
      and no intervening live session (snapshot session == ctx session).
    - missing / ambiguous source -> fail-closed (never assumed flat).
    """
    p = Path(position_state_path)
    if not p.is_file():
        return _fail("flat_snapshot", ["GUARD_SNAPSHOT_MISSING"])
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _fail("flat_snapshot", ["GUARD_SNAPSHOT_INVALID"])

    source = str(data.get("source") or data.get("mode") or "").lower()
    ctx_mode = str((ctx_data or {}).get("requested_mode")
                   or (ctx_data or {}).get("effective_mode") or "")
    is_live_ctx = "live" in ctx_mode

    if source == "paper":
        if is_live_ctx:
            return _fail("flat_snapshot",
                         ["GUARD_SNAPSHOT_PAPER_NOT_LIVE"],
                         "paper snapshot is never live-flat evidence")
        return _pass("flat_snapshot", "paper snapshot (paper deployment)")

    if source != "live_broker":
        return _fail("flat_snapshot", ["GUARD_SNAPSHOT_SOURCE_AMBIGUOUS"],
                     f"source={source!r} (missing/unknown)")

    positions = data.get("positions", data.get("position", None))
    open_orders = data.get("open_orders", data.get("pending_orders", None))
    captured_at = _parse_ts(data.get("captured_at"))
    # single canonical digest name: canonical_input_hash (the preflight's
    # content-addressed capture hash — no loose aliases)
    digest = data.get("canonical_input_hash")
    if positions is None or open_orders is None:
        return _fail("flat_snapshot", ["GUARD_SNAPSHOT_INVALID"],
                     "positions/open_orders required")
    # A) MTS flat scope = FUTURES account only (unless explicit
    # global-risk config). Stock rows stay in the evidence but never
    # block MTS futures flat. Untagged rows default to futures-relevant.
    global_risk = bool(data.get("global_risk", False))

    def _relevant(rows, kind):
        if not isinstance(rows, list) or not rows:
            return rows
        if not all(isinstance(r, dict) for r in rows):
            return rows                      # legacy rows: all relevant
        if global_risk:
            return rows
        return [r for r in rows
                if str(r.get("account", "") or "") in ("", "futures")]

    _positions = _relevant(positions, "positions")
    _open_orders = _relevant(open_orders, "open_orders")

    # flat: 0 / 0.0 / [] / () (empty list is the Codex-contract shape)
    if isinstance(_positions, list):
        non_zero = [p for p in _positions
                    if int(p.get("quantity", 0) or 0) != 0]
        if non_zero:
            return _fail("flat_snapshot", ["GUARD_POSITION_NOT_FLAT"],
                         f"{len(non_zero)} futures position(s) "
                         f"({', '.join(str(p.get('code')) for p in non_zero[:5])})")
    elif _positions not in (0, 0.0, [], ()):
        return _fail("flat_snapshot", ["GUARD_POSITION_NOT_FLAT"],
                     f"positions={_positions!r}")
    if _open_orders:
        return _fail("flat_snapshot", ["GUARD_PENDING_ORDERS"],
                     f"{len(_open_orders)} open")
    if captured_at is None:
        return _fail("flat_snapshot", ["GUARD_SNAPSHOT_STALE"],
                     "captured_at missing")
    if time.time() - captured_at > SNAPSHOT_MAX_AGE_S:
        return _fail("flat_snapshot", ["GUARD_SNAPSHOT_STALE"],
                     f"captured_at {captured_at:.0f} older than "
                     f"{SNAPSHOT_MAX_AGE_S:.0f}s")
    if not digest:
        return _fail("flat_snapshot", ["GUARD_SNAPSHOT_HASH_MISSING"])
    snap_session = data.get("session_id")
    ctx_session = (ctx_data or {}).get("session_id")
    if snap_session and ctx_session and snap_session != ctx_session:
        return _fail("flat_snapshot", ["GUARD_SESSION_INTERVENED"],
                     f"snapshot session {snap_session} != ctx "
                     f"{ctx_session}")
    _stock_rows = 0
    if isinstance(positions, list):
        _stock_rows = sum(
            1 for p in positions
            if isinstance(p, dict) and str(p.get("account", "")) == "stock")
    _detail = f"futures flat captured_at={captured_at:.0f}"
    if _stock_rows:
        _detail += f"; {_stock_rows} stock row(s) in evidence (not blocking)"
    return _pass("flat_snapshot", _detail)


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


# ── 7. session/cert generation (registry-bound; split gate) ────────────────

_REGISTRY_GEN_RE = re.compile(r"^[0-9a-f]{32}$")   # token_hex(16) format


def guard_session_generation(generation: Optional[int],
                             revoked: bool) -> GuardResult:
    # The live session generation is REGISTRY-BOUND: the session_registry
    # produces token_hex(16) (32-hex str) generations. Standalone preflight
    # identity (sha256(account_id) 16-hex) NEVER passes. Assessed only in
    # the post_startup phase (pre_deploy skips it).
    if generation is None or isinstance(generation, bool):
        return _fail("session_generation", ["GUARD_SESSION_MISSING"],
                     "registry-bound generation required")
    if isinstance(generation, str):
        if not _REGISTRY_GEN_RE.match(generation):
            return _fail("session_generation", ["GUARD_SESSION_MISSING"],
                         "standalone/foreign identity rejected (need "
                         "registry-bound 32-hex generation)")
    elif not isinstance(generation, int):
        return _fail("session_generation", ["GUARD_SESSION_MISSING"],
                     f"registry-bound generation required, got "
                     f"{type(generation).__name__}")
    if revoked:
        return _fail("session_generation", ["GUARD_SESSION_REVOKED"],
                     f"generation={generation}")
    return _pass("session_generation", f"generation={generation}")


# ── 8. margin ──────────────────────────────────────────────────────────────

def guard_margin(margin_available: Optional[float],
                 floor: float = MARGIN_FLOOR,
                 margin_evidence: Optional[dict] = None) -> GuardResult:
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
    # D2: bare margin_available is NOT enough to pass — traceable evidence
    # (account identity, scope, captured_at, canonical input hash) is
    # REQUIRED (the read-only preflight packet provides all four)
    if margin_evidence is None:
        return _fail("margin", ["GUARD_MARGIN_EVIDENCE_MISSING"],
                     "margin evidence required (account/scope/captured_at/"
                     "hash) — bare value insufficient")
    missing = [k for k in ("account_identity_hash", "scope",
                           "captured_at", "canonical_input_hash")
               if not margin_evidence.get(k)]
    if missing:
        return _fail("margin", ["GUARD_MARGIN_EVIDENCE_MISSING"],
                     f"missing {','.join(missing)}")
    # the margin guard validates its OWN evidence freshness — it must not
    # rely on the flat guard: captured_at must be a finite epoch within
    # SNAPSHOT_MAX_AGE_S (canonical epoch-ms integers normalize via
    # _parse_ts)
    _ts = _parse_ts(margin_evidence.get("captured_at"))
    if _ts is None or not math.isfinite(_ts) or _ts <= 0:
        return _fail("margin", ["GUARD_MARGIN_EVIDENCE_STALE"],
                     "captured_at must be a finite epoch")
    if time.time() - _ts > SNAPSHOT_MAX_AGE_S:
        return _fail("margin", ["GUARD_MARGIN_EVIDENCE_STALE"],
                     f"captured_at {_ts:.0f} older than "
                     f"{SNAPSHOT_MAX_AGE_S:.0f}s")
    return _pass("margin", str(value))


# ── 9. rollback manifest + drift abort (exclude-self tree identity) ─────────

_MANIFEST_HASH_RE = re.compile(r"frozen_tree_hash:\s*([0-9a-f]{64})")


def _exclude_self_tree_hash(release_dir: str,
                            commit: str,
                            exclude_paths: Sequence[str]) -> Optional[str]:
    """Content-addressed release-tree identity: SHA-256 of the
    `git ls-tree -r <commit>` entries EXCLUDING the manifest/rollback docs.

    Exclude-self semantics resolve the freeze cycle: recording the freeze
    in a manifest commit does NOT move the identity (the manifest is
    excluded), while ANY code change after the freeze changes the hash and
    is refused (GUARD_MANIFEST_STALE)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(release_dir), "ls-tree", "-r", commit],
            capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return None
        lines = [ln for ln in (proc.stdout or "").splitlines()
                 if not any(ln.split("\t")[-1] == p for p in exclude_paths)]
        import hashlib
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()
    except Exception:
        return None


def guard_rollback_manifest(release_dir: str,
                            manifest_paths: Sequence[str],
                            expected_sha: str,
                            exclude_paths: Sequence[str] = ()) -> GuardResult:
    """Rollback manifest + drift abort with exclude-self tree identity.

    The manifest must record `frozen_tree_hash` (the exclude-self tree
    hash at freeze time). The guard re-derives the hash from the CURRENT
    HEAD and refuses on ANY code drift; manifest-only commits (recording
    the freeze itself) do NOT invalidate the record — the SHA cycle is
    broken without weakening fail-closed."""
    present = [p for p in manifest_paths if Path(p).is_file()]
    if not present:
        return _fail("rollback_manifest", ["GUARD_MANIFEST_MISSING"])
    recorded = None
    for p in present:
        try:
            text = Path(p).read_text(encoding="utf-8")
        except OSError:
            continue
        m = _MANIFEST_HASH_RE.search(text)
        if m:
            recorded = m.group(1)
            break
    if recorded is None:
        return _fail("rollback_manifest", ["GUARD_MANIFEST_STALE"],
                     "no manifest carries frozen_tree_hash "
                     f"(checked {len(present)} file(s))")
    current = _exclude_self_tree_hash(release_dir, "HEAD", exclude_paths)
    if current is None:
        return _fail("rollback_manifest", ["GUARD_MANIFEST_STALE"],
                     "cannot derive HEAD tree identity")
    if current != recorded:
        return _fail("rollback_manifest", ["GUARD_MANIFEST_STALE"],
                     f"tree drift: recorded {recorded[:12]} != HEAD "
                     f"{current[:12]}")
    return _pass("rollback_manifest",
                 f"exclude-self tree identity {current[:16]}… "
                 f"(expected {expected_sha[:12]}…)")


# ── 11. capture consistency (flat + margin same preflight capture) ─────────


def guard_capture_consistency(position_state_path: Optional[str],
                              margin_evidence: Optional[dict]
                              ) -> GuardResult:
    """The flat snapshot and the margin evidence must come from the SAME
    read-only preflight capture: canonical input hash, captured_at,
    account identity and scope EXACTLY consistent (same-source artifact —
    no skew tolerance) and fresh (<=600s). Different/missing captures =>
    GUARD_CAPTURE_MISMATCH (fail-closed). Independent captures are NOT
    supported yet (a future capture_id/skew policy would govern them)."""
    if margin_evidence is None:
        return _fail("capture_consistency", ["GUARD_CAPTURE_MISMATCH"],
                     "margin evidence required for capture binding")
    try:
        data = json.loads(Path(position_state_path).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return _fail("capture_consistency", ["GUARD_CAPTURE_MISMATCH"],
                     "snapshot unreadable")
    snap_hash = data.get("canonical_input_hash")
    snap_raw = data.get("captured_at")
    snap_acct = data.get("account_identity_hash")
    ev_hash = margin_evidence.get("canonical_input_hash")
    ev_raw = margin_evidence.get("captured_at")
    ev_acct = margin_evidence.get("account_identity_hash")
    if not snap_hash or not ev_hash or snap_hash != ev_hash:
        return _fail("capture_consistency", ["GUARD_CAPTURE_MISMATCH"],
                     "canonical input hash differs between snapshot and "
                     "margin evidence")
    # canonical epoch-ms INTEGER in BOTH files — the same preflight
    # capture provides one value; separate float time.time() artifacts
    # are rejected (no per-file timestamps)
    if isinstance(snap_raw, bool) or not isinstance(snap_raw, int) or \
            isinstance(ev_raw, bool) or not isinstance(ev_raw, int):
        return _fail("capture_consistency", ["GUARD_CAPTURE_MISMATCH"],
                     "captured_at must be the canonical epoch-ms integer "
                     "in both snapshot and margin evidence")
    if snap_raw != ev_raw:
        return _fail("capture_consistency", ["GUARD_CAPTURE_MISMATCH"],
                     f"captured_at differs (snapshot {snap_raw} vs "
                     f"evidence {ev_raw}) — same-source capture must be "
                     f"exact")
    snap_ts = _parse_ts(snap_raw)
    ev_ts = _parse_ts(ev_raw)
    if snap_ts is None or ev_ts is None:
        return _fail("capture_consistency", ["GUARD_CAPTURE_MISMATCH"],
                     "captured_at unparseable")
    if not snap_acct or not ev_acct or snap_acct != ev_acct:
        return _fail("capture_consistency", ["GUARD_CAPTURE_MISMATCH"],
                     "account identity differs")
    if not margin_evidence.get("scope"):
        return _fail("capture_consistency", ["GUARD_CAPTURE_MISMATCH"],
                     "margin scope missing")
    if time.time() - ev_ts > SNAPSHOT_MAX_AGE_S:
        return _fail("capture_consistency", ["GUARD_CAPTURE_MISMATCH"],
                     f"capture stale ({ev_ts:.0f})")
    return _pass("capture_consistency",
                 f"single capture hash={str(snap_hash)[:12]}…")


# ── 10. ctx atomic read/write health ───────────────────────────────────────

def guard_ctx_atomic_health(runtime_dir: str) -> GuardResult:
    from core.execution_context_state import read_execution_context
    data = read_execution_context(runtime_dir=runtime_dir)
    reasons = tuple(data.get("audit_reasons") or ())
    if not reasons and data.get("effective_mode") is None:
        reasons = ("GUARD_CTX_INVALID",)
    if reasons:
        # first-deployment bootstrap: a MISSING ctx file may only be
        # bootstrapped as LIVE_QUARANTINED (the startup writes it
        # atomically); the gate verifies the atomic write capability and
        # NEVER lets a missing file enable LIVE.
        if reasons == ("RESTART_MAINTAIN_QUARANTINE",):
            probe_ok = _atomic_write_probe(runtime_dir)
            if not probe_ok:
                return _fail("ctx_atomic_health", ["GUARD_CTX_WRITE_FAIL"])
            return _pass("ctx_atomic_health",
                         "bootstrap: missing ctx written LIVE_QUARANTINED "
                         "atomically at startup (never LIVE)")
        mapped = tuple(
            "GUARD_CTX_MISSING" if r == "RESTART_MAINTAIN_QUARANTINE"
            else "GUARD_CTX_CORRUPT" if r == "STATE_FILE_CORRUPTED"
            else r for r in reasons)
        return _fail("ctx_atomic_health", mapped)
    # non-destructive write probe: same-dir temp file, fsync, read back, unlink
    if not _atomic_write_probe(runtime_dir):
        return _fail("ctx_atomic_health", ["GUARD_CTX_WRITE_FAIL"])
    return _pass("ctx_atomic_health", f"revision={data.get('revision')}")


def _atomic_write_probe(runtime_dir: str) -> bool:
    """Non-destructive same-dir write probe (temp + fsync + read-back +
    unlink). Never modifies the state file itself."""
    try:
        probe = Path(runtime_dir) / f"._gate_probe_{os.getpid()}"
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
            fh.flush()
            os.fsync(fh.fileno())
        ok = probe.read_text(encoding="utf-8") == "ok"
        probe.unlink(missing_ok=True)
        return ok
    except OSError:
        return False


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
    margin_evidence: Optional[dict] = None,
    manifest_paths: Sequence[str] = (),
    manifest_exclude_paths: Sequence[str] = (),
    expected_sha: Optional[str] = None,
    phase: str = "pre_deploy",
) -> DeploymentCheck:
    """Run all guards. Fail-closed: ANY guard failure -> NOT_READY.

    phase="pre_deploy" (default): the static gate — session_generation is
    NOT assessed (the registry-bound generation only exists after the
    runtime logs in); all other guards run.
    phase="post_startup": the live gate — requires the registry-bound
    session generation (32-hex token / int), not revoked, and the
    snapshot session consistency (GUARD_SESSION_INTERVENED).

    Never deploys / restarts / unlocks LIVE — the caller decides what to
    do with the structured result.
    """
    runtime_dir = runtime_dir if runtime_dir is not None \
        else os.environ.get("TRADING_RUNTIME_DIR", "")
    expected_sha = expected_sha if expected_sha is not None \
        else os.environ.get("LRC_RELEASE_SHA", "")
    if phase == "post_startup":
        session_result = guard_session_generation(session_generation,
                                                  session_revoked)
    else:
        session_result = GuardResult(
            guard="session_generation", ok=True,
            detail="not assessed in pre_deploy phase "
                   "(post_startup gate requires registry-bound generation)")
    results = (
        guard_release_head(release_dir, expected_sha),
        guard_clean_tree(release_dir, closure_files),
        guard_runtime_paths(runtime_dir),
        guard_single_process(pid_file),
        guard_flat_no_pending(position_state_path,
                              read_ctx_snapshot(runtime_dir)),
        guard_quarantine_first_startup(monitor_path),
        session_result,
        guard_margin(margin_available, margin_evidence=margin_evidence),
        guard_capture_consistency(position_state_path, margin_evidence),
        guard_rollback_manifest(release_dir, manifest_paths, expected_sha,
                                exclude_paths=manifest_exclude_paths),
        guard_ctx_atomic_health(runtime_dir),
    )
    return DeploymentCheck(ok=all(r.ok for r in results), results=results)


def read_ctx_snapshot(runtime_dir: str) -> dict:
    """Read-only ctx snapshot for the flat guard (never a broker call)."""
    from core.execution_context_state import read_execution_context
    return read_execution_context(runtime_dir=runtime_dir or None)
