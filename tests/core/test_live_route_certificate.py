#!/usr/bin/env python3
"""RED tests v5: Live Route Certification (codex round-6 — capability-map based).

core.live_route_certificate does not exist yet → collection ImportError is the
expected RED for the certificate API. The fake implements the VERIFIED
Shioaji 1.7.0 surface (see .planning/shioaji_capability_map.md): futopt_account
/ list_accounts() / margin(account) — login_token, account, authenticated,
margin_rates are VERIFIED ABSENT and must not appear.

P0-auth: authentication = futopt_account valid AND list_accounts() live
non-empty AND session epoch registered by the login wrapper.
P0-margin: margin_source_for = CONFIG_FLOOR only (no broker margin-rate
surface exists); account capacity from api.margin(account).
Route assertions: issuer binds canonical facts + session epoch; transition
rejects mutated certs (same nonce/issuer) by re-deriving facts from the
in-process runtime context; failed transition RETURNS an explicit
LIVE_QUARANTINED ctx with audit reason; restart/reconstructed issuer can
never validate an old cert; AST call-site scan for the legacy bypass.
"""

import ast
import json
from datetime import datetime, timedelta, timezone

import pytest

from core.live_route_certificate import (   # noqa: F401 — RED: module TBD
    SESSION_EPOCH_BY_API,
    CertificateIssuer,
    LiveBrokerCertificate,
    MARGIN_SOURCE_VERSION,
    certify_route,
    is_authenticated_session,
    margin_source_for,
    register_session_epoch,
    transition_with_certificate,
    validate_live_broker_certificate,
)


# ── adapter-faithful fake (verified Shioaji 1.7.0 surface) ─────────────────

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
    """Verified 1.7.0 surface only. No login_token/account/authenticated/
    margin_rates (verified absent). Order methods raise (zero-order)."""

    def __init__(self, *, futopt_account=None, accounts_ok=True,
                 flat=True, open_trade_statuses=(), margin_val=1_000_000.0,
                 trading_limits_ok=True, subscribe_ok=True, unsubscribe_ok=True,
                 snapshot_codes=("TMFH6", "TMFI6")):
        self.futopt_account = futopt_account if futopt_account is not None \
            else _Account()
        self._accounts_ok = accounts_ok
        self._flat = flat
        self._open_trade_statuses = list(open_trade_statuses)
        self._margin_val = margin_val
        self._trading_limits_ok = trading_limits_ok
        self._subscribe_ok = subscribe_ok
        self._unsubscribe_ok = unsubscribe_ok
        self._snapshot_codes = list(snapshot_codes)
        self._contracts = [_Contract("TMFH6", "2026-08-19"),
                           _Contract("TMFI6", "2026-09-16")]
        self.Contracts = _Contracts(self._contracts)
        self.calls = []

    def list_accounts(self):
        self.calls.append("list_accounts")
        if not self._accounts_ok:
            raise RuntimeError("list_accounts unavailable")
        return [self.futopt_account]

    def list_positions(self, account):
        self.calls.append("list_positions")
        return [] if self._flat else [_Position("TMFH6", 1)]

    def list_trades(self, *args):
        self.calls.append("list_trades")
        return [_Trade("TMFH6", 1, s) for s in self._open_trade_statuses]

    def margin(self, account):
        self.calls.append("margin")
        if self._margin_val is None:
            return None
        return _Margin(self._margin_val)

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


# ── helpers: issuing issuer + runtime facts ────────────────────────────────

def _certify(api, *, config_floor=100_000.0, **kw):
    issuer = CertificateIssuer()
    cert, failures = certify_route(
        api, process_start_id="p-1", issuer=issuer,
        margin_source=margin_source_for(api, "TMF", config_floor=config_floor),
        **kw)
    return cert, failures, issuer


def _runtime(api, cert=None, issuer=None, **over):
    """In-process runtime facts the transition derives expectations from."""
    facts = dict(
        process_start_id="p-1",
        account_hash=cert.account_hash if cert else "acc-hash",
        near_code="TMFH6", far_code="TMFI6",
        margin_source=margin_source_for(api, "TMF", config_floor=100_000.0),
        session_epoch=SESSION_EPOCH_BY_API.get(id(api), 0.0),
        now_ts=datetime.now(timezone.utc).isoformat(),
    )
    facts.update(over)
    return facts


def _validated(cert, issuer, *, api=None, **over):
    ok, reasons = validate_live_broker_certificate(
        cert, issuer=issuer,
        process_start_id=over.get("process_start_id", "p-1"),
        account_hash=over.get("account_hash", cert.account_hash),
        near_code=over.get("near_code", "TMFH6"),
        far_code=over.get("far_code", "TMFI6"),
        now_ts=over.get("now_ts"),
        margin_source=over.get("margin_source"))
    return ok, reasons


def _assert_nonce_ok(issuer, cert):
    assert issuer.peek(cert.nonce) is not None, "issuing issuer must know the nonce"


# ── P0-auth: verified-session adapter (capability map §4) ──────────────────

def test_auth_requires_all_three_evidence():
    api = _FakeApi()
    register_session_epoch(api)                  # login wrapper stamps epoch
    assert is_authenticated_session(api)


def test_auth_no_futopt_account_fails():
    api = _FakeApi(futopt_account=None)
    register_session_epoch(api)
    assert not is_authenticated_session(api)


def test_auth_list_accounts_unavailable_fails():
    api = _FakeApi(accounts_ok=False)
    register_session_epoch(api)
    assert not is_authenticated_session(api)


def test_auth_unregistered_session_fails():
    api = _FakeApi()                             # no epoch stamped (not logged in)
    assert not is_authenticated_session(api)


def test_auth_exception_fails_closed():
    class _Broken:
        def __getattr__(self, name):
            raise RuntimeError("boom")
    assert not is_authenticated_session(_Broken())


def test_certify_requires_authenticated_session():
    api = _FakeApi()                             # no epoch → unauthenticated
    cert, failures, _ = _certify(api)
    assert cert is None
    assert any("AUTH" in f for f in failures), failures


# ── P0-margin: CONFIG_FLOOR only, bound to config commit ───────────────────

def test_margin_source_config_floor_deterministic():
    api = _FakeApi()
    src = margin_source_for(api, "TMF", config_floor=100_000.0)
    assert src["source"] == "CONFIG_FLOOR"
    assert src["per_pair_margin"] == 100_000.0
    assert src["version"] == MARGIN_SOURCE_VERSION
    assert isinstance(src["config_commit"], str) and len(src["config_commit"]) >= 7
    assert margin_source_for(api, "TMF", config_floor=100_000.0) == src


@pytest.mark.parametrize("floor", [0.0, -1.0, float("nan"), float("inf")])
def test_margin_source_invalid_floor_fails(floor):
    with pytest.raises(Exception):
        margin_source_for(_FakeApi(), "TMF", config_floor=floor)


def test_margin_source_missing_floor_fails():
    with pytest.raises(Exception):
        margin_source_for(_FakeApi(), "TMF", config_floor=None)


def test_margin_source_unknown_product_fails():
    with pytest.raises(Exception):
        margin_source_for(_FakeApi(), "XXX")


def test_margin_source_config_commit_mismatch_rejected_at_validation():
    api = _FakeApi()
    cert, failures, issuer = _certify(api)
    assert cert is not None and failures == []
    _assert_nonce_ok(issuer, cert)
    changed = margin_source_for(api, "TMF", config_floor=150_000.0)  # floor changed
    ok, reasons = _validated(cert, issuer, margin_source=changed)
    assert not ok
    assert any("SOURCE" in r for r in reasons), reasons


# ── capacity boundaries (account margin from api.margin) ───────────────────

@pytest.mark.parametrize("margin_val,expect_ok", [
    (None, False), (0.0, False), (109_999.0, False),
    (110_000.0, True), (1_000_000.0, True),
])
def test_margin_capacity_boundaries(margin_val, expect_ok):
    api = _FakeApi(margin_val=margin_val)
    register_session_epoch(api)
    cert, failures, _ = _certify(api, config_floor=100_000.0)
    if expect_ok:
        assert cert is not None and failures == []
        assert cert.required_margin == 110_000.0      # 100k × 1.1 buffer
    else:
        assert cert is None and any("MARGIN" in f for f in failures), failures


# ── B4/L5: subscription symmetry + snapshot presence ───────────────────────

def test_unsubscribe_failure_is_fatal():
    api = _FakeApi(unsubscribe_ok=False)
    register_session_epoch(api)
    cert, failures, _ = _certify(api)
    assert cert is None
    assert any("QUOTE" in f or "SUB" in f for f in failures)


def test_subscribe_failure_is_fatal():
    api = _FakeApi(subscribe_ok=False)
    register_session_epoch(api)
    cert, failures, _ = _certify(api)
    assert cert is None
    assert any("QUOTE" in f or "SUB" in f for f in failures)


@pytest.mark.parametrize("snapshot_codes", [
    (), ("TMFH6",), ("TMFH6", "TMFH6"), ("TMFX6", "TMFH6"),
])
def test_snapshot_presence_semantics_fail(snapshot_codes):
    api = _FakeApi(snapshot_codes=snapshot_codes)
    register_session_epoch(api)
    cert, failures, _ = _certify(api)
    assert cert is None
    assert any("SNAPSHOT" in f for f in failures), failures


def test_broker_not_flat_fails():
    api = _FakeApi(flat=False)
    register_session_epoch(api)
    cert, failures, _ = _certify(api)
    assert cert is None and any("FLAT" in f for f in failures)


def test_open_orders_with_submitted_status_fail():
    api = _FakeApi(open_trade_statuses=["Submitted"])
    register_session_epoch(api)
    cert, failures, _ = _certify(api)
    assert cert is None
    assert any("OPEN_ORDERS" in f for f in failures), failures


def test_all_terminal_trades_pass():
    api = _FakeApi(open_trade_statuses=["Filled", "Cancelled"])
    register_session_epoch(api)
    cert, failures, _ = _certify(api)
    assert cert is not None and failures == []


def test_certificate_binds_identity_and_snapshots():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, _ = _certify(api)
    assert cert.process_start_id == "p-1"
    assert cert.account_hash
    assert cert.session_epoch > 0
    assert cert.margin_source == "CONFIG_FLOOR"
    assert cert.config_commit
    assert cert.position_snapshot_ts and cert.order_snapshot_ts
    assert cert.bidask_subscribed == ("NEAR", "FAR")
    assert cert.bidask_unsubscribed == ("NEAR", "FAR")


def test_certify_route_collects_through_real_preflight_surface():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, _ = _certify(api)
    assert failures == [], failures
    for method in ("list_positions", "list_trades", "margin", "snapshots",
                   "subscribe", "unsubscribe", "list_accounts"):
        assert method in api.calls, f"{method} was never called"
    assert cert.near_code == "TMFH6" and cert.far_code == "TMFI6"


# ── B2/B6/L6: unforgeable issuance + lifecycle ─────────────────────────────

def test_forged_copied_json_with_matching_process_id_fails():
    issuer_a = CertificateIssuer()
    api_a = _FakeApi()
    register_session_epoch(api_a)
    cert_a, failures = certify_route(
        api_a, process_start_id="p-A", issuer=issuer_a,
        margin_source=margin_source_for(api_a, "TMF", config_floor=100_000.0))
    assert cert_a is not None and failures == []
    payload = json.loads(json.dumps(cert_a.__dict__, default=str))

    issuer_b = CertificateIssuer()
    forged = LiveBrokerCertificate(**payload)
    ok, reasons = _validated(forged, issuer_b, process_start_id="p-A")
    assert not ok
    assert any("NONCE" in r for r in reasons), reasons


def test_nonce_invalidation_on_session_renewal():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer = _certify(api)
    assert cert is not None
    _assert_nonce_ok(issuer, cert)
    issuer.invalidate_all()
    ok, reasons = _validated(cert, issuer)
    assert not ok and any("NONCE" in r for r in reasons), reasons


def test_nonce_single_use_redeem_consumes():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer = _certify(api)
    assert issuer.peek(cert.nonce) is not None
    consumed = issuer.redeem(cert.nonce)
    assert consumed is not None
    assert issuer.peek(cert.nonce) is None


def test_reconnect_new_issuer_rejects_old_certificate():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer_old = _certify(api)
    assert cert is not None
    issuer_new = CertificateIssuer()
    ok, reasons = _validated(cert, issuer_new)
    assert not ok and any("NONCE" in r for r in reasons)


def test_restart_no_reconstructed_issuer_can_validate_old_cert():
    # v5: a fresh process (fresh issuer) + any persisted bytes can never
    # recreate the issuance state; the issuer constructor takes no state
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer_old = _certify(api)
    assert cert is not None
    persisted = json.dumps(issuer_old.__dict__, default=str)  # attacker snapshot
    fresh = CertificateIssuer()                               # restart
    assert fresh.__dict__ == {}                                # no restored state
    ok, reasons = _validated(cert, fresh)
    assert not ok and any("NONCE" in r for r in reasons)
    assert persisted  # snapshot bytes exist but cannot restore authorization


def test_no_order_calls_in_full_certification_cycle():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer = _certify(api)
    ok, reasons = _validated(cert, issuer)
    assert ok and reasons == []
    assert not any(c in ("place_order", "cancel_order", "modify_order")
                   for c in api.calls), api.calls


def test_query_failure_quarantines_zero_submit():
    api = _FakeApi()
    register_session_epoch(api)
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
    register_session_epoch(api)
    cert, failures, issuer = _certify(api)
    _assert_nonce_ok(issuer, cert)
    old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    ok, reasons = _validated(_mutated(cert, captured_at=old), issuer)
    assert not ok
    assert "STALE" in reasons and "NONCE_UNKNOWN" not in reasons, reasons


def test_future_clock_skew_rejected():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer = _certify(api)
    _assert_nonce_ok(issuer, cert)
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    ok, reasons = _validated(_mutated(cert, captured_at=future), issuer)
    assert not ok
    assert "SKEW" in reasons and "NONCE_UNKNOWN" not in reasons, reasons


def test_different_process_start_rejected():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer = _certify(api)
    _assert_nonce_ok(issuer, cert)
    ok, reasons = _validated(cert, issuer, process_start_id="p-2")
    assert not ok
    assert "PROCESS" in reasons and "NONCE_UNKNOWN" not in reasons, reasons


def test_different_account_rejected():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer = _certify(api)
    _assert_nonce_ok(issuer, cert)
    ok, reasons = _validated(cert, issuer, account_hash="deadbeef")
    assert not ok
    assert "ACCOUNT" in reasons and "NONCE_UNKNOWN" not in reasons, reasons


def test_changed_contract_codes_rejected():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer = _certify(api)
    _assert_nonce_ok(issuer, cert)
    ok, reasons = _validated(cert, issuer, near_code="TMFJ6")
    assert not ok
    assert "CONTRACT" in reasons and "NONCE_UNKNOWN" not in reasons, reasons


# ── v5 route assertions: transition (runtime-derived facts, quarantine) ────

def _transition_ctx():
    from core.mode_transition import live_preflight_context
    return live_preflight_context(account_id="A1")


def test_transition_succeeds_and_consumes_exactly_once():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer = _certify(api)
    rt = _runtime(api, cert, issuer)
    ready = transition_with_certificate(_transition_ctx(), cert, issuer, runtime=rt)
    assert ready.is_live_ready()
    assert issuer.peek(cert.nonce) is None, "successful transition consumes once"
    with pytest.raises(Exception):
        transition_with_certificate(_transition_ctx(), cert, issuer, runtime=rt)


def test_transition_rejects_mutated_certificate_same_nonce():
    # v5: same nonce/issuer but mutated facts → rejected (facts re-derived
    # from the runtime context, not the certificate's claims)
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer = _certify(api)
    _assert_nonce_ok(issuer, cert)
    forged = _mutated(cert, near_code="TMFJ6")       # mutated contract code
    result = transition_with_certificate(_transition_ctx(), forged, issuer,
                                         runtime=_runtime(api, cert, issuer))
    assert not result.is_live_ready()
    assert result.to_dict().get("effective_mode") == "live_quarantined"
    assert any("CONTRACT" in r for r in result.audit_reasons)
    assert issuer.peek(cert.nonce) is not None, "failed transition preserves nonce"


def test_transition_derives_facts_from_runtime_not_certificate():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer = _certify(api)
    # runtime says the account is different — cert must be rejected even
    # though the cert itself claims the old (correct-at-issue) hash
    rt = _runtime(api, cert, issuer, account_hash="runtime-account-2")
    result = transition_with_certificate(_transition_ctx(), cert, issuer, runtime=rt)
    assert not result.is_live_ready()
    assert any("ACCOUNT" in r for r in result.audit_reasons)


def test_transition_failed_returns_quarantined_with_audit_reason():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer = _certify(api)
    rt = _runtime(api, cert, issuer)
    rt["margin_source"] = margin_source_for(api, "TMF", config_floor=150_000.0)
    result = transition_with_certificate(_transition_ctx(), cert, issuer, runtime=rt)
    assert result.to_dict().get("effective_mode") == "live_quarantined"
    assert result.audit_reasons, "quarantined context must carry audit reasons"
    assert not result.is_live_ready()


def test_transition_toctou_invalidation_between_validate_and_transition():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer = _certify(api)
    ok, reasons = _validated(cert, issuer)
    assert ok and reasons == []
    issuer.invalidate_all()
    result = transition_with_certificate(_transition_ctx(), cert, issuer,
                                         runtime=_runtime(api, cert, issuer))
    assert not result.is_live_ready()
    assert result.to_dict().get("effective_mode") == "live_quarantined"


def test_transition_has_no_order_calls():
    api = _FakeApi()
    register_session_epoch(api)
    cert, failures, issuer = _certify(api)
    ready = transition_with_certificate(_transition_ctx(), cert, issuer,
                                        runtime=_runtime(api, cert, issuer))
    assert ready.is_live_ready()
    assert not any(c in ("place_order", "cancel_order", "modify_order")
                   for c in api.calls), api.calls


# ── B2: legacy bypass — AST call-site scan ─────────────────────────────────

def test_legacy_transition_no_cert_quarantines():
    from core.mode_transition import (live_preflight_context,
                                      transition_to_live_ready)
    ctx = live_preflight_context(account_id="A1")
    ready = transition_to_live_ready(ctx, [])     # no certificate
    assert not ready.is_live_ready(), "legacy no-cert transition must not authorize"
    assert ready.to_dict().get("effective_mode") == "live_quarantined", \
        "no-cert result must be LIVE_QUARANTINED"


def test_ast_call_site_scan_no_legacy_transition_in_monitor():
    # v5: AST-based scan — the production monitor module must not contain a
    # transition_to_live_ready call (known bypass: monitor.py:522)
    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    monitor = root / "strategies" / "futures" / "monitor.py"
    assert monitor.exists(), f"monitor not found: {monitor}"
    tree = ast.parse(monitor.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "transition_to_live_ready":
            hits.append(node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "transition_to_live_ready":
            hits.append(node.lineno)
    assert not hits, f"monitor.py still calls transition_to_live_ready at: {hits}"
