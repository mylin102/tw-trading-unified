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


def test_post_start_login_smoke_reaches_readiness_after_authentication(monkeypatch):
    """Exercise the real get_api -> _login path with a PAPER-style API double."""
    from core import shioaji_session
    from core.broker import shioaji_compat

    class Api:
        def __init__(self):
            self.authenticated = False
            self.position_queries = 0

        @property
        def futopt_account(self):
            if not self.authenticated:
                raise RuntimeError("Not authenticated")
            return "paper-account"

        def login(self, **kwargs):
            self.authenticated = True
            return True

        def list_positions(self, *, account):
            self.position_queries += 1
            assert account == "paper-account"
            return []

    api = Api()
    monkeypatch.setattr(shioaji_session.sj, "Shioaji", lambda: api)
    monkeypatch.setattr(shioaji_session, "_api", None)
    monkeypatch.setattr(shioaji_session, "_load_env_credentials", lambda: None)
    monkeypatch.setattr(shioaji_session, "_require_ca_material",
                        lambda path, passwd: ("paper-ca", "paper-pass"))
    monkeypatch.setattr(shioaji_session, "_activate_futopt_ca", lambda *args: None)
    monkeypatch.setenv("SHIOAJI_API_KEY", "paper-key")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "paper-secret")
    monkeypatch.setattr(shioaji_compat, "safe_login",
                        lambda obj, *args, **kwargs: obj.login())
    monkeypatch.setattr(
        "core.deployment_role_gate.assert_broker_access_allowed", lambda: None)

    assert shioaji_session.get_api() is api
    assert api.position_queries == 1


def test_post_start_login_smoke_rejects_false_login(monkeypatch):
    from core import shioaji_session
    from core.broker import shioaji_compat

    class Api:
        futopt_account = "paper-account"

    monkeypatch.setattr(shioaji_session.sj, "Shioaji", Api)
    monkeypatch.setattr(shioaji_session, "_api", None)
    monkeypatch.setattr(shioaji_session, "_load_env_credentials", lambda: None)
    monkeypatch.setattr(shioaji_session, "_require_ca_material",
                        lambda path, passwd: ("paper-ca", "paper-pass"))
    monkeypatch.setattr(shioaji_session, "_activate_futopt_ca", lambda *args: None)
    monkeypatch.setenv("SHIOAJI_API_KEY", "paper-key")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "paper-secret")
    monkeypatch.setattr(shioaji_compat, "safe_login", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "core.deployment_role_gate.assert_broker_access_allowed", lambda: None)

    with pytest.raises(RuntimeError, match="login"):
        shioaji_session.get_api()


def test_post_start_login_smoke_rejects_readiness_failure(monkeypatch):
    from core import shioaji_session
    from core.broker import shioaji_compat

    class Api:
        futopt_account = "paper-account"

        def list_positions(self, *, account):
            raise RuntimeError("SessionNotEstablished")

    monkeypatch.setattr(shioaji_session.sj, "Shioaji", Api)
    monkeypatch.setattr(shioaji_session, "_api", None)
    monkeypatch.setattr(shioaji_session, "_load_env_credentials", lambda: None)
    monkeypatch.setattr(shioaji_session, "_require_ca_material",
                        lambda path, passwd: ("paper-ca", "paper-pass"))
    monkeypatch.setattr(shioaji_session, "_activate_futopt_ca", lambda *args: None)
    monkeypatch.setenv("SHIOAJI_API_KEY", "paper-key")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "paper-secret")
    monkeypatch.setattr(shioaji_compat, "safe_login", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "core.deployment_role_gate.assert_broker_access_allowed", lambda: None)

    with pytest.raises(RuntimeError, match="readiness"):
        shioaji_session.get_api()
