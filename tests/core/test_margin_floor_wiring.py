#!/usr/bin/env python3
"""TMF margin floor/config closure — trusted config source wiring.

Core (round-9) already seals the margin source from the ACTUAL config
bytes (path/sha256/commit/product/floor) and requires the broker account
capacity query. The remaining blocker: the EFFECTIVE config must define
the explicit TMF key mts.live_required_margin_per_pair (no default).

Contracts:
- missing / zero / NaN / inf / non-numeric / unknown product -> fail-closed
- exact margin boundary: capacity == per_pair*(1+buffer) -> OK; below -> MARGIN
- any config/source change after certification -> SOURCE_MISMATCH ->
  LIVE_QUARANTINED
- startup wiring: the effective repo config carries the finite positive key
"""

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHA = "a" * 40


def _helpers():
    spec = importlib.util.spec_from_file_location(
        "_lrc_helpers", _REPO_ROOT / "tests" / "core" /
        "test_live_route_certificate.py")
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    return h


def _cfg(tmp_path, floor="100000.0"):
    p = tmp_path / "futures.yaml"
    p.write_text(f"mts:\n  live_required_margin_per_pair: {floor}\n",
                 encoding="utf-8")
    return p


def test_missing_key_fail_closed(tmp_path, monkeypatch):
    import core.live_route_certificate as lrc
    monkeypatch.setenv("LRC_RELEASE_SHA", _SHA)
    p = tmp_path / "futures.yaml"
    p.write_text("mts:\n  enabled: true\n", encoding="utf-8")
    with pytest.raises(ValueError):
        lrc.load_trusted_margin_source(str(p))


@pytest.mark.parametrize("floor", ["0", "-1", "nan", "inf", "abc", "true"])
def test_invalid_floor_fail_closed(tmp_path, monkeypatch, floor):
    import core.live_route_certificate as lrc
    monkeypatch.setenv("LRC_RELEASE_SHA", _SHA)
    p = _cfg(tmp_path, floor=floor)
    with pytest.raises(ValueError):
        lrc.load_trusted_margin_source(str(p))


def test_unknown_product_fail_closed(tmp_path, monkeypatch):
    import core.live_route_certificate as lrc
    monkeypatch.setenv("LRC_RELEASE_SHA", _SHA)
    p = _cfg(tmp_path)
    with pytest.raises(ValueError):
        lrc.load_trusted_margin_source(str(p), product="TXO")


def test_exact_margin_boundary(monkeypatch):
    # capacity == per_pair*(1+buffer) -> OK; just below -> MARGIN fail
    h = _helpers()
    monkeypatch.setenv("LRC_RELEASE_SHA", _SHA)
    from core.live_route_certificate import register_session
    api = h._FakeApi(margin_val=110_000.0)      # exact boundary
    register_session(api)
    cert, failures, _ = h._certify(api)
    assert cert is not None and failures == [], failures
    assert cert.required_margin == 110_000.0
    api2 = h._FakeApi(margin_val=109_999.99)    # just below
    register_session(api2)
    cert2, failures2, _ = h._certify(api2)
    assert cert2 is None and any("MARGIN" in f for f in failures2), failures2


def test_changed_source_yields_source_mismatch(tmp_path, monkeypatch):
    # config/source change after certification -> SOURCE_MISMATCH ->
    # LIVE_QUARANTINED (never LIVE_READY with a stale margin source)
    h = _helpers()
    monkeypatch.setenv("LRC_RELEASE_SHA", _SHA)
    from core.live_route_certificate import (
        register_session, transition_with_certificate)
    from core.mode_transition import live_preflight_context
    cfg = _cfg(tmp_path, floor="100000.0")
    api = h._FakeApi()
    register_session(api)
    cert, failures, issuer = h._certify(api, config_path=str(cfg))
    assert cert is not None, failures
    # change the SOURCE (config file) before the transition
    cfg.write_text("mts:\n  live_required_margin_per_pair: 99999.0\n",
                   encoding="utf-8")
    runtime = h._ctx_runtime(api, config_path=str(cfg))
    result = transition_with_certificate(live_preflight_context(), cert,
                                         issuer, runtime=runtime)
    assert not result.is_live_ready(), \
        "changed margin source must never reach LIVE_READY"
    assert any("SOURCE_MISMATCH" in r for r in result.audit_reasons), \
        result.audit_reasons


def test_startup_wiring_effective_config_key():
    # the effective repo config defines the explicit TMF key with a
    # finite positive value (no default) — the certification can bind it
    cfg = _REPO_ROOT / "config" / "futures.yaml"
    text = cfg.read_text(encoding="utf-8")
    assert "live_required_margin_per_pair" in text
    import os
    old = os.environ.get("LRC_RELEASE_SHA")
    os.environ["LRC_RELEASE_SHA"] = _SHA
    try:
        import core.live_route_certificate as lrc
        source = lrc.load_trusted_margin_source(str(cfg))
        assert source.product == "TMF"
        assert source.per_pair_margin > 0
        import math
        assert math.isfinite(source.per_pair_margin)
    finally:
        if old is None:
            os.environ.pop("LRC_RELEASE_SHA", None)
        else:
            os.environ["LRC_RELEASE_SHA"] = old
