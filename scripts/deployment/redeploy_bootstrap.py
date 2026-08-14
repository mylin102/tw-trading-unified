#!/usr/bin/env python3
"""Auditable redeploy bootstrap/reset for a stale execution_context.

The pre_deploy gate can be blocked by a stale runtime ctx (e.g. a
SESSION_LOGOUT + timestamp session_id left by a STOPPED process). This
tool performs a CONTROLLED reset:

  - DRY-RUN by default: validates every guard and reports what --apply
    would do, WITHOUT touching any file.
  - --apply: only when ALL guards pass —
      1. no active process/pid lock (the pid file is absent or the pid
         is not alive)
      2. fresh live_broker futures-flat / no-open-orders snapshot (the
         same canonical preflight JSON as the deployment gate)
      3. sealed live config profile (config/futures_live.yaml + sha256)
      4. no pending safety reconcile in the ctx
      5. the ctx is readable and in an acceptable stale state
         (SESSION_LOGOUT / RESTART_MAINTAIN_QUARANTINE /
         AUTH_SESSION_UNAVAILABLE). Corrupt,
         missing or any other audit state => refuse.
    Then:
      - archives the OLD ctx (full content + sha256 + timestamp) to
        <runtime>/logs/context_history/<sha256>_<ts>.json
      - atomically rewrites the ctx as LIVE_QUARANTINED with
        audit_reasons=(REDEPLOY_BOOTSTRAP,), session_id=None,
        config_hash=<profile sha256> — NEVER LIVE_READY, never a
        session generation.

Refusals NEVER modify the ctx. post_startup still requires the
registry-bound session generation (this tool only prepares pre_deploy).
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# acceptable stale audit states for the reset (anything else => refuse)
_ACCEPTABLE_STALE = {
    "SESSION_LOGOUT",
    "RESTART_MAINTAIN_QUARANTINE",
    # A previous stopped process could not authenticate.  With every other
    # bootstrap guard passing, archive that stale failure and restart only in
    # LIVE_QUARANTINED; the post-startup certificate/gate still controls any
    # later LIVE_READY transition.
    "AUTH_SESSION_UNAVAILABLE",
    # A stale EXIT_ONLY quarantine (the broker was NOT flat at the last
    # renewal / the capability legs mismatched) is retryable: the bootstrap
    # re-verifies the CURRENT flatness with a fresh canonical live_broker
    # snapshot (guard_flat_no_pending) and the sealed profile before any
    # reset; the archived stale audits never promote LIVE.  Refusing them
    # would deadlock the recovery (only a manual ctx deletion could clear).
    "BROKER_NOT_FLAT",
    "EXIT_ONLY_RENEWAL:EXIT_ONLY_POSITION_MISMATCH",
    # A previous process may have quarantined itself because PM2 injected an
    # older LRC_RELEASE_SHA. Fresh canonical broker evidence and the sealed
    # profile remain mandatory before resetting this stale context.
    "RELEASE_IDENTITY_MISMATCH",
}
_BLOCKING_AUDIT = {"SAFETY_STOP_RECONCILE_PENDING"}
_BOOTSTRAP_REASON = "REDEPLOY_BOOTSTRAP"
_RETRY_GATE_REASON = "POST_STARTUP_GATE_FAILED"


def _audit_acceptance(audits: set, retry_gate: bool, applying: bool):
    """Return None if the ctx audit state is acceptable for a reset,
    else a refusal reason string.

    - a pending safety reconcile ALWAYS refuses (never reset under
      reconcile)
    - classic stale states (SESSION_LOGOUT / RESTART_MAINTAIN_QUARANTINE /
      AUTH_SESSION_UNAVAILABLE) are always acceptable
    - a POST_STARTUP_GATE_FAILED ctx (with its known GUARD_* refusal
      codes) is retryable ONLY via --retry-post-startup-gate WITH
      --apply; default / dry-run / --apply alone refuse it
    - ANY other/unknown reason refuses (no silent re-bootstrap)"""
    if audits & _BLOCKING_AUDIT:
        return ("safety reconcile pending — refuse (never reset under "
                "reconcile)")
    if audits.issubset(_ACCEPTABLE_STALE):
        return None
    if (applying and retry_gate and _RETRY_GATE_REASON in audits and
            all(a in _ACCEPTABLE_STALE or a == _RETRY_GATE_REASON
                or a.startswith("GUARD_") for a in audits)):
        return None
    return (f"unacceptable ctx audit state {sorted(audits)} — refuse "
            f"(retry of POST_STARTUP_GATE_FAILED requires "
            f"--retry-post-startup-gate WITH --apply)")


def _ctx_path(runtime_dir: str) -> Path:
    return Path(runtime_dir) / "execution_context.json"


def _read_ctx(runtime_dir: str):
    """Return (data, raw_bytes) or raise a ValueError with the reason."""
    p = _ctx_path(runtime_dir)
    if not p.is_file():
        raise ValueError("ctx missing (nothing to reset)")
    raw = p.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("ctx corrupt (unparseable JSON) — refuse")
    if not isinstance(data, dict):
        raise ValueError("ctx corrupt (not an object) — refuse")
    return data, raw


def _pid_alive(pid_file: str) -> bool:
    try:
        pid = int(Path(pid_file).read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runtime-dir", required=True)
    ap.add_argument("--pid-file", required=True)
    ap.add_argument("--position-state", required=True)
    ap.add_argument("--margin-evidence", required=True)
    ap.add_argument("--config-profile", required=True)
    ap.add_argument("--config-hash", required=True)
    ap.add_argument("--retry-post-startup-gate", action="store_true",
                    help="allow resetting a ctx whose audit carries "
                         "POST_STARTUP_GATE_FAILED + the known GUARD_* "
                         "refusal codes (REQUIRES --apply; default / "
                         "dry-run / --apply alone refuse this reason)")
    ap.add_argument("--apply", action="store_true",
                    help="perform the reset (default: dry-run, no writes)")
    args = ap.parse_args()

    rt = args.runtime_dir
    failures = []

    # 1. no active process/pid lock
    if _pid_alive(args.pid_file):
        failures.append("pid file references an ACTIVE process — refuse")

    # 2. fresh live_broker futures-flat / no-orders snapshot. Local strategy
    # state may be stale/ghosted after a stopped process; use the same
    # canonical broker evidence supplied for the margin proof.
    from core.deployment_safety_gate import guard_flat_no_pending
    flat = guard_flat_no_pending(args.margin_evidence, {"requested_mode": "live"})
    if not flat.ok:
        failures.append(f"snapshot not flat: {flat.reasons}")

    # 3. sealed live config profile + sha256
    from core.deployment_safety_gate import guard_config_profile
    prof = guard_config_profile(args.config_profile, args.config_hash)
    if not prof.ok:
        failures.append(f"config profile invalid: {prof.reasons}")

    # 4/5. ctx readable + acceptable stale state + no reconcile pending
    try:
        ctx, raw = _read_ctx(rt)
    except ValueError as e:
        failures.append(str(e))
        ctx, raw = None, b""

    if ctx is not None:
        audits = set(ctx.get("audit_reasons") or ())
        _reason = _audit_acceptance(audits,
                                    args.retry_post_startup_gate,
                                    args.apply)
        if _reason:
            failures.append(_reason)
        elif not audits and ctx.get("effective_mode") != "live_quarantined":
            failures.append(
                "ctx in an unexpected clean state — refuse (nothing to "
                "reset)")

    if failures:
        print("REFUSE — " + "; ".join(failures), file=sys.stderr)
        print("ctx NOT modified (dry-run/refusal)")
        return 2

    profile_hash = args.config_hash
    print("ALL GUARDS PASS")
    print(f"  ctx: {_ctx_path(rt)}")
    print(f"  archive target: {rt}/logs/context_history/")
    print(f"  reset reason: {_BOOTSTRAP_REASON}")
    print("  new ctx: effective_mode=live_quarantined "
          "live_order_allowed=False session_id=None "
          f"config_hash={profile_hash[:12]}…")
    if not args.apply:
        print("DRY_RUN — no files modified; re-run with --apply to reset")
        return 0

    # archive the old ctx (full content + sha256 + timestamp)
    hist_dir = Path(rt) / "logs" / "context_history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    ctx_sha = hashlib.sha256(raw).hexdigest()
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    archive = hist_dir / f"{ctx_sha}_{ts}.json"
    archive.write_text(json.dumps({
        "sha256": ctx_sha,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "ctx": ctx,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    # atomic rewrite: LIVE_QUARANTINED + REDEPLOY_BOOTSTRAP (never
    # LIVE_READY, never a session generation)
    new_ctx = {
        "requested_mode": "live",
        "effective_mode": "live_quarantined",
        "live_order_allowed": False,
        "audit_reasons": [_BOOTSTRAP_REASON],
        "revision": 1,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "account_id_hash": None,
        "session_id": None,
        "process_start_id": None,
        "config_hash": profile_hash,
        "state_namespace": "live",
    }
    p = _ctx_path(rt)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(new_ctx, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    print(f"APPLIED — old ctx archived: {archive}")
    print(f"APPLIED — ctx reset to LIVE_QUARANTINED "
          f"(reason={_BOOTSTRAP_REASON})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
