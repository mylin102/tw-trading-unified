from types import SimpleNamespace

import pytest


def test_get_api_recreates_singleton_after_failed_login(monkeypatch):
    """A failed first login must not make retries reuse an unauthenticated API."""
    from core import shioaji_session

    created = []
    login_calls = []

    class Api:
        pass

    def factory():
        api = Api()
        created.append(api)
        return api

    def login(api):
        login_calls.append(api)
        if len(login_calls) == 1:
            raise RuntimeError("transient broker login failure")

    monkeypatch.setattr(shioaji_session.sj, "Shioaji", factory)
    monkeypatch.setattr(shioaji_session, "_login", login)
    monkeypatch.setattr(
        "core.deployment_role_gate.assert_broker_access_allowed", lambda: None)
    monkeypatch.setattr(shioaji_session, "_api", None)

    with pytest.raises(RuntimeError, match="transient broker"):
        shioaji_session.get_api()

    api = shioaji_session.get_api()
    assert api is created[1]
    assert created == login_calls
    assert len(created) == 2
