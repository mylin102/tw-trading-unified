#!/usr/bin/env python3
"""RED tests v6: Live Route Certification — MONITOR INTEGRATION phase
(codex round-7 P0-5/P0-6).

This file is EXPLICITLY RED until the separately-reviewed monitor wiring
phase: monitor.py is deliberately untouched in the core phase, so the AST
call-site assertion fails (hits=[522]) — that is the expected, documented
RED. The PAPER-path test asserts existing behavior (green).

P0-6 integration mapping (design §6.5): before any order route —
  1. cert, failures = certify_route(api, ...)        # fresh in-process
  2. failures non-empty → LIVE_QUARANTINED + audit reasons (fail-closed)
  3. ctx = transition_with_certificate(ctx, cert, issuer, ctx_runtime)
  4. not is_live_ready() → LIVE_QUARANTINED (assert_live_order_allowed raises)
PAPER path unchanged: paper_context() never consumes a certificate.
"""

import ast
from pathlib import Path

import pytest


def _monitor_path():
    return Path(__file__).resolve().parents[2] / "strategies" / "futures" / "monitor.py"


def test_ast_call_site_scan_no_legacy_transition_in_monitor():
    """RED until wiring phase: monitor.py must not call
    transition_to_live_ready (known legacy bypass at :522)."""
    monitor = _monitor_path()
    assert monitor.exists(), f"monitor not found: {monitor}"
    tree = ast.parse(monitor.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "transition_to_live_ready":
                hits.append(node.lineno)
            elif isinstance(fn, ast.Name) and fn.id == "transition_to_live_ready":
                hits.append(node.lineno)
    assert not hits, f"monitor.py still calls transition_to_live_ready at: {hits}"


def test_monitor_uses_certificate_flow_before_order_routes():
    """RED until wiring phase: monitor.py must reference certify_route or
    transition_with_certificate (the future wiring) — today it does not."""
    monitor = _monitor_path()
    tree = ast.parse(monitor.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert "certify_route" in names or "transition_with_certificate" in names, \
        "monitor wiring (certify_route / transition_with_certificate) not present"


def test_paper_path_unchanged():
    """GREEN now: PAPER behavior identical to today — no live authorization;
    paper orders stay allowed (assert_live_order_allowed does NOT raise in
    paper mode); no certificate can flip PAPER into LIVE_READY."""
    from core.mode_transition import paper_context
    ctx = paper_context(account_id="A1")
    assert not ctx.is_live_ready()
    assert ctx.to_dict().get("live_order_allowed") is False
    # paper orders are legal by design — the gate only blocks LIVE orders
    ctx.assert_live_order_allowed()          # must NOT raise (paper path)
    # is_live_ready requires requested_mode == LIVE, so PAPER can never
    # become LIVE_READY no matter what (cert or not)
    assert ctx.to_dict().get("requested_mode") != "live"


# ── Phase 2 test matrix (RED until the separately-reviewed wiring phase) ───

def _repo_root():
    return Path(__file__).resolve().parents[2]


def test_release_identity_check_wired_before_certification():
    """(3) RED: the LIVE startup path must verify cwd/HEAD == LRC_RELEASE_SHA
    before any certification — the wiring does not exist yet."""
    monitor = _monitor_path()
    text = monitor.read_text(encoding="utf-8")
    assert "LRC_RELEASE_SHA" in text, \
        "release-identity env check not wired into monitor startup"


def test_margin_floor_config_key_defined():
    """(4) RED: the TMF pair margin floor must exist in the effective config
    (owner/version documented) — fail-closed until the config key lands."""
    cfg = _repo_root() / "config" / "futures.yaml"
    assert cfg.exists()
    text = cfg.read_text(encoding="utf-8")
    assert "live_required_margin_per_pair" in text, \
        "mts.live_required_margin_per_pair not defined in futures.yaml"


def test_startup_live_path_recertifies_with_certificate_flow():
    """(1)+(5) RED: the LIVE startup path must reference the certificate
    flow (certify_route / transition_with_certificate) — the legacy
    transition_to_live_ready path is closed in core, so without the wiring
    a LIVE startup can never reach LIVE_READY."""
    monitor = _monitor_path()
    text = monitor.read_text(encoding="utf-8")
    assert "certify_route" in text or "transition_with_certificate" in text, \
        "monitor LIVE startup does not reference the certificate flow"


def test_reconnect_path_recertifies_after_safe_login():
    """(5) RED: main._try_shioaji_reconnect must re-certify after the
    reconnected safe_login (the new registry generation kills the old cert)."""
    main_py = _repo_root() / "main.py"
    assert main_py.exists()
    text = main_py.read_text(encoding="utf-8")
    assert "certify_route" in text or "transition_with_certificate" in text, \
        "main reconnect path does not re-certify after safe_login"


def test_logout_invalidates_monitor_certificate_route():
    """(5) RED: after broker logout the monitor must hold no usable
    certificate — shioaji_session.logout unregisters (core, green); the
    monitor must not retain a LIVE_READY context across it."""
    monitor = _monitor_path()
    text = monitor.read_text(encoding="utf-8")
    assert "unregister_session" in text or "session_registry" in text, \
        "monitor does not reference session invalidation on logout"
