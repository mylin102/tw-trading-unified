#!/usr/bin/env python3
"""RED tests: Live Route Certification (design phase — API not yet implemented).

Importing core.live_route_certificate fails (module does not exist yet) →
collection error → RED. These tests pin the design contract from
.planning/live_route_certification_design.md.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.live_route_certificate import (   # noqa: F401 — RED: module TBD
    LiveBrokerCertificate,
    build_live_broker_certificate,
    certify_route,
    validate_live_broker_certificate,
)


# ── helpers ────────────────────────────────────────────────────────────────

def _recording_api(flat=True, open_orders=(), margin=1_000_000.0,
                   trading_limits_error=False, subscribe_ok=True):
    """Recording api mock: any order/cancel/modify call is a test failure."""
    calls = []
    near = SimpleNamespace(code="TMFH6", delivery_date="2026-08-19",
                           name="TMFH6", category="FUT", update_time="x")
    far = SimpleNamespace(code="TMFI6", delivery_date="2026-09-16",
                          name="TMFI6", category="FUT", update_time="x")

    def _wrap(name, fn):
        def _w(*a, **k):
            calls.append(name)
            return fn(*a, **k)
        return _w

    api = MagicMock()
    api.place_order = _wrap("place_order", MagicMock(return_value=None))
    api.cancel_order = _wrap("cancel_order", MagicMock(return_value=None))
    api.modify_order = _wrap("modify_order", MagicMock(return_value=None))
    api.subscribe = _wrap("subscribe",
                          MagicMock(side_effect=None if subscribe_ok else Exception("sub fail")))
    api.unsubscribe = _wrap("unsubscribe", MagicMock(return_value=None))
    api.fetch_positions = _wrap("fetch_positions",
                                MagicMock(return_value=[] if flat else [{"code": "TMFH6"}]))
    api.list_open_orders = _wrap("list_open_orders", MagicMock(return_value=list(open_orders)))
    api.margin = _wrap("margin", MagicMock(return_value={"available_margin": margin}))
    if trading_limits_error:
        api.trading_limits = _wrap("trading_limits",
                                   MagicMock(side_effect=RuntimeError("limits unavailable")))
    else:
        api.trading_limits = _wrap("trading_limits",
                                   MagicMock(return_value={"trading_limits": []}))
    api.resolve_contract = _wrap("resolve_contract",
                                 MagicMock(side_effect=lambda code: near if code == "TMFH6" else far))
    api.calls = calls
    return api


def _preflight(api, **over):
    from core.live_broker_preflight import collect_read_only_preflight
    result = collect_read_only_preflight(api, product="TMF")
    result.update(over)
    return result


# ── 1. certificate binds identity + snapshot + checks ──────────────────────

def test_certificate_binds_identity_and_snapshots():
    api = _recording_api()
    cert = build_live_broker_certificate(_preflight(api), api, process_start_id="p-1")
    assert cert.process_start_id == "p-1"
    assert cert.account_hash                      # hashed, not plaintext
    assert cert.near_code == "TMFH6" and cert.far_code == "TMFI6"
    assert cert.position_snapshot_ts and cert.order_snapshot_ts
    assert cert.margin_available > 0
    assert "MARGIN_OK" in cert.query_results
    assert "BROKER_FLAT" in cert.query_results
    assert "NO_OPEN_ORDERS" in cert.query_results
    assert len(cert.bidask_subscribed) == 2 and len(cert.bidask_unsubscribed) == 2
    assert cert.bidask_subscribed == ("NEAR", "FAR")


# ── 2. every required query fails closed ───────────────────────────────────

@pytest.mark.parametrize("scenario", [
    ("margin missing", {"margin": None}),
    ("margin zero", {"margin": 0.0}),
])
def test_margin_failure_quarantines(scenario):
    api = _recording_api()
    pre = _preflight(api)
    if scenario[0] == "margin missing":
        pre["margin"] = None
    else:
        pre["margin"] = {"available_margin": 0.0}
    cert, failures = certify_route(pre, api, process_start_id="p-1")
    assert cert is None and any("MARGIN" in f for f in failures)


def test_unauthenticated_account_fails():
    api = _recording_api()
    api.login = MagicMock(return_value=None)  # not authenticated
    api.account = None
    cert, failures = certify_route(_preflight(api), api, process_start_id="p-1")
    assert cert is None and any("ACCOUNT" in f for f in failures)


def test_broker_not_flat_fails():
    api = _recording_api(flat=False)
    cert, failures = certify_route(_preflight(api), api, process_start_id="p-1")
    assert cert is None and any("FLAT" in f for f in failures)


def test_open_orders_fail():
    api = _recording_api(open_orders=[{"order_id": "ORD-1"}])
    cert, failures = certify_route(_preflight(api), api, process_start_id="p-1")
    assert cert is None and any("OPEN_ORDERS" in f for f in failures)


def test_subscribe_failure_fails():
    api = _recording_api(subscribe_ok=False)
    cert, failures = certify_route(_preflight(api), api, process_start_id="p-1")
    assert cert is None and any("BIDASK" in f for f in failures)


def test_contracts_not_distinct_fails():
    api = _recording_api()
    pre = _preflight(api)
    pre["contracts"] = {"near": {"code": "TMFH6"}, "far": {"code": "TMFH6"}}  # same code
    cert, failures = certify_route(pre, api, process_start_id="p-1")
    assert cert is None and any("CONTRACT" in f for f in failures)


def test_snapshot_code_consistency_fails():
    api = _recording_api()
    pre = _preflight(api)
    pre["positions"] = [{"code": "TMFX6"}]  # not near/far codes
    cert, failures = certify_route(pre, api, process_start_id="p-1")
    assert cert is None and any("CONSIST" in f for f in failures)


# ── 3. staleness / skew / identity changes rejected ────────────────────────

def test_stale_certificate_rejected():
    api = _recording_api()
    cert = build_live_broker_certificate(_preflight(api), api, process_start_id="p-1")
    old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    cert = LiveBrokerCertificate(**{**cert.__dict__, "captured_at": old})
    ok, reasons = validate_live_broker_certificate(
        cert, process_start_id="p-1", account_hash=cert.account_hash,
        near_code="TMFH6", far_code="TMFI6")
    assert not ok and any("STALE" in r for r in reasons)


def test_future_clock_skew_rejected():
    api = _recording_api()
    cert = build_live_broker_certificate(_preflight(api), api, process_start_id="p-1")
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    cert = LiveBrokerCertificate(**{**cert.__dict__, "captured_at": future})
    ok, reasons = validate_live_broker_certificate(
        cert, process_start_id="p-1", account_hash=cert.account_hash,
        near_code="TMFH6", far_code="TMFI6")
    assert not ok and any("SKEW" in r for r in reasons)


def test_different_process_start_rejected():
    api = _recording_api()
    cert = build_live_broker_certificate(_preflight(api), api, process_start_id="p-1")
    ok, reasons = validate_live_broker_certificate(
        cert, process_start_id="p-2", account_hash=cert.account_hash,
        near_code="TMFH6", far_code="TMFI6")
    assert not ok and any("PROCESS" in r for r in reasons)


def test_different_account_rejected():
    api = _recording_api()
    cert = build_live_broker_certificate(_preflight(api), api, process_start_id="p-1")
    ok, reasons = validate_live_broker_certificate(
        cert, process_start_id="p-1", account_hash="deadbeef",
        near_code="TMFH6", far_code="TMFI6")
    assert not ok and any("ACCOUNT" in r for r in reasons)


def test_changed_contract_codes_rejected():
    api = _recording_api()
    cert = build_live_broker_certificate(_preflight(api), api, process_start_id="p-1")
    ok, reasons = validate_live_broker_certificate(
        cert, process_start_id="p-1", account_hash=cert.account_hash,
        near_code="TMFJ6", far_code="TMFI6")
    assert not ok and any("CONTRACT" in r for r in reasons)


# ── 4. session binding: copied JSON from another process is rejected ───────

def test_copied_json_from_other_process_rejected():
    api_a = _recording_api()
    cert_a = build_live_broker_certificate(_preflight(api_a), api_a, process_start_id="p-A")
    payload = json.loads(json.dumps(cert_a.__dict__, default=str))
    # another process / session tries to consume the copied payload
    api_b = _recording_api()
    cert, failures = certify_route(payload, api_b, process_start_id="p-B")
    assert cert is None, "copied certificate must not authorize live"
    assert any("SESSION" in f or "PROCESS" in f for f in failures)


# ── 5. incomplete certificate → LIVE_QUARANTINED + zero live submit ────────

def test_incomplete_certificate_quarantines_zero_submit():
    api = _recording_api()
    pre = _preflight(api)
    del pre["margin"]
    cert, failures = certify_route(pre, api, process_start_id="p-1")
    assert cert is None
    assert "place_order" not in api.calls
    assert "cancel_order" not in api.calls
    assert "modify_order" not in api.calls


# ── 6. PAPER mode cannot consume a certificate ─────────────────────────────

def test_paper_mode_cannot_consume_certificate():
    from core.mode_transition import paper_context, ExecutionMode
    api = _recording_api()
    cert = build_live_broker_certificate(_preflight(api), api, process_start_id="p-1")
    with paper_context(ExecutionMode.PAPER) as ctx:
        # even with a valid cert present, paper mode must not authorize live
        assert not ctx.is_live_ready()
        with pytest.raises(Exception):
            ctx.assert_live_order_allowed()


# ── 7. trading_limits warning-only; margin failure is fatal ────────────────

def test_trading_limits_failure_is_warning_only():
    api = _recording_api(trading_limits_error=True)
    cert = build_live_broker_certificate(_preflight(api), api, process_start_id="p-1")
    assert cert is not None, "trading_limits failure must remain a warning"
    assert "TRADING_LIMITS" in cert.warnings


def test_margin_unreadable_is_fatal():
    api = _recording_api()
    pre = _preflight(api)
    pre["margin"] = {"error": "unreadable"}
    cert, failures = certify_route(pre, api, process_start_id="p-1")
    assert cert is None and any("MARGIN" in f for f in failures)


# ── 8. zero order/cancel/modify calls anywhere in certification ────────────

def test_no_order_calls_in_full_certification_cycle():
    api = _recording_api()
    cert = build_live_broker_certificate(_preflight(api), api, process_start_id="p-1")
    ok, _ = validate_live_broker_certificate(
        cert, process_start_id="p-1", account_hash=cert.account_hash,
        near_code="TMFH6", far_code="TMFI6")
    assert ok
    forbidden = [c for c in api.calls
                 if c in ("place_order", "cancel_order", "modify_order")]
    assert forbidden == [], f"certification must never call order APIs; got {forbidden}"
