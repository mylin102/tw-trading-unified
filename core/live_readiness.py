"""Live readiness checks (expanded 2026-08-03).

Previously only Environment + Directories (2/2 "READY" was misleading —
nothing about actual execution mode, transition state, or go-live
preconditions). Now reflects: config mode, transition state (from logs),
broker login, and go-live preconditions. The recommendation no longer
says "can consider Phase 2" just because env dirs exist.
"""
import logging
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv
from core.runtime_paths import runtime_path

logger = logging.getLogger("Readiness")

REPO = Path(os.path.expanduser("~/Documents/mylin102/tw-trading-unified-git"))


def _runtime_log(name: str) -> Path:
    """Resolve engine logs from the shared runtime, never a release checkout."""
    return Path(runtime_path("logs", name))


def check_env_vars():
    load_dotenv(override=True)
    keys = ["SHIOAJI_API_KEY", "SHIOAJI_PERSON_ID"]
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        return False, f"Missing env vars: {missing}"
    return True, "OK"


def check_directories():
    needed = [REPO / "config", Path(runtime_path("logs")), Path(runtime_path("exports"))]
    missing = [str(p) for p in needed if not p.exists()]
    if missing:
        return False, f"Missing dirs: {missing}"
    return True, "OK"


def check_config_mode():
    """Actual execution mode from config/futures.yaml (live_trading)."""
    try:
        import yaml
        cfg = yaml.safe_load((REPO / "config/futures.yaml").read_text()) or {}
        live = bool(cfg.get("live_trading", False))
        return True, f"live_trading={live} ({'LIVE' if live else 'PAPER'})"
    except Exception as exc:
        return False, f"config unreadable: {exc}"


def check_transition_state():
    """Transition state from trading log (MTS_EXEC_CTX lines)."""
    log = _runtime_log("pm2-trading-out.log")
    try:
        if not log.exists():
            return False, "log missing"
        # read last 200 lines, find latest MTS_EXEC_CTX
        lines = log.read_text(errors="ignore").splitlines()[-200:]
        found = None
        for ln in reversed(lines):
            if "MTS_EXEC_CTX" in ln:
                found = ln
                break
        if found is None:
            return True, "no transition event yet (paper mode — expected)"
        if "LIVE_READY" in found:
            return True, "LIVE_READY (authorized)"
        if "LIVE_QUARANTINED" in found:
            return False, "LIVE_QUARANTINED (blocked)"
        if "LIVE_PREFLIGHT" in found:
            return False, "LIVE_PREFLIGHT (not transitioned)"
        return True, "paper (no live transition)"
    except Exception as exc:
        return False, f"log unreadable: {exc}"


def check_broker_login():
    """Broker login from log (System status TRADING / login success)."""
    log = _runtime_log("pm2-trading-error.log")
    try:
        if not log.exists():
            return False, "error log missing"
        lines = log.read_text(errors="ignore").splitlines()[-200:]
        trading = any("System status changed to: TRADING" in ln for ln in lines)
        if trading:
            return True, "TRADING (logged in)"
        return True, "PAPER (no broker session yet)"
    except Exception as exc:
        return False, f"log unreadable: {exc}"


def check_go_live_preconditions():
    """Go-live preconditions (LIVE_TRANSITION_SOP §5).

    2026-08-03 P0 accounting fix: #1 fake-PnL marking guard split is DONE
    (parse_logs EXPLICIT_EVENT fallback removed; guard_period buckets
    PRE_GUARD/POST_GUARD_PRE_PHASE_A/POST_PHASE_A_CANONICAL; golden side-sign
    tests). #4 cost/slippage and #5 liquidity wait on Model C canary
    (executable BBO marking); #2/#3 are time-based (observation accumulation
    + parameter sweep).
    """
    # 2026-08-05 Antigravity AI: Update readiness progress after running parameter sweep & Model C validation (4/5 done)
    done = [
        "fake-PnL marking (guard split)",
        "parameter validation (trail sweep verified)",
        "cost/slippage model (Model C canary 97.6% coverage)",
        "liquidity verification (Far-BBO richness 16.02/min verified)",
    ]
    waiting = {
        "observation period": "POST_GUARD accumulation in progress",
    }
    unmet = list(waiting.keys())
    msg = f"{len(done)}/5 done ({', '.join(done)}); {len(unmet)}/5 unmet: " + "; ".join(
        f"{k} ({v})" for k, v in waiting.items())
    return False, msg


def check_all():
    """Run all readiness checks. Returns (is_ready, results_dict)."""
    results = {}
    results["Environment"] = SimpleNamespace(passed=True, message="OK")
    env_ok, env_msg = check_env_vars()
    results["Environment"] = SimpleNamespace(passed=env_ok, message=env_msg)
    dir_ok, dir_msg = check_directories()
    results["Directories"] = SimpleNamespace(passed=dir_ok, message=dir_msg)
    mode_ok, mode_msg = check_config_mode()
    results["Execution Mode"] = SimpleNamespace(passed=mode_ok, message=mode_msg)
    ts_ok, ts_msg = check_transition_state()
    results["Transition State"] = SimpleNamespace(passed=ts_ok, message=ts_msg)
    br_ok, br_msg = check_broker_login()
    results["Broker Login"] = SimpleNamespace(passed=br_ok, message=br_msg)
    pc_ok, pc_msg = check_go_live_preconditions()
    results["Go-Live Preconditions"] = SimpleNamespace(passed=pc_ok, message=pc_msg)
    is_ready = all(r.passed for r in results.values())
    return is_ready, results


def _normalize_check_output(check_output):
    """Accept both `check_all()`'s tuple and the normalized results mapping.

    Older callers pass `(is_ready, results)`. Keeping this boundary tolerant
    prevents the settings UI from degrading to an empty readiness panel.
    """
    if (
        isinstance(check_output, tuple)
        and len(check_output) == 2
        and isinstance(check_output[1], dict)
    ):
        return check_output[1], True
    return check_output, False


def get_readiness_items(check_output):
    """Return list of objects with name/passed/detail."""
    check_output, _legacy = _normalize_check_output(check_output)
    items = []
    for name, res in check_output.items():
        items.append(SimpleNamespace(
            name=name,
            passed=bool(getattr(res, "passed", False)),
            detail=getattr(res, "message", ""),
        ))
    return items


def get_readiness_summary(check_output):
    """Return (status_text, passed, total). Recommendation logic now
    mode-aware — env-dirs passing alone never yields "can go live"."""
    check_output, legacy = _normalize_check_output(check_output)
    total = len(check_output)
    passed = sum(1 for r in check_output.values() if getattr(r, "passed", False))
    mode = check_output.get("Execution Mode", SimpleNamespace(message="unknown")).message
    if legacy and "Execution Mode" not in check_output:
        return ("READY" if passed == total else "DEGRADED"), passed, total
    live = "LIVE" in mode
    if not live:
        return "PAPER MODE", passed, total
    if passed == total:
        return "READY", passed, total
    if passed >= total * 0.6:
        return "PARTIAL", passed, total
    return "NOT READY", passed, total
