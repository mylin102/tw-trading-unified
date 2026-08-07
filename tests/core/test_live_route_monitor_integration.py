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
