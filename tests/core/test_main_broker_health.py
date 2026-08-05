# main.py api_is_healthy classified behavior — extracted-function test.
# main module import has heavy side effects (broker session, monitors), so we
# extract the api_is_healthy source and exec it in isolation with a stub
# console + fake shioaji exceptions + fake api.
# 2026-08-05.
import sys
import os
import re
import types
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class ShioajiError(Exception):
    """Plain exception (name-matched by core.broker_health classifier)."""


class AuthError(ShioajiError):
    pass


class TokenError(AuthError):
    pass


class ServerError(ShioajiError):
    pass


class BadRequestError(ShioajiError):
    pass


class _FakeApi:
    def __init__(self):
        self.calls = 0
        self.exc = None

    @property
    def futopt_account(self):
        return object()

    def list_positions(self, account):
        self.calls += 1
        if self.exc:
            raise self.exc
        return []


def _load_api_is_healthy():
    """Extract api_is_healthy + _get_broker_health from main.py and exec in
    isolation with a stub console and time module."""
    src = open(os.path.join(REPO, "main.py")).read()
    # grab from the tracker marker through end of api_is_healthy
    m = re.search(r"# 2026-08-05: broker health tracker.*?\n    return _tracker\.state\.value\n", src, re.S)
    assert m, "api_is_healthy block not found"
    code = m.group(0)
    ns = {
        "console": types.SimpleNamespace(print=lambda *a, **k: None),
        "time": __import__("time"),
        "classify_broker_error": None,  # filled below
        "BrokerErrorClass": None,
        "BrokerHealthTracker": None,
    }
    from core.broker_health import BrokerHealthTracker, classify_broker_error, BrokerErrorClass
    ns["BrokerHealthTracker"] = BrokerHealthTracker
    ns["classify_broker_error"] = classify_broker_error
    ns["BrokerErrorClass"] = BrokerErrorClass
    exec(compile(code, "main_api_is_healthy", "exec"), ns)
    return ns["api_is_healthy"], ns["_get_broker_health"]


@pytest.fixture()
def api_healthy():
    fn, _ = _load_api_is_healthy()
    return fn


def test_single_500_transient_no_exit(api_healthy):
    api = _FakeApi()
    api.exc = ServerError("list_positions: code: 500, detail: Please check param.")
    state = api_healthy(api)
    assert state in ("TRANSIENT_FAILURE", "DEGRADED")
    assert state != "PROCESS_RESTART_REQUIRED"


def test_500_then_success_healthy(api_healthy):
    api = _FakeApi()
    api.exc = ServerError("500")
    api_healthy(api)
    api.exc = None
    state = api_healthy(api)
    assert state == "HEALTHY"


def test_auth_session_invalid(api_healthy):
    api = _FakeApi()
    api.exc = TokenError("token expired")
    state = api_healthy(api)
    assert state == "SESSION_INVALID"
    assert state != "PROCESS_RESTART_REQUIRED"


def test_two_500s_degraded_never_restart(api_healthy):
    api = _FakeApi()
    api.exc = ServerError("500")
    s1 = api_healthy(api)
    s2 = api_healthy(api)
    assert s1 != "PROCESS_RESTART_REQUIRED"
    assert s2 == "DEGRADED"
