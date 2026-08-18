from types import SimpleNamespace


def test_register_order_callback_is_idempotent_and_observable(monkeypatch):
    import main

    class API:
        def __init__(self): self.callbacks = []
        def set_order_callback(self, callback): self.callbacks.append(callback)

    events = []
    mon = SimpleNamespace(
        _append_mts_event=lambda *a, **kw: events.append((a, kw)))
    api = API()
    monkeypatch.setattr(main, "_order_callback_fn", None)
    monkeypatch.setattr(main, "_order_callback_generation", 0)
    monkeypatch.setattr(main, "_quarantine_for_handoff",
                        lambda *a, **kw: None)

    assert main.register_order_callback(api, [mon], None) is True
    assert main.register_order_callback(api, [mon], None) is True
    assert len(api.callbacks) == 2
    assert api.callbacks[0] is api.callbacks[1]
    assert main._order_callback_generation == 2
    assert [item[0][0] for item in events] == [
        "ORDER_CALLBACK_REGISTERED", "ORDER_CALLBACK_REGISTERED"]


def test_safe_login_explicitly_subscribes_trade(monkeypatch):
    from core.broker import shioaji_compat
    from core.live_route_certificate import session_registry

    class API:
        def __init__(self): self.kwargs = None
        def login(self, **kwargs): self.kwargs = kwargs; return True

    api = API()
    monkeypatch.setattr(session_registry, "unregister",
                        lambda api: None)
    monkeypatch.setattr(session_registry, "register",
                        lambda api: None)
    assert shioaji_compat.safe_login(api, "key", "secret") is True
    assert api.kwargs["subscribe_trade"] is True


def test_callback_registration_failure_keeps_quarantine(monkeypatch):
    import main

    class API:
        def set_order_callback(self, callback):
            raise RuntimeError("registration failed")

    events = []
    quarantined = []
    mon = SimpleNamespace(
        _append_mts_event=lambda *a, **kw: events.append((a, kw)))
    monkeypatch.setattr(main, "_order_callback_fn", None)
    monkeypatch.setattr(main, "_quarantine_for_handoff",
                        lambda *a, **kw: quarantined.append(kw))
    assert main.register_order_callback(API(), [mon], None) is False
    assert quarantined and quarantined[0]["reason"] == \
        "ORDER_CALLBACK_REGISTRATION_FAILED"
    assert events[0][0][0] == "ORDER_CALLBACK_REGISTRATION_FAILED"


def test_callback_exception_is_durable_and_dispatcher_remains_single(monkeypatch):
    import main

    events = []
    mon = SimpleNamespace(
        _append_mts_event=lambda *a, **kw: events.append((a, kw)),
        on_order_event=lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("callback boom")))
    monkeypatch.setattr(main, "_order_callback_fn", None)
    callback = main.order_dispatcher([mon], None)
    callback("FDEAL", {"trade_id": "d1"})
    assert any(item[0][0] == "ORDER_CALLBACK_EXCEPTION" for item in events)
    assert getattr(mon, "_callback_exception_count") == 1
