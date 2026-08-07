#!/usr/bin/env python3
"""RED tests v6: Live Route Certification — CORE phase (codex round-7 P0-1..5).

core.live_route_certificate does not exist yet → collection ImportError is the
expected RED. Monitor integration lives in test_live_route_monitor_integration.py
(explicit RED until the separately-reviewed wiring phase).

P0-1: SessionRegistry hooked at safe_login (the only success chokepoint) —
initial login, reconnect replacement, failed reconnect, logout all covered.
P0-2: weak-key per-live-object mapping + secrets opaque generation (no
id()+time.time()); id-reuse/object-replacement cannot validate an old cert.
P0-3: margin source bound to the effective config (path/sha256/commit/parsed
floor) from the TRUSTED loaded config.
P0-4: transition input is the internal immutable RuntimeCertificationContext
built ONLY by the trusted factory; forged dict/mapping rejected.
P0-5: split — this file is core-only and goes green with the core module.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.live_route_certificate import (   # noqa: F401 — RED: module TBD
    CertificateIssuer,
    LiveBrokerCertificate,
    RuntimeCertificationContext,
    SessionRegistry,
    build_runtime_certification_context,
    certify_route,
    is_authenticated_session,
    margin_source_from_config,
    register_session,
    session_registry,
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
    """Verified 1.7.0 surface only. Order methods raise (zero-order)."""

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


# ── trusted config fixture（P0-3 — effective config 快照）──────────────────

def _config(per_pair=100_000.0, commit="0123456789abcdef", path="/cfg/futures.yaml",
            sha="cfgsha256" * 4, products=("TMF",)):
    return {"path": path, "sha256": sha, "commit": commit,
            "per_pair_margin": per_pair, "products": products}


# ── helpers ────────────────────────────────────────────────────────────────

def _certify(api, *, config=None, **kw):
    config = config or _config()
    issuer = CertificateIssuer()
    cert, failures = certify_route(
        api, process_start_id="p-1", issuer=issuer,
        margin_source=margin_source_from_config(config), **kw)
    return cert, failures, issuer


def _ctx_runtime(api, cert=None, **over):
    """RuntimeCertificationContext via the trusted factory (P0-4)."""
    cfg = _config()
    facts = dict(config=cfg, process_state={"process_start_id": "p-1"})
    return build_runtime_certification_context(api, cfg, facts)


def _validated(cert, issuer, **over):
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
    assert issuer.peek(cert.nonce) is not None


# ── P0-1/P0-2 (v7): SessionRegistry — strong-registration map ──────────────

def test_registry_strong_ref_and_opaque_generation():
    reg = SessionRegistry()
    api = _FakeApi()
    g = reg.register(api)
    assert isinstance(g, str) and len(g) >= 32          # secrets opaque
    assert reg.generation(api) == g
    assert g != reg.register(api)                        # every login = new gen
    # strong ref: the entry holds the api object itself (id-reuse guard)
    assert reg._entries[id(api)].api is api


def test_registry_identity_mismatch_returns_none():
    # v7: a stale entry at the same id with a DIFFERENT object must fail the
    # identity check — no id-reuse can validate an old registration
    reg = SessionRegistry()
    api = _FakeApi()
    other = _FakeApi()
    reg._entries[id(api)] = type(reg)._entries[id(api)] = \
        __import__("core.live_route_certificate", fromlist=["_SessionEntry"]) \
        ._SessionEntry(api=other, generation="deadbeef" * 4, logged_in_at=0.0)
    assert reg.generation(api) is None, "identity mismatch must invalidate"


def test_unregister_removes_entry_and_generation():
    reg = SessionRegistry()
    api = _FakeApi()
    g = reg.register(api)
    assert reg.generation(api) == g
    reg.unregister(api)                                  # logout()
    assert reg.generation(api) is None


def test_safe_login_hook_semantics_invalidate_before_register():
    # v7 hook: unregister BEFORE each attempt; register ONLY after success
    api = _FakeApi()
    register_session(api)                                # initial success
    session_registry.unregister(api)                     # pre-attempt invalidate
    assert session_registry.generation(api) is None      # login attempt pending
    register_session(api)                                # attempt succeeded
    assert session_registry.generation(api) is not None


def test_failed_relogin_leaves_no_valid_generation():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    assert cert is not None
    session_registry.unregister(api)                     # pre-attempt invalidate
    # login FAILED → no register → no valid generation
    assert session_registry.generation(api) is None
    ok, reasons = _validated(cert, issuer)
    assert not ok and any("SESSION" in r for r in reasons), reasons


def test_reconnect_invalidates_cert_even_with_issuer_nonce():
    # round-8 #3: certify AND transition check the CURRENT registry
    # generation — a reconnect invalidates the old cert even though the
    # issuer nonce is still valid
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    assert cert is not None and failures == []
    assert issuer.peek(cert.nonce) is not None           # nonce still valid
    register_session(api)                                # reconnect → new gen
    ok, reasons = _validated(cert, issuer)
    assert not ok and any("SESSION" in r for r in reasons), reasons


def test_transition_checks_registry_generation():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    assert cert is not None
    register_session(api)                                # reconnect after certify
    result = transition_with_certificate(_transition_ctx(), cert, issuer,
                                         runtime=_ctx_runtime(api, cert))
    assert not result.is_live_ready()
    assert any("SESSION" in r for r in result.audit_reasons), result.audit_reasons


def test_registry_is_module_level_not_api_attribute():
    # round-8 #5: setattr is NOT available on the Rust api — the registry
    # must be module-level; no session state may live on the api object
    api = _FakeApi()
    register_session(api)
    assert not hasattr(api, "_session_generation"), \
        "registry state must not be stored on the api object"
    assert not hasattr(api, "_SessionEntry")


def test_initial_login_registers_and_authenticates():
    api = _FakeApi()
    register_session(api)                                # hook after safe_login
    assert is_authenticated_session(api)


def test_reconnect_replaces_generation_old_cert_invalid():
    api = _FakeApi()
    register_session(api)                                # initial login
    cert, failures, issuer = _certify(api)
    assert cert is not None and failures == []
    register_session(api)                                # successful reconnect
    assert session_registry.generation(api) != cert.session_generation
    ok, reasons = _validated(cert, issuer)
    assert not ok and any("SESSION" in r for r in reasons), reasons


def test_failed_reconnect_preserves_no_authorization():
    api = _FakeApi()
    register_session(api)
    session_registry.unregister(api)                     # reconnect failure cleanup
    assert not is_authenticated_session(api)
    assert session_registry.generation(api) is None


def test_logout_invalidation():
    api = _FakeApi()
    register_session(api)
    assert is_authenticated_session(api)
    session_registry.unregister(api)                     # logout()
    assert not is_authenticated_session(api)


def test_object_replacement_cannot_validate_old_cert():
    # P0-2/v7: after unregister + release, a NEW api object (even at a reused
    # id) cannot validate the old cert — the old generation is gone
    api_old = _FakeApi()
    register_session(api_old)
    cert, failures, issuer = _certify(api_old)
    assert cert is not None
    session_registry.unregister(api_old)                 # logout / release
    del api_old
    api_new = _FakeApi()                                 # replacement object
    register_session(api_new)                            # fresh login
    assert session_registry.generation(api_new) != cert.session_generation
    ok, reasons = _validated(cert, issuer)
    assert not ok and any("SESSION" in r for r in reasons), reasons


def test_auth_requires_futopt_account_and_live_query():
    api = _FakeApi(futopt_account=None)
    register_session(api)
    assert not is_authenticated_session(api)
    api2 = _FakeApi(accounts_ok=False)
    register_session(api2)
    assert not is_authenticated_session(api2)


def test_auth_exception_fails_closed():
    class _Broken:
        def __getattr__(self, name):
            raise RuntimeError("boom")
    assert not is_authenticated_session(_Broken())


# ── P0-3: margin source from trusted config ────────────────────────────────

def test_margin_source_from_config_deterministic():
    cfg = _config()
    src = margin_source_from_config(cfg)
    assert src["source"] == "CONFIG_FLOOR"
    assert src["per_pair_margin"] == 100_000.0
    assert src["config_path"] == cfg["path"]
    assert src["config_sha256"] == cfg["sha256"]
    assert src["config_commit"] == cfg["commit"]
    assert margin_source_from_config(cfg) == src


@pytest.mark.parametrize("floor", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_margin_source_malformed_config_fails(floor):
    with pytest.raises(Exception):
        margin_source_from_config(_config(per_pair=floor))


def test_margin_source_unknown_product_fails():
    with pytest.raises(Exception):
        margin_source_from_config(_config(products=("TMF",)), product="XXX")


def test_config_changed_after_cert_rejected():
    api = _FakeApi()
    register_session(api)
    cfg_a = _config(per_pair=100_000.0, sha="AAAA" * 8, commit="aaaa1111")
    cert, failures, issuer = _certify(api, config=cfg_a)
    assert cert is not None and failures == []
    _assert_nonce_ok(issuer, cert)
    cfg_b = _config(per_pair=100_000.0, sha="BBBB" * 8, commit="bbbb2222")
    ok, reasons = _validated(cert, issuer, margin_source=margin_source_from_config(cfg_b))
    assert not ok and any("SOURCE" in r for r in reasons), reasons


# ── P0-4: RuntimeCertificationContext — trusted factory only ───────────────

def test_factory_builds_context_from_trusted_config():
    api = _FakeApi()
    register_session(api)
    ctx = _ctx_runtime(api)
    assert isinstance(ctx, RuntimeCertificationContext)
    assert ctx.account_hash
    assert ctx.near_code == "TMFH6" and ctx.far_code == "TMFI6"
    assert ctx.margin_source["source"] == "CONFIG_FLOOR"
    assert ctx.session_generation == session_registry.generation(api)
    assert ctx.process_start_id == "p-1"


def test_transition_rejects_forged_dict_runtime():
    # P0-4: a plain dict/mapping is NOT a RuntimeCertificationContext → the
    # public route must reject it (no forgeable API shape)
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    assert cert is not None
    forged = {"account_hash": "x", "near_code": "TMFH6", "far_code": "TMFI6"}
    with pytest.raises(Exception):
        transition_with_certificate(_transition_ctx(), cert, issuer, runtime=forged)


def _transition_ctx():
    from core.mode_transition import live_preflight_context
    return live_preflight_context(account_id="A1")


# ── core route assertions ──────────────────────────────────────────────────

def test_transition_succeeds_and_consumes_exactly_once():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    rt = _ctx_runtime(api, cert)
    ready = transition_with_certificate(_transition_ctx(), cert, issuer, runtime=rt)
    assert ready.is_live_ready()
    assert issuer.peek(cert.nonce) is None
    with pytest.raises(Exception):
        transition_with_certificate(_transition_ctx(), cert, issuer, runtime=rt)


def test_transition_rejects_mutated_certificate_same_nonce():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    forged = LiveBrokerCertificate(**{**cert.__dict__, "near_code": "TMFJ6"})
    result = transition_with_certificate(_transition_ctx(), forged, issuer,
                                         runtime=_ctx_runtime(api, cert))
    assert not result.is_live_ready()
    assert result.to_dict().get("effective_mode") == "live_quarantined"
    assert any("CONTRACT" in r for r in result.audit_reasons)
    assert issuer.peek(cert.nonce) is not None


def test_transition_failed_returns_quarantined_with_audit_reason():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    rt = _ctx_runtime(api, cert)
    rt2 = build_runtime_certification_context(
        api, _config(per_pair=150_000.0), {"process_start_id": "p-1"})
    result = transition_with_certificate(_transition_ctx(), cert, issuer, runtime=rt2)
    assert result.to_dict().get("effective_mode") == "live_quarantined"
    assert result.audit_reasons, "quarantined context must carry audit reasons"


def test_transition_toctou_invalidation():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    ok, reasons = _validated(cert, issuer)
    assert ok and reasons == []
    issuer.invalidate_all()
    result = transition_with_certificate(_transition_ctx(), cert, issuer,
                                         runtime=_ctx_runtime(api, cert))
    assert not result.is_live_ready()


def test_transition_has_no_order_calls():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    ready = transition_with_certificate(_transition_ctx(), cert, issuer,
                                        runtime=_ctx_runtime(api, cert))
    assert ready.is_live_ready()
    assert not any(c in ("place_order", "cancel_order", "modify_order")
                   for c in api.calls), api.calls


# ── staleness / identity (same issuer, targeted reasons) ───────────────────

def test_stale_certificate_rejected():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    _assert_nonce_ok(issuer, cert)
    old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    ok, reasons = _validated(
        LiveBrokerCertificate(**{**cert.__dict__, "captured_at": old}), issuer)
    assert not ok
    assert "STALE" in reasons and "NONCE_UNKNOWN" not in reasons, reasons


def test_future_clock_skew_rejected():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    ok, reasons = _validated(
        LiveBrokerCertificate(**{**cert.__dict__, "captured_at": future}), issuer)
    assert not ok and "SKEW" in reasons


def test_different_account_rejected():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    ok, reasons = _validated(cert, issuer, account_hash="deadbeef")
    assert not ok and "ACCOUNT" in reasons and "NONCE_UNKNOWN" not in reasons


def test_forged_copied_json_with_matching_process_id_fails():
    issuer_a = CertificateIssuer()
    api_a = _FakeApi()
    register_session(api_a)
    cert_a, failures = certify_route(
        api_a, process_start_id="p-A", issuer=issuer_a,
        margin_source=margin_source_from_config(_config()))
    assert cert_a is not None and failures == []
    payload = json.loads(json.dumps(cert_a.__dict__, default=str))
    issuer_b = CertificateIssuer()
    ok, reasons = _validated(
        LiveBrokerCertificate(**payload), issuer_b, process_start_id="p-A")
    assert not ok and any("NONCE" in r for r in reasons)


def test_restart_no_reconstructed_issuer_validates_old_cert():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer_old = _certify(api)
    assert cert is not None
    persisted = json.dumps(issuer_old.__dict__, default=str)
    fresh = CertificateIssuer()
    assert fresh.__dict__ == {}
    ok, reasons = _validated(cert, fresh)
    assert not ok and any("NONCE" in r for r in reasons)
    assert persisted


# ── capacity / snapshot / open-orders / zero-order ─────────────────────────

@pytest.mark.parametrize("margin_val,expect_ok", [
    (None, False), (0.0, False), (109_999.0, False),
    (110_000.0, True), (1_000_000.0, True),
])
def test_margin_capacity_boundaries(margin_val, expect_ok):
    api = _FakeApi(margin_val=margin_val)
    register_session(api)
    cert, failures, _ = _certify(api)
    if expect_ok:
        assert cert is not None and failures == []
        assert cert.required_margin == 110_000.0      # 100k × 1.1 buffer
    else:
        assert cert is None and any("MARGIN" in f for f in failures), failures


@pytest.mark.parametrize("snapshot_codes", [
    (), ("TMFH6",), ("TMFH6", "TMFH6"), ("TMFX6", "TMFH6"),
])
def test_snapshot_presence_semantics_fail(snapshot_codes):
    api = _FakeApi(snapshot_codes=snapshot_codes)
    register_session(api)
    cert, failures, _ = _certify(api)
    assert cert is None and any("SNAPSHOT" in f for f in failures)


def test_open_orders_with_submitted_status_fail():
    api = _FakeApi(open_trade_statuses=["Submitted"])
    register_session(api)
    cert, failures, _ = _certify(api)
    assert cert is None and any("OPEN_ORDERS" in f for f in failures)


def test_all_terminal_trades_pass():
    api = _FakeApi(open_trade_statuses=["Filled", "Cancelled"])
    register_session(api)
    cert, failures, _ = _certify(api)
    assert cert is not None and failures == []


def test_broker_not_flat_fails():
    api = _FakeApi(flat=False)
    register_session(api)
    cert, failures, _ = _certify(api)
    assert cert is None and any("FLAT" in f for f in failures)


def test_unsubscribe_failure_is_fatal():
    api = _FakeApi(unsubscribe_ok=False)
    register_session(api)
    cert, failures, _ = _certify(api)
    assert cert is None and any("QUOTE" in f or "SUB" in f for f in failures)


def test_no_order_calls_in_full_certification_cycle():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    ok, reasons = _validated(cert, issuer)
    assert ok and reasons == []
    assert not any(c in ("place_order", "cancel_order", "modify_order")
                   for c in api.calls), api.calls
