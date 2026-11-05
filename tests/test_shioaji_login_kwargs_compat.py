"""2026-11-05 Hermes Agent: shioaji 1.7.x removed login(contracts_timeout=...).

safe_login passed it unconditionally and only survived because the TypeError
fallback retried without it -- i.e. every start/reconnect spent one FAILED
authentication attempt. Tests pin the behaviour the code must have: pass the
kwarg only when the installed login() accepts it.
"""
import inspect

import pytest

from core.broker import shioaji_compat as compat


class _NewApi:
    """shioaji 1.7.10 signature."""
    def __init__(self):
        self.calls = []

    def login(self, api_key, secret_key, subscribe_trade=True, receive_window=30000,
              force_refresh=False):
        self.calls.append({"subscribe_trade": subscribe_trade})
        return True


class _OldApi:
    """shioaji 1.5.5 signature."""
    def __init__(self):
        self.calls = []

    def login(self, api_key, secret_key, fetch_contract=True, contracts_timeout=0,
              contracts_cb=None, subscribe_trade=True, receive_window=30000):
        self.calls.append({"contracts_timeout": contracts_timeout,
                           "subscribe_trade": subscribe_trade})
        return True


@pytest.fixture(autouse=True)
def _fresh_capability_probe(monkeypatch):
    monkeypatch.setattr(compat, "_LOGIN_ACCEPTS_CONTRACTS_TIMEOUT", None)


@pytest.fixture(autouse=True)
def _stub_registry(monkeypatch):
    import core.live_route_certificate as lrc
    class _Reg:
        def unregister(self, api): pass
        def register(self, api): pass
    monkeypatch.setattr(lrc, "session_registry", _Reg())


def test_17_login_never_receives_contracts_timeout():
    api = _NewApi()
    assert compat.safe_login(api, "k", "s", contracts_timeout=10000) is True
    assert len(api.calls) == 1, "must not need a failed attempt first"
    assert "contracts_timeout" not in api.calls[0]
    assert api.calls[0]["subscribe_trade"] is True


def test_155_login_still_receives_contracts_timeout():
    api = _OldApi()
    assert compat.safe_login(api, "k", "s", contracts_timeout=12345) is True
    assert api.calls[0]["contracts_timeout"] == 12345


def test_capability_probe_is_cached_once_per_process(monkeypatch):
    calls = {"n": 0}
    real = inspect.signature

    def counting(obj, *a, **kw):
        calls["n"] += 1
        return real(obj, *a, **kw)

    monkeypatch.setattr(compat.inspect, "signature", counting)
    api = _NewApi()
    compat.safe_login(api, "k", "s")
    compat.safe_login(api, "k", "s")
    assert calls["n"] == 1


def test_installed_shioaji_login_matches_the_probe():
    """Guard against a future shioaji re-adding the kwarg silently."""
    import shioaji
    params = inspect.signature(shioaji.Shioaji.login).parameters
    expected = "contracts_timeout" in params
    assert compat._login_accepts_contracts_timeout(shioaji.Shioaji()) is expected
