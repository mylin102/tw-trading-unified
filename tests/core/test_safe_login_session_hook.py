#!/usr/bin/env python3
"""Round-9 #8: safe_login hook behavior (core/broker/shioaji_compat).

Contract:
- a successful login registers exactly once (new opaque generation)
- a FAILED login leaves NO valid generation (unregister-before + no
  register-after)
- the TypeError fallback path (1.3.3-style) registers exactly once too
- existing return/error semantics are preserved (returns the login result,
  propagates exceptions)
- logout invalidation lives in core/shioaji_session (covered in
  test_live_route_certificate.py)
"""

import pytest
import sys
from types import SimpleNamespace

from core.broker.shioaji_compat import fetch_all_contracts, safe_login
from core.live_route_certificate import session_registry


class _FakeSj:
    """Scripted login surface: each step is either "ok" or an exception."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def login(self, **kwargs):
        self.calls.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def test_safe_login_success_registers_once():
    api = _FakeSj(["ok"])
    result = safe_login(api, api_key="k", secret_key="s")
    assert result == "ok"
    gen = session_registry.generation(api)
    assert gen is not None and len(gen) >= 32
    # exactly one registration (generation stable across reads)
    assert session_registry.generation(api) == gen
    # first attempt carries the contracts_timeout default
    assert api.calls[0]["contracts_timeout"] == 10000


def test_safe_login_false_return_registers_nothing():
    # round-11 #2: Shioaji's documented failure mode is exceptions, but a
    # falsey login return is treated as failure (fail-closed) — no
    # generation is registered
    api = _FakeSj([False])
    result = safe_login(api, api_key="k", secret_key="s")
    assert result is False
    assert session_registry.generation(api) is None, \
        "a falsey login return must not register a generation"


def test_safe_login_failure_leaves_no_generation():
    api = _FakeSj([RuntimeError("broker down")])
    with pytest.raises(RuntimeError):
        safe_login(api, api_key="k", secret_key="s")
    assert session_registry.generation(api) is None, \
        "failed login must leave no valid generation"


def test_safe_login_typeerror_fallback_registers_once():
    # 1.3.3-style: first call raises TypeError (contracts_timeout unsupported)
    # → fallback retries without contracts_timeout → success
    api = _FakeSj([TypeError("contracts_timeout"), "ok"])
    result = safe_login(api, api_key="k", secret_key="s")
    assert result == "ok"
    gen = session_registry.generation(api)
    assert gen is not None
    assert session_registry.generation(api) == gen          # registered once
    assert "contracts_timeout" in api.calls[0]
    assert "contracts_timeout" not in api.calls[1]           # fallback dropped it


def test_safe_login_fallback_failure_leaves_no_generation():
    api = _FakeSj([TypeError("contracts_timeout"), RuntimeError("still down")])
    with pytest.raises(RuntimeError):
        safe_login(api, api_key="k", secret_key="s")
    assert session_registry.generation(api) is None


def test_safe_login_invalidates_previous_registration_before_attempt():
    # pre-attempt invalidate: an old registration is dead before the attempt
    api = _FakeSj([RuntimeError("down")])
    session_registry.register(api)                # simulate previous session
    old_gen = session_registry.generation(api)
    assert old_gen is not None
    with pytest.raises(RuntimeError):
        safe_login(api, api_key="k", secret_key="s")
    assert session_registry.generation(api) is None, \
        "old registration must be invalidated before the attempt"


def test_ca_activation_uses_logged_in_futopt_person_id():
    """Order signing must bind the CA to the actual futures account owner."""
    from core.shioaji_session import _activate_futopt_ca

    class Api:
        futopt_account = SimpleNamespace(person_id="FUTOPT_OWNER")

        def __init__(self):
            self.calls = []

        def activate_ca(self, **kwargs):
            self.calls.append(kwargs)

    api = Api()
    _activate_futopt_ca(api, "/safe/certificate.pfx", "password")

    assert api.calls == [{
        "ca_path": "/safe/certificate.pfx",
        "ca_passwd": "password",
        "person_id": "FUTOPT_OWNER",
    }]


def test_ca_activation_refuses_missing_futopt_person_id():
    """A CA without its account identity must never appear activated."""
    from core.shioaji_session import _activate_futopt_ca

    class Api:
        futopt_account = SimpleNamespace(person_id=None)

        def activate_ca(self, **kwargs):
            raise AssertionError("must not activate without a futures owner")

    with pytest.raises(RuntimeError, match="futures account identity"):
        _activate_futopt_ca(Api(), "/safe/certificate.pfx", "password")


def test_contract_sync_worker_uses_futopt_person_id(monkeypatch):
    """The isolated contract-fetch session follows the same CA contract."""
    from core import shioaji_session

    class Api:
        futopt_account = SimpleNamespace(person_id="FUTOPT_OWNER")

        def __init__(self):
            self.ca_calls = []

        def login(self, *args):
            assert args == ("api-key", "secret-key")

        def activate_ca(self, **kwargs):
            self.ca_calls.append(kwargs)

        def fetch_contracts(self):
            return None

    api = Api()
    received = []
    monkeypatch.setitem(sys.modules, "shioaji", SimpleNamespace(Shioaji=lambda: api))
    monkeypatch.setattr(shioaji_session.os.path, "exists", lambda path: True)

    shioaji_session._sync_worker(
        "api-key", "secret-key", "/safe/certificate.pfx", "password",
        SimpleNamespace(put=received.append),
    )

    assert received == [True]
    assert api.ca_calls == [{
        "ca_path": "/safe/certificate.pfx",
        "ca_passwd": "password",
        "person_id": "FUTOPT_OWNER",
    }]


def test_contract_sync_worker_redacts_sensitive_ca_error(monkeypatch):
    """Certificate failures cannot leak provider context over IPC/logging."""
    from core import shioaji_session

    class Api:
        futopt_account = SimpleNamespace(person_id="FUTOPT_OWNER")

        def login(self, *args):
            return None

        def activate_ca(self, **kwargs):
            raise RuntimeError("certificate=/safe/certificate.pfx owner=FUTOPT_OWNER")

    received = []
    monkeypatch.setitem(sys.modules, "shioaji", SimpleNamespace(Shioaji=Api))
    monkeypatch.setattr(shioaji_session.os.path, "exists", lambda path: True)

    shioaji_session._sync_worker(
        "api-key", "secret-key", "/safe/certificate.pfx", "password",
        SimpleNamespace(put=received.append),
    )

    assert received == [False]


def test_ca_material_refuses_missing_certificate_file(monkeypatch):
    """Live startup must not silently skip signing when CA material is absent."""
    from core import shioaji_session

    monkeypatch.setattr(shioaji_session.os.path, "isfile", lambda path: False)

    with pytest.raises(RuntimeError, match="certificate file unavailable"):
        shioaji_session._require_ca_material("/missing/certificate.pfx", "password")


def test_ca_material_returns_existing_path_and_password(monkeypatch):
    """Only verified certificate material can proceed to account activation."""
    from core import shioaji_session

    monkeypatch.setattr(shioaji_session.os.path, "isfile", lambda path: True)

    assert shioaji_session._require_ca_material(
        "/safe/certificate.pfx", "password") == ("/safe/certificate.pfx", "password")


def test_contract_sync_uses_existing_futures_cache_without_child_refresh(monkeypatch):
    """A live parent session must never trigger a second-login refresh worker."""
    from core import shioaji_session
    import core.broker.shioaji_compat as compat

    class Api:
        Contracts = SimpleNamespace(
            Futures=SimpleNamespace(TMF=object()),
        )

    def unexpected_refresh(_api):
        raise AssertionError("contract refresh must not re-login beside the live session")

    monkeypatch.setattr(shioaji_session, "fetch_contracts", unexpected_refresh)
    monkeypatch.setattr(compat.time, "sleep", lambda _seconds: None)

    assert fetch_all_contracts(Api(), timeout=0.001) is True


def test_contract_sync_missing_cache_refuses_without_child_refresh(monkeypatch):
    """Missing cache is a safe startup refusal, not a child re-login loop."""
    from core import shioaji_session
    import core.broker.shioaji_compat as compat

    class Api:
        Contracts = SimpleNamespace(Futures=SimpleNamespace())

    def unexpected_refresh(_api):
        raise AssertionError("contract refresh must not re-login beside the live session")

    monkeypatch.setattr(shioaji_session, "fetch_contracts", unexpected_refresh)
    monkeypatch.setattr(compat.time, "sleep", lambda _seconds: None)

    assert fetch_all_contracts(Api(), timeout=0.001) is False
