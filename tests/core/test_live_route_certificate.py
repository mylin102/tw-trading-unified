#!/usr/bin/env python3
"""RED tests v2: Live Route Certification (codex B1-B6 — design not accepted
until these pass). core.live_route_certificate does not exist yet → RED.

B1: the fake api implements the REAL collect_read_only_preflight surface
(futopt_account / Contracts.Futures.<sym> / list_positions / list_trades /
margin / trading_limits / snapshots / subscribe / unsubscribe) — no
MagicMock fallback. B2: certify_route collects from the session itself and
never accepts an external payload. B3: margin capacity = explicit required
margin for 1 near + 1 far micro with boundary tests. B4: subscribe↔unsubscribe
symmetry, unsubscribe failure fatal, snapshot codes bound to resolved
contracts. B5: weak legacy preflight alone never reaches LIVE_READY; PAPER
rejects even a valid certificate. B6: in-memory issuance nonce — copied JSON
with a matching process_start_id still fails (NONCE_UNKNOWN).
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from core.live_route_certificate import (   # noqa: F401 — RED: module TBD
    CertificateIssuer,
    LiveBrokerCertificate,
    certify_route,
    validate_live_broker_certificate,
)


# ── adapter-faithful fake (B1) ─────────────────────────────────────────────

class _Account:
    def __init__(self, person_id="P1", broker_id="B1", account_id="A1"):
        self.person_id = person_id
        self.broker_id = broker_id
        self.account_id = account_id


class _Contract:
    def __init__(self, code, delivery="2026-08-19"):
        self.code = code
        self.delivery_date = datetime.strptime(delivery, "%Y-%m-%d").date()
        self.category = "FUT"
        self.symbol = "TMF"


class _Status:
    def __init__(self, status):
        self.status = status


class _Trade:
    def __init__(self, code, qty, status):
        self.code = code
        self.quantity = qty
        self.status = _Status(status)


class _Position:
    def __init__(self, code, qty):
        self.code = code
        self.quantity = qty
        self.pnl = 0.0


class _Margin:
    def __init__(self, available):
        self.available_margin = available
        self.equity_amount = available
        self.risk_indicator = 0


class _FuturesGroup:
    def __init__(self, by_symbol):
        self._by = by_symbol

    def __getattr__(self, symbol):
        return self._by[symbol]


class _Contracts:
    def __init__(self, contracts):
        self.Futures = _FuturesGroup({"TMF": contracts})


class _FakeApi:
    """Faithful to the real preflight collection contract. Any order/
    cancel/modify call raises (structural zero-order guarantee)."""

    def __init__(self, *, flat=True, open_orders=(), margin_val=1_000_000.0,
                 trading_limits_ok=True, subscribe_ok=True, unsubscribe_ok=True,
                 snapshot_codes=("TMFH6", "TMFI6"), account=None):
        self.futopt_account = account or _Account()
        self._contracts = [_Contract("TMFH6", "2026-08-19"),
                           _Contract("TMFI6", "2026-09-16")]
        self.Contracts = _Contracts(self._contracts)
        self._flat = flat
        self._open_orders = list(open_orders)
        self._margin_val = margin_val
        self._trading_limits_ok = trading_limits_ok
        self._subscribe_ok = subscribe_ok
        self._unsubscribe_ok = unsubscribe_ok
        self._snapshot_codes = list(snapshot_codes)
        self.calls = []

    def list_positions(self, account):
        self.calls.append("list_positions")
        return [] if self._flat else [_Position("TMFH6", 1)]

    def list_trades(self, *args):
        self.calls.append("list_trades")
        return [_Trade("TMFH6", 1, "Filled") for _ in self._open_orders]

    def margin(self, account):
        self.calls.append("margin")
        return None if self._margin_val is None else _Margin(self._margin_val)

    def trading_limits(self, account):
        self.calls.append("trading_limits")
        if not self._trading_limits_ok:
            raise RuntimeError("trading_limits unavailable")
        return {"trading_limits": []}

    def snapshots(self, contracts):
        self.calls.append("snapshots")
        return [_Contract(code) for code in self._snapshot_codes]

    def subscribe(self, contract, **kwargs):
        self.calls.append("subscribe")
        if not self._subscribe_ok:
            raise RuntimeError("subscribe failed")
        return None

    def unsubscribe(self, contract, **kwargs):
        self.calls.append("unsubscribe")
        if not self._unsubscribe_ok:
            raise RuntimeError("unsubscribe failed")
        return None

    # ── forbidden in certification (B6 requirement) ──
    def place_order(self, *a, **k):
        raise RuntimeError("ORDER_CALL_FORBIDDEN")

    def cancel_order(self, *a, **k):
        raise RuntimeError("ORDER_CALL_FORBIDDEN")

    def modify_order(self, *a, **k):
        raise RuntimeError("ORDER_CALL_FORBIDDEN")


def _certify(api, *, required_margin=200_000.0, **kw):
    return certify_route(api, process_start_id="p-1",
                         issuer=CertificateIssuer(),
                         required_margin=required_margin, **kw)


# ── B1: certification collects through the REAL preflight contract ─────────

def test_certify_route_collects_through_real_preflight_surface():
    api = _FakeApi()
    cert, failures = _certify(api)
    assert failures == [], f"unexpected failures: {failures}"
    # the real collection contract must have been exercised — not a mock fallback
    for method in ("list_positions", "list_trades", "margin", "snapshots",
                   "subscribe", "unsubscribe"):
        assert method in api.calls, f"{method} was never called"
    assert cert.margin_available == 1_000_000.0
    assert cert.near_code == "TMFH6" and cert.far_code == "TMFI6"


def test_certificate_binds_identity_and_snapshots():
    api = _FakeApi()
    cert, failures = _certify(api)
    assert cert.process_start_id == "p-1"
    assert cert.account_hash
    assert cert.position_snapshot_ts and cert.order_snapshot_ts
    assert "MARGIN_OK" in cert.query_results
    assert "BROKER_FLAT" in cert.query_results
    assert "NO_OPEN_ORDERS" in cert.query_results
    assert cert.bidask_subscribed == ("NEAR", "FAR")
    assert cert.bidask_unsubscribed == ("NEAR", "FAR")


# ── B3: margin capacity = required margin for 1 near + 1 far micro ─────────

@pytest.mark.parametrize("margin_val,expect_ok", [
    (None, False),          # missing
    (0.0, False),           # zero
    (199_999.0, False),     # just below requirement
    (200_000.0, True),      # exactly at requirement
    (1_000_000.0, True),    # comfortable
])
def test_margin_capacity_boundaries(margin_val, expect_ok):
    api = _FakeApi(margin_val=margin_val)
    cert, failures = _certify(api)
    if expect_ok:
        assert cert is not None and failures == []
    else:
        assert cert is None and any("MARGIN" in f for f in failures), failures


# ── B4: subscription symmetry + snapshot code binding ──────────────────────

def test_unsubscribe_failure_is_fatal():
    api = _FakeApi(unsubscribe_ok=False)
    cert, failures = _certify(api)
    assert cert is None
    assert any("QUOTE" in f or "SUB" in f for f in failures)


def test_subscribe_failure_is_fatal():
    api = _FakeApi(subscribe_ok=False)
    cert, failures = _certify(api)
    assert cert is None
    assert any("QUOTE" in f or "SUB" in f for f in failures)


def test_snapshot_codes_bound_to_resolved_contracts():
    api = _FakeApi(snapshot_codes=("TMFX6", "TMFH6"))
    cert, failures = _certify(api)
    assert cert is None
    assert any("CONSIST" in f for f in failures)


def test_broker_not_flat_fails():
    api = _FakeApi(flat=False)
    cert, failures = _certify(api)
    assert cert is None and any("FLAT" in f for f in failures)


def test_open_orders_fail():
    api = _FakeApi(open_orders=[{"order_id": "ORD-1"}])
    cert, failures = _certify(api)
    assert cert is None and any("OPEN_ORDERS" in f for f in failures)


# ── B2/B6: session-bound, unforgeable issuance ─────────────────────────────

def test_certify_route_collects_from_session_ignores_external_payload():
    # B2: certify_route has no payload parameter — it collects from the api
    # session itself; a forged payload cannot be injected
    api = _FakeApi()
    cert, failures = _certify(api)
    assert cert is not None
    assert "list_positions" in api.calls and "margin" in api.calls


def test_forged_copied_json_with_matching_process_id_fails():
    # B6: a copied certificate JSON (even with the SAME process_start_id)
    # must fail validation in a different process — the issuance nonce is
    # in-memory only
    issuer_a = CertificateIssuer()
    api_a = _FakeApi()
    cert_a, failures = certify_route(api_a, process_start_id="p-A", issuer=issuer_a)
    assert cert_a is not None and failures == []
    payload = json.loads(json.dumps(cert_a.__dict__, default=str))

    issuer_b = CertificateIssuer()   # attacker's process
    forged = LiveBrokerCertificate(**payload)
    ok, reasons = validate_live_broker_certificate(
        forged, issuer=issuer_b, process_start_id="p-A",
        account_hash=forged.account_hash,
        near_code="TMFH6", far_code="TMFI6")
    assert not ok, "copied certificate must not validate in another process"
    assert any("NONCE" in r for r in reasons)


def test_copied_json_with_forged_process_start_id_fails():
    issuer_a = CertificateIssuer()
    api_a = _FakeApi()
    cert_a, _ = certify_route(api_a, process_start_id="p-A", issuer=issuer_a)
    payload = json.loads(json.dumps(cert_a.__dict__, default=str))
    payload["process_start_id"] = "p-FORGED"

    issuer_b = CertificateIssuer()
    forged = LiveBrokerCertificate(**payload)
    ok, reasons = validate_live_broker_certificate(
        forged, issuer=issuer_b, process_start_id="p-FORGED",
        account_hash=forged.account_hash,
        near_code="TMFH6", far_code="TMFI6")
    assert not ok and any("NONCE" in r for r in reasons)


# ── staleness / skew / identity changes ────────────────────────────────────

def _validated(cert, issuer, **over):
    return validate_live_broker_certificate(
        cert, issuer=issuer, process_start_id=over.get("process_start_id", "p-1"),
        account_hash=over.get("account_hash", cert.account_hash),
        near_code=over.get("near_code", "TMFH6"),
        far_code=over.get("far_code", "TMFI6"),
        now_ts=over.get("now_ts"))


def test_stale_certificate_rejected():
    api = _FakeApi()
    cert, _ = _certify(api)
    old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    cert = LiveBrokerCertificate(**{**cert.__dict__, "captured_at": old})
    ok, reasons = _validated(cert, CertificateIssuer())
    assert not ok and any("STALE" in r for r in reasons)


def test_future_clock_skew_rejected():
    api = _FakeApi()
    cert, _ = _certify(api)
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    cert = LiveBrokerCertificate(**{**cert.__dict__, "captured_at": future})
    ok, reasons = _validated(cert, CertificateIssuer())
    assert not ok and any("SKEW" in r for r in reasons)


def test_different_process_start_rejected():
    api = _FakeApi()
    cert, _ = _certify(api)
    ok, reasons = _validated(cert, CertificateIssuer(), process_start_id="p-2")
    assert not ok and any("PROCESS" in r for r in reasons)


def test_different_account_rejected():
    api = _FakeApi()
    cert, _ = _certify(api)
    ok, reasons = _validated(cert, CertificateIssuer(), account_hash="deadbeef")
    assert not ok and any("ACCOUNT" in r for r in reasons)


def test_changed_contract_codes_rejected():
    api = _FakeApi()
    cert, _ = _certify(api)
    ok, reasons = _validated(cert, CertificateIssuer(), near_code="TMFJ6")
    assert not ok and any("CONTRACT" in r for r in reasons)


# ── B5: transition seam + PAPER rejection ─────────────────────────────────

def test_weak_legacy_preflight_alone_not_live_ready():
    from core.mode_transition import live_preflight_context, transition_to_live_ready
    ctx = live_preflight_context(account_id="A1")
    # legacy: weak startup preflight (no certificate) must NOT reach LIVE_READY
    ready = transition_to_live_ready(ctx, [])
    assert not ready.is_live_ready(), \
        "weak legacy preflight (no certificate) must stay non-LIVE_READY"


def test_paper_mode_rejects_even_valid_certificate():
    from core.mode_transition import paper_context
    api = _FakeApi()
    cert, failures = _certify(api)
    assert cert is not None
    with paper_context(account_id="A1") as ctx:
        assert not ctx.is_live_ready()
        with pytest.raises(Exception):
            ctx.assert_live_order_allowed()


# ── zero order/cancel/modify calls anywhere (B6) ───────────────────────────

def test_no_order_calls_in_full_certification_cycle():
    api = _FakeApi()
    cert, failures = _certify(api)
    ok, reasons = _validated(cert, CertificateIssuer())
    assert ok and reasons == []
    assert not any(c in ("place_order", "cancel_order", "modify_order")
                   for c in api.calls), api.calls


def test_query_failure_quarantines_zero_submit():
    api = _FakeApi()
    api.margin = lambda account: (_ for _ in ()).throw(RuntimeError("margin down"))
    cert, failures = certify_route(api, process_start_id="p-1",
                                   issuer=CertificateIssuer(),
                                   required_margin=200_000.0)
    assert cert is None
    assert any("MARGIN" in f for f in failures)
    assert not any(c in ("place_order", "cancel_order", "modify_order")
                   for c in api.calls)
