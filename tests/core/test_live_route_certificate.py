#!/usr/bin/env python3
"""RED/GREEN tests: Live Route Certification — CORE (round-9 corrective).

Round-9 P0 fixes covered:
1) logout invalidation (unregister_session) invalidates certs + transition
2) is_authenticated_session verifies the futures account is REPRESENTED in
   list_accounts (stable identity) — mismatched list rejected
3) margin source is SEALED from the actual config bytes (path/sha256/commit/
   product/floor bound; forged metadata cannot be injected)
4) certify_route accepts ONLY a trusted loader path (config_path+release_sha)
   — a margin_source kwarg is rejected (TypeError)
5) quote checks require exactly two unique passed codes == {near, far}
6) registry unregister is identity-aware — removing a non-owner api does not
   clear the current generation
7) transition compares the supplied certificate to the issuer's CANONICAL
   issued certificate — any tampering with the same nonce → CERT_TAMPERED
8) safe_login hook behavior (separate test file: test_safe_login_session_hook)
"""

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.live_route_certificate import (   # noqa: F401
    CertificateIssuer,
    LiveBrokerCertificate,
    RuntimeCertificationContext,
    SealedMarginSource,
    SessionRegistry,
    build_runtime_certification_context,
    certify_route,
    is_authenticated_session,
    load_trusted_margin_source,
    parse_margin_config,
    register_session,
    session_registry,
    transition_with_certificate,
    unregister_session,
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


_MISSING = object()          # sentinel: honor an explicit None futopt_account


class _FakeApi:
    """Verified 1.7.0 surface only. Order methods raise (zero-order)."""

    def __init__(self, *, futopt_account=_MISSING, accounts_ok=True,
                 list_accounts_accounts=_MISSING, flat=True,
                 open_trade_statuses=(), margin_val=1_000_000.0,
                 trading_limits_ok=True, subscribe_ok=True, unsubscribe_ok=True,
                 snapshot_codes=("TMFH6", "TMFI6")):
        self.futopt_account = _Account() if futopt_account is _MISSING \
            else futopt_account
        self._accounts_ok = accounts_ok
        self._list_accounts_accounts = list_accounts_accounts
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
        if self._list_accounts_accounts is not _MISSING:
            return list(self._list_accounts_accounts)
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


# ── trusted config (round-9 #3: sealed from ACTUAL bytes) ──────────────────

CONFIG_YAML = "mts:\n  live_required_margin_per_pair: 100000.0\n"
RELEASE_SHA = "0123456789abcdef"

_SHARED_CFG_DIR = tempfile.mkdtemp(prefix="lrc_cfg_")
_SHARED_CFG = Path(_SHARED_CFG_DIR) / "futures.yaml"
_SHARED_CFG.write_text(CONFIG_YAML, encoding="utf-8")


def _write_config(tmp_path, yaml_text=CONFIG_YAML, name="futures.yaml"):
    p = tmp_path / name
    p.write_text(yaml_text, encoding="utf-8")
    return p


def _sealed(cfg_path, release_sha=RELEASE_SHA):
    return load_trusted_margin_source(cfg_path, release_sha=release_sha)


# ── helpers ────────────────────────────────────────────────────────────────

def _certify(api, *, config_path=None, release_sha=RELEASE_SHA, **kw):
    cfg = config_path or _SHARED_CFG
    issuer = CertificateIssuer()
    cert, failures = certify_route(
        api, process_start_id="p-1", issuer=issuer,
        config_path=cfg, release_sha=release_sha, **kw)
    return cert, failures, issuer


def _ctx_runtime(api, cert=None, config_path=None, release_sha=RELEASE_SHA):
    cfg = config_path or _SHARED_CFG
    facts = dict(config=cfg, process_state={"process_start_id": "p-1",
                                            "release_sha": release_sha})
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


def _transition_ctx():
    from core.mode_transition import live_preflight_context
    return live_preflight_context(account_id="A1")


# ── registry: strong refs, identity, lifecycle (round-9 #6) ────────────────

def test_registry_strong_ref_and_opaque_generation():
    reg = SessionRegistry()
    api = _FakeApi()
    g = reg.register(api)
    assert isinstance(g, str) and len(g) >= 32
    assert reg.generation(api) == g
    assert g != reg.register(api)
    assert reg._entries[id(api)].api is api


def test_registry_identity_mismatch_returns_none():
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
    reg.unregister(api)
    assert reg.generation(api) is None


def test_unregister_other_api_preserves_current_generation():
    # round-9 #6: identity-aware removal — a non-owner api must not clear
    # the live session's generation
    a, b = _FakeApi(), _FakeApi()
    session_registry.register(a)
    session_registry.register(b)
    gb = session_registry.current_generation()
    session_registry.unregister(a)                 # a does NOT own current
    assert session_registry.current_generation() == gb
    session_registry.unregister(b)                 # owner removed → cleared
    assert session_registry.current_generation() is None


def test_safe_login_hook_semantics_invalidate_before_register():
    api = _FakeApi()
    register_session(api)
    session_registry.unregister(api)
    assert session_registry.generation(api) is None
    register_session(api)
    assert session_registry.generation(api) is not None


def test_failed_relogin_leaves_no_valid_generation():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    assert cert is not None
    session_registry.unregister(api)
    assert session_registry.generation(api) is None
    ok, reasons = _validated(cert, issuer)
    assert not ok and any("SESSION" in r for r in reasons), reasons


def test_reconnect_invalidates_cert_even_with_issuer_nonce():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    assert cert is not None and failures == []
    assert issuer.peek(cert.nonce) is not None
    register_session(api)
    ok, reasons = _validated(cert, issuer)
    assert not ok and any("SESSION" in r for r in reasons), reasons


def test_transition_checks_registry_generation():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    assert cert is not None
    register_session(api)
    result = transition_with_certificate(_transition_ctx(), cert, issuer,
                                         runtime=_ctx_runtime(api, cert))
    assert not result.is_live_ready()
    assert any("SESSION" in r for r in result.audit_reasons), result.audit_reasons


def test_registry_is_module_level_not_api_attribute():
    api = _FakeApi()
    register_session(api)
    assert not hasattr(api, "_session_generation")
    assert not hasattr(api, "_SessionEntry")


def test_initial_login_registers_and_authenticates():
    api = _FakeApi()
    register_session(api)
    assert is_authenticated_session(api)


def test_object_replacement_cannot_validate_old_cert():
    api_old = _FakeApi()
    register_session(api_old)
    cert, failures, issuer = _certify(api_old)
    assert cert is not None
    session_registry.unregister(api_old)
    del api_old
    api_new = _FakeApi()
    register_session(api_new)
    assert session_registry.generation(api_new) != cert.session_generation
    ok, reasons = _validated(cert, issuer)
    assert not ok and any("SESSION" in r for r in reasons), reasons


# ── round-9 #1: logout invalidation ────────────────────────────────────────

def test_logout_invalidates_cert_and_transition_quarantines():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    assert cert is not None
    unregister_session(api)                          # logout hook
    assert session_registry.generation(api) is None
    result = transition_with_certificate(_transition_ctx(), cert, issuer,
                                         runtime=_ctx_runtime(api, cert))
    assert not result.is_live_ready()
    assert any("SESSION" in r for r in result.audit_reasons), result.audit_reasons


def test_unregister_session_is_public_hook():
    from core.live_route_certificate import unregister_session as hook
    api = _FakeApi()
    register_session(api)
    hook(api)
    assert session_registry.generation(api) is None


# ── round-9 #2: auth verifies the futures account identity ────────────────

def test_auth_requires_futopt_account_and_live_query():
    api = _FakeApi(futopt_account=None)
    register_session(api)
    assert not is_authenticated_session(api)
    api2 = _FakeApi(accounts_ok=False)
    register_session(api2)
    assert not is_authenticated_session(api2)


def test_auth_mismatched_listed_account_rejected():
    # round-9 #2: list_accounts returning a DIFFERENT account (non-empty)
    # must not authenticate the futures session
    api = _FakeApi(list_accounts_accounts=[_Account("P2", "B2", "A2")])
    register_session(api)
    assert not is_authenticated_session(api)
    cert, failures, _ = _certify(api)
    assert cert is None and any("AUTH" in f for f in failures), failures


def test_auth_exception_fails_closed():
    class _Broken:
        def __getattr__(self, name):
            raise RuntimeError("boom")
    assert not is_authenticated_session(_Broken())


def test_certify_requires_authenticated_session():
    api = _FakeApi()
    cert, failures, _ = _certify(api)                # no registered session
    assert cert is None
    assert any("AUTH" in f for f in failures), failures


# ── round-9 #3/#4: sealed margin source from trusted config bytes ──────────

def test_margin_source_sealed_from_actual_bytes():
    raw = CONFIG_YAML.encode("utf-8")
    src = parse_margin_config(raw, config_path=str(_SHARED_CFG),
                              release_sha=RELEASE_SHA)
    assert isinstance(src, SealedMarginSource)
    assert src.source == "CONFIG_FLOOR"
    assert src.per_pair_margin == 100_000.0
    assert src.config_sha256 == hashlib.sha256(raw).hexdigest(), \
        "sha256 must be computed from the actual bytes"
    assert src.config_commit == RELEASE_SHA
    assert src.config_path == str(_SHARED_CFG)
    assert src.product == "TMF"
    assert parse_margin_config(raw, config_path=str(_SHARED_CFG),
                               release_sha=RELEASE_SHA) == src


def test_margin_source_loader_reads_file():
    src = _sealed(_SHARED_CFG)
    assert src.per_pair_margin == 100_000.0
    assert src.config_sha256 == hashlib.sha256(CONFIG_YAML.encode()).hexdigest()


def test_margin_source_missing_file_fails():
    with pytest.raises(Exception):
        load_trusted_margin_source("/nonexistent/futures.yaml",
                                   release_sha=RELEASE_SHA)


@pytest.mark.parametrize("yaml_text", [
    "mts:\n  live_required_margin_per_pair: 0.0\n",
    "mts:\n  live_required_margin_per_pair: -1.0\n",
    "mts:\n  live_required_margin_per_pair: nan\n",
    "mts:\n  live_required_margin_per_pair: inf\n",
    "mts:\n  other: 1\n",                        # missing key
    "not: [valid, yaml\n",                       # malformed
])
def test_margin_source_malformed_config_fails(tmp_path, yaml_text):
    p = _write_config(tmp_path, yaml_text)
    with pytest.raises(Exception):
        load_trusted_margin_source(p, release_sha=RELEASE_SHA)


def test_margin_source_unknown_product_fails():
    with pytest.raises(Exception):
        parse_margin_config(CONFIG_YAML.encode(), config_path="x",
                            release_sha=RELEASE_SHA, product="XXX")


def test_margin_source_empty_release_sha_fails():
    with pytest.raises(Exception):
        parse_margin_config(CONFIG_YAML.encode(), config_path="x",
                            release_sha="")


def test_certify_rejects_forged_margin_source_dict():
    # round-9 #4: an unsealed source is not accepted by a public
    # authorizing call
    api = _FakeApi()
    register_session(api)
    with pytest.raises(TypeError):
        certify_route(api, process_start_id="p-1", issuer=CertificateIssuer(),
                      margin_source={"per_pair_margin": 1.0})


def test_config_changed_after_cert_rejected(tmp_path):
    cfg_a = _write_config(tmp_path, CONFIG_YAML)
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api, config_path=cfg_a)
    assert cert is not None and failures == []
    _assert_nonce_ok(issuer, cert)
    cfg_a.write_text("mts:\n  live_required_margin_per_pair: 150000.0\n",
                     encoding="utf-8")           # file changed → new sha
    ok, reasons = _validated(cert, issuer, margin_source=_sealed(cfg_a))
    assert not ok and any("SOURCE" in r for r in reasons), reasons


def test_forged_sealed_metadata_never_reaches_certificate():
    # a caller-constructed SealedMarginSource with lying metadata cannot be
    # injected — certify_route loads from the config path itself
    api = _FakeApi()
    register_session(api)
    forged = SealedMarginSource(source="CONFIG_FLOOR", version=1,
                                config_path="/fake", config_sha256="0" * 64,
                                config_commit="deadbeef",
                                per_pair_margin=1.0, product="TMF")
    with pytest.raises(TypeError):
        certify_route(api, process_start_id="p-1", issuer=CertificateIssuer(),
                      margin_source=forged)


# ── round-9 #5: quote checks — exactly two unique passed codes ─────────────

def _preflight_base():
    return dict(
        account_id_hash="h", positions=[], open_orders=[],
        margin={"available_margin": 1_000_000.0},
        contracts={"near": {"code": "TMFH6"}, "far": {"code": "TMFI6"}},
        snapshot_codes=["TMFH6", "TMFI6"],
        position_snapshot_time="2026-08-08T00:00:00+00:00",
        order_snapshot_time="2026-08-08T00:00:00+00:00",
        query_failures=[], warnings=[])


@pytest.mark.parametrize("quote_codes", [
    [],                                              # empty → all([]) trap
    [{"code": "TMFH6", "passed": True}],             # one
    [{"code": "TMFH6", "passed": True},
     {"code": "TMFH6", "passed": True}],             # duplicate
    [{"code": "TMFX6", "passed": True},
     {"code": "TMFI6", "passed": True}],             # wrong code
    [{"code": "TMFH6", "passed": False},
     {"code": "TMFI6", "passed": True}],             # one failed
])
def test_quote_checks_require_exactly_two_unique_passed(tmp_path, monkeypatch,
                                                        quote_codes):
    import core.live_route_certificate as lrc
    cfg = _write_config(tmp_path)
    api = _FakeApi()
    register_session(api)
    base = _preflight_base()
    base["quote_subscription"] = quote_codes
    monkeypatch.setattr(lrc, "collect_read_only_preflight",
                        lambda api, product="TMF": base)
    cert, failures = certify_route(api, process_start_id="p-1",
                                   issuer=CertificateIssuer(),
                                   config_path=cfg, release_sha=RELEASE_SHA)
    assert cert is None
    assert any("QUOTE" in f for f in failures), failures


def test_quote_checks_two_unique_passed_ok(tmp_path, monkeypatch):
    import core.live_route_certificate as lrc
    cfg = _write_config(tmp_path)
    api = _FakeApi()
    register_session(api)
    base = _preflight_base()
    base["quote_subscription"] = [
        {"code": "TMFH6", "passed": True},
        {"code": "TMFI6", "passed": True}]
    monkeypatch.setattr(lrc, "collect_read_only_preflight",
                        lambda api, product="TMF": base)
    cert, failures = certify_route(api, process_start_id="p-1",
                                   issuer=CertificateIssuer(),
                                   config_path=cfg, release_sha=RELEASE_SHA)
    assert cert is not None and failures == []


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


def test_certificate_binds_identity_and_snapshots():
    api = _FakeApi()
    register_session(api)
    cert, failures, _ = _certify(api)
    assert cert.process_start_id == "p-1"
    assert cert.account_hash
    assert cert.session_generation
    assert cert.margin_source == "CONFIG_FLOOR"
    assert cert.config_path == str(_SHARED_CFG)
    assert cert.config_sha256 == hashlib.sha256(CONFIG_YAML.encode()).hexdigest()
    assert cert.config_commit == RELEASE_SHA
    assert cert.product == "TMF"
    assert cert.position_snapshot_ts and cert.order_snapshot_ts
    assert cert.bidask_subscribed == ("NEAR", "FAR")
    assert cert.bidask_unsubscribed == ("NEAR", "FAR")


def test_certify_route_collects_through_real_preflight_surface():
    api = _FakeApi()
    register_session(api)
    cert, failures, _ = _certify(api)
    assert failures == [], failures
    for method in ("list_positions", "list_trades", "margin", "snapshots",
                   "subscribe", "unsubscribe", "list_accounts"):
        assert method in api.calls, f"{method} was never called"
    assert cert.near_code == "TMFH6" and cert.far_code == "TMFI6"


def test_no_order_calls_in_full_certification_cycle():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    ok, reasons = _validated(cert, issuer)
    assert ok and reasons == []
    assert not any(c in ("place_order", "cancel_order", "modify_order")
                   for c in api.calls), api.calls


def test_query_failure_quarantines_zero_submit():
    api = _FakeApi()
    register_session(api)
    api.margin = lambda account: (_ for _ in ()).throw(RuntimeError("margin down"))
    cert, failures, _ = _certify(api)
    assert cert is None
    assert any("MARGIN" in f for f in failures)
    assert not any(c in ("place_order", "cancel_order", "modify_order")
                   for c in api.calls)


# ── RuntimeCertificationContext — trusted factory only ─────────────────────

def test_factory_builds_context_from_trusted_config():
    api = _FakeApi()
    register_session(api)
    ctx = _ctx_runtime(api)
    assert isinstance(ctx, RuntimeCertificationContext)
    assert ctx.account_hash
    assert ctx.near_code == "TMFH6" and ctx.far_code == "TMFI6"
    assert ctx.margin_source.source == "CONFIG_FLOOR"
    assert ctx.margin_source.config_sha256 == \
        hashlib.sha256(CONFIG_YAML.encode()).hexdigest()
    assert ctx.session_generation == session_registry.generation(api)
    assert ctx.process_start_id == "p-1"


# ── transition: canonicality (round-9 #7) + quarantine contract ────────────

def test_transition_rejects_forged_dict_runtime():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    assert cert is not None
    forged = {"account_hash": "x", "near_code": "TMFH6", "far_code": "TMFI6"}
    with pytest.raises(Exception):
        transition_with_certificate(_transition_ctx(), cert, issuer,
                                    runtime=forged)


def test_transition_succeeds_and_consumes_exactly_once():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    rt = _ctx_runtime(api, cert)
    ready = transition_with_certificate(_transition_ctx(), cert, issuer,
                                        runtime=rt)
    assert ready.is_live_ready()
    assert issuer.peek(cert.nonce) is None
    with pytest.raises(Exception):
        transition_with_certificate(_transition_ctx(), cert, issuer, runtime=rt)


@pytest.mark.parametrize("field,value", [
    ("captured_at", "2020-01-01T00:00:00+00:00"),
    ("margin_available", 1.0),
    ("required_margin", 1.0),
    ("account_hash", "deadbeef"),
    ("near_code", "TMFJ6"),
    ("far_code", "TMFJ6"),
    ("config_sha256", "f" * 64),
    ("config_commit", "f" * 16),
    ("product", "MTX"),
])
def test_transition_rejects_any_tampering_same_nonce(field, value):
    # round-9 #7: canonical certificate equality — same nonce/issuer but any
    # altered field → CERT_TAMPERED + quarantine
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    forged = LiveBrokerCertificate(**{**cert.__dict__, field: value})
    result = transition_with_certificate(_transition_ctx(), forged, issuer,
                                         runtime=_ctx_runtime(api, cert))
    assert not result.is_live_ready()
    assert result.to_dict().get("effective_mode") == "live_quarantined"
    assert "CERT_TAMPERED" in result.audit_reasons, result.audit_reasons
    assert issuer.peek(cert.nonce) is not None, "failed transition preserves nonce"


def test_transition_rejects_mutated_certificate_same_nonce():
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    forged = LiveBrokerCertificate(**{**cert.__dict__, "near_code": "TMFJ6"})
    result = transition_with_certificate(_transition_ctx(), forged, issuer,
                                         runtime=_ctx_runtime(api, cert))
    assert not result.is_live_ready()
    assert "CERT_TAMPERED" in result.audit_reasons
    assert issuer.peek(cert.nonce) is not None


def test_transition_failed_returns_quarantined_with_audit_reason(tmp_path):
    api = _FakeApi()
    register_session(api)
    cert, failures, issuer = _certify(api)
    cfg2 = _write_config(tmp_path, "mts:\n  live_required_margin_per_pair: 150000.0\n")
    rt2 = _ctx_runtime(api, cert, config_path=cfg2)
    result = transition_with_certificate(_transition_ctx(), cert, issuer,
                                         runtime=rt2)
    assert result.to_dict().get("effective_mode") == "live_quarantined"
    assert result.audit_reasons, "quarantined context must carry audit reasons"
    assert not result.is_live_ready()


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
        config_path=_SHARED_CFG, release_sha=RELEASE_SHA)
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
