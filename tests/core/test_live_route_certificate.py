#!/usr/bin/env python3
"""RED tests v3: Live Route Certification (codex round-4 L1-L7).

core.live_route_certificate does not exist yet → collection ImportError is
the expected RED for the certificate API; the transition-seam tests are RED
against the CURRENT core.mode_transition (weak preflight alone must not
reach LIVE_READY).

L1 no vacuity: every validation test uses the ISSUING issuer and asserts
nonce redemption succeeds before the targeted reason. L2 open-orders fake
models Submitted/Pending statuses. L3 deterministic required-margin provider
bound to the certificate. L4 explicit authenticated-session assertion.
L5 snapshot presence semantics (empty/missing/duplicate/subset fail).
L6 nonce lifecycle (peek/redeem/invalidate_all, reconnect). L7 genuine
transition boundary.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.live_route_certificate import (   # noqa: F401 — RED: module TBD
    CertificateIssuer,
    LiveBrokerCertificate,
    MARGIN_PROVIDER_VERSION,
    certify_route,
    required_margin_for,
    transition_with_certificate,
    validate_live_broker_certificate,
)


# ── adapter-faithful fake (B1, L2, L4) ─────────────────────────────────────

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
    """Faithful to the real preflight collection contract. Order methods
    raise (structural zero-order guarantee). L4: explicit authenticated
    session flag. L2: open trades carry explicit Submitted/Pending status."""

    def __init__(self, *, flat=True, open_trade_statuses=(),
                 margin_val=1_000_000.0, trading_limits_ok=True,
                 subscribe_ok=True, unsubscribe_ok=True,
                 snapshot_codes=("TMFH6", "TMFI6"), authenticated=True,
                 account=None):
        self.futopt_account = account or _Account()
        self.authenticated = authenticated
        self._contracts = [_Contract("TMFH6", "2026-08-19"),
                           _Contract("TMFI6", "2026-09-16")]
        self.Contracts = _Contracts(self._contracts)
        self._flat = flat
        self._open_trade_statuses = list(open_trade_statuses)
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
        # L2: return the trades with their REAL status — Submitted/Pending
        # are open; Filled/Cancelled/Expired/Done are terminal
        return [_Trade("TMFH6", 1, s) for s in self._open_trade_statuses]

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

    # ── forbidden in certification ──
    def place_order(self, *a, **k):
        raise RuntimeError("ORDER_CALL_FORBIDDEN")

    def cancel_order(self, *a, **k):
        raise RuntimeError("ORDER_CALL_FORBIDDEN")

    def modify_order(self, *a, **k):
        raise RuntimeError("ORDER_CALL_FORBIDDEN")


# ── L1: helpers keep the ISSUING issuer; nonce redemption asserted first ───

def _certify(api, *, margin_provider=None, **kw):
    issuer = CertificateIssuer()
    cert, failures = certify_route(
        api, process_start_id="p-1", issuer=issuer,
        margin_provider=margin_provider, **kw)
    return cert, failures, issuer


def _validated(cert, issuer, **over):
    ok, reasons = validate_live_broker_certificate(
        cert, issuer=issuer,
        process_start_id=over.get("process_start_id", "p-1"),
        account_hash=over.get("account_hash", cert.account_hash),
        near_code=over.get("near_code", "TMFH6"),
        far_code=over.get("far_code", "TMFI6"),
        now_ts=over.get("now_ts"),
        margin_provider=over.get("margin_provider"))
    return ok, reasons


def _assert_nonce_ok(issuer, cert):
    # L1: for normal same-issuer validation, redemption must succeed first
    assert issuer.peek(cert.nonce) is not None, "issuing issuer must know the nonce"


# ── B1/L4: certification collects through the real contract + auth ─────────

def test_certify_route_collects_through_real_preflight_surface():
    api = _FakeApi()
    cert, failures, _ = _certify(api)
    assert failures == [], failures
    for method in ("list_positions", "list_trades", "margin", "snapshots",
                   "subscribe", "unsubscribe"):
        assert method in api.calls, f"{method} was never called"
    assert cert.margin_available == 1_000_000.0
    assert cert.near_code == "TMFH6" and cert.far_code == "TMFI6"


def test_certificate_binds_identity_and_snapshots():
    api = _FakeApi()
    cert, failures, _ = _certify(api)
    assert cert.process_start_id == "p-1"
    assert cert.account_hash
    assert cert.position_snapshot_ts and cert.order_snapshot_ts
    assert "MARGIN_OK" in cert.query_results
    assert "BROKER_FLAT" in cert.query_results
    assert "NO_OPEN_ORDERS" in cert.query_results
    assert cert.bidask_subscribed == ("NEAR", "FAR")
    assert cert.bidask_unsubscribed == ("NEAR", "FAR")


def test_unauthenticated_session_fails():
    # L4: the certificate requires an explicit authenticated-session
    # assertion; a session that is not authenticated fails closed
    api = _FakeApi(authenticated=False)
    cert, failures, _ = _certify(api)
    assert cert is None
    assert any("AUTH" in f for f in failures), failures


# ── B3/L3: deterministic required-margin provider, bound to cert ───────────

def test_required_margin_provider_is_deterministic():
    prov = required_margin_for("TMF", per_lot_margin=100_000.0, buffer=0.1)
    assert prov["provider_version"] == MARGIN_PROVIDER_VERSION
    assert prov["required_margin"] == 220_000.0     # 2 × 100k × 1.1
    assert required_margin_for("TMF", per_lot_margin=100_000.0,
                               buffer=0.1) == prov  # deterministic


@pytest.mark.parametrize("margin_val,expect_ok", [
    (None, False),          # missing
    (0.0, False),           # zero
    (219_999.0, False),     # just below provider requirement (220_000)
    (220_000.0, True),      # exactly at requirement
    (1_000_000.0, True),    # comfortable
])
def test_margin_capacity_boundaries(margin_val, expect_ok):
    api = _FakeApi(margin_val=margin_val)
    cert, failures, _ = _certify(api, margin_provider=required_margin_for(
        "TMF", per_lot_margin=100_000.0, buffer=0.1))
    if expect_ok:
        assert cert is not None and failures == []
        assert cert.required_margin == 220_000.0
        assert cert.margin_provider_version == MARGIN_PROVIDER_VERSION
    else:
        assert cert is None and any("MARGIN" in f for f in failures), failures


def test_changed_margin_provider_rejected_at_validation():
    # L3: the certificate binds the provider version+value; if the current
    # provider differs (config changed), validation flags PROVIDER_MISMATCH
    api = _FakeApi(margin_val=1_000_000.0)
    prov_v1 = required_margin_for("TMF", per_lot_margin=100_000.0, buffer=0.1)
    cert, failures, issuer = _certify(api, margin_provider=prov_v1)
    assert cert is not None and failures == []
    _assert_nonce_ok(issuer, cert)

    prov_v2 = required_margin_for("TMF", per_lot_margin=100_000.0, buffer=0.2)
    ok, reasons = _validated(cert, issuer, margin_provider=prov_v2)
    assert not ok
    assert any("PROVIDER" in r for r in reasons), reasons


# ── B4/L5: subscription symmetry + snapshot presence semantics ─────────────

def test_unsubscribe_failure_is_fatal():
    api = _FakeApi(unsubscribe_ok=False)
    cert, failures, _ = _certify(api)
    assert cert is None
    assert any("QUOTE" in f or "SUB" in f for f in failures)


def test_subscribe_failure_is_fatal():
    api = _FakeApi(subscribe_ok=False)
    cert, failures, _ = _certify(api)
    assert cert is None
    assert any("QUOTE" in f or "SUB" in f for f in failures)


@pytest.mark.parametrize("snapshot_codes", [
    (),                                  # empty — L5: must fail (market-closed
    ("TMFH6",),                          # is not a documented exception yet)
    ("TMFH6", "TMFH6"),                  # duplicate
    ("TMFX6", "TMFH6"),                  # extra code
])
def test_snapshot_presence_semantics_fail(snapshot_codes):
    api = _FakeApi(snapshot_codes=snapshot_codes)
    cert, failures, _ = _certify(api)
    assert cert is None
    assert any("SNAPSHOT" in f for f in failures), failures


def test_broker_not_flat_fails():
    api = _FakeApi(flat=False)
    cert, failures, _ = _certify(api)
    assert cert is None and any("FLAT" in f for f in failures)


def test_open_orders_with_submitted_status_fail():
    # L2: Submitted/Pending trades are open — the fake returns their REAL
    # status and the certification must fail
    api = _FakeApi(open_trade_statuses=["Submitted"])
    cert, failures, _ = _certify(api)
    assert cert is None
    assert any("OPEN_ORDERS" in f for f in failures), failures


def test_all_terminal_trades_pass():
    # L2: terminal statuses (Filled/Cancelled/Expired/Done) are not open
    api = _FakeApi(open_trade_statuses=["Filled", "Cancelled"])
    cert, failures, _ = _certify(api)
    assert cert is not None and failures == []


# ── B2/B6/L6: session-bound nonce + lifecycle ──────────────────────────────

def test_forged_copied_json_with_matching_process_id_fails():
    issuer_a = CertificateIssuer()
    api_a = _FakeApi()
    cert_a, failures = certify_route(api_a, process_start_id="p-A", issuer=issuer_a)
    assert cert_a is not None and failures == []
    payload = json.loads(json.dumps(cert_a.__dict__, default=str))

    issuer_b = CertificateIssuer()
    forged = LiveBrokerCertificate(**payload)
    ok, reasons = _validated(forged, issuer_b, process_start_id="p-A")
    assert not ok
    assert any("NONCE" in r for r in reasons), reasons


def test_nonce_invalidation_on_session_renewal():
    # L6: reconnect/session renewal invalidates all issued nonces
    api = _FakeApi()
    cert, failures, issuer = _certify(api)
    assert cert is not None
    _assert_nonce_ok(issuer, cert)
    issuer.invalidate_all()          # session renewal / shutdown
    ok, reasons = _validated(cert, issuer)
    assert not ok and any("NONCE" in r for r in reasons), reasons


def test_nonce_single_use_redeem_consumes():
    # L6: redeem is consuming (transition uses it once); peek is not
    api = _FakeApi()
    cert, failures, issuer = _certify(api)
    assert issuer.peek(cert.nonce) is not None
    consumed = issuer.redeem(cert.nonce)
    assert consumed is not None
    assert issuer.peek(cert.nonce) is None, "redeem must consume the nonce"


def test_reconnect_new_issuer_rejects_old_certificate():
    api = _FakeApi()
    cert, failures, issuer_old = _certify(api)
    assert cert is not None
    issuer_new = CertificateIssuer()   # process reconnected → fresh issuer
    ok, reasons = _validated(cert, issuer_new)
    assert not ok and any("NONCE" in r for r in reasons)


def test_no_order_calls_in_full_certification_cycle():
    api = _FakeApi()
    cert, failures, issuer = _certify(api)
    ok, reasons = _validated(cert, issuer)
    assert ok and reasons == []
    assert not any(c in ("place_order", "cancel_order", "modify_order")
                   for c in api.calls), api.calls


def test_query_failure_quarantines_zero_submit():
    api = _FakeApi()
    api.margin = lambda account: (_ for _ in ()).throw(RuntimeError("margin down"))
    cert, failures, _ = _certify(api)
    assert cert is None
    assert any("MARGIN" in f for f in failures)
    assert not any(c in ("place_order", "cancel_order", "modify_order")
                   for c in api.calls)


# ── L1: staleness / skew / identity — SAME issuer, targeted reasons ────────

def _mutated(cert, **over):
    return LiveBrokerCertificate(**{**cert.__dict__, **over})


def test_stale_certificate_rejected():
    api = _FakeApi()
    cert, failures, issuer = _certify(api)
    _assert_nonce_ok(issuer, cert)
    old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    ok, reasons = _validated(_mutated(cert, captured_at=old), issuer)
    assert not ok
    assert "STALE" in reasons and "NONCE_UNKNOWN" not in reasons, reasons


def test_future_clock_skew_rejected():
    api = _FakeApi()
    cert, failures, issuer = _certify(api)
    _assert_nonce_ok(issuer, cert)
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    ok, reasons = _validated(_mutated(cert, captured_at=future), issuer)
    assert not ok
    assert "SKEW" in reasons and "NONCE_UNKNOWN" not in reasons, reasons


def test_different_process_start_rejected():
    api = _FakeApi()
    cert, failures, issuer = _certify(api)
    _assert_nonce_ok(issuer, cert)
    ok, reasons = _validated(cert, issuer, process_start_id="p-2")
    assert not ok
    assert "PROCESS" in reasons and "NONCE_UNKNOWN" not in reasons, reasons


def test_different_account_rejected():
    api = _FakeApi()
    cert, failures, issuer = _certify(api)
    _assert_nonce_ok(issuer, cert)
    ok, reasons = _validated(cert, issuer, account_hash="deadbeef")
    assert not ok
    assert "ACCOUNT" in reasons and "NONCE_UNKNOWN" not in reasons, reasons


def test_changed_contract_codes_rejected():
    api = _FakeApi()
    cert, failures, issuer = _certify(api)
    _assert_nonce_ok(issuer, cert)
    ok, reasons = _validated(cert, issuer, near_code="TMFJ6")
    assert not ok
    assert "CONTRACT" in reasons and "NONCE_UNKNOWN" not in reasons, reasons


# ── L7: genuine route boundary ─────────────────────────────────────────────

def test_weak_legacy_preflight_alone_not_live_ready():
    from core.mode_transition import live_preflight_context, transition_to_live_ready
    ctx = live_preflight_context(account_id="A1")
    # RED vs CURRENT code: weak legacy preflight (no certificate) must NOT
    # reach LIVE_READY — today transition_to_live_ready(ctx, []) does
    ready = transition_to_live_ready(ctx, [])
    assert not ready.is_live_ready(), \
        "weak legacy preflight (no certificate) must stay non-LIVE_READY"


def test_transition_requires_same_issuer_certificate():
    # L7: the ONLY allowed future transition path takes a valid certificate
    # issued by the SAME in-process issuer; this function does not exist yet
    # (RED). The copied-certificate case must be rejected by the function.
    from core.mode_transition import live_preflight_context
    ctx = live_preflight_context(account_id="A1")
    api = _FakeApi()
    cert, failures, issuer = _certify(api)
    assert cert is not None and failures == []
    ready = transition_with_certificate(ctx, cert, issuer)
    assert ready.is_live_ready()

    # a certificate issued by ANOTHER issuer must be rejected
    foreign = CertificateIssuer()
    with pytest.raises(Exception):
        transition_with_certificate(ctx, cert, foreign)
