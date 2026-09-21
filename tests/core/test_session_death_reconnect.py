from types import SimpleNamespace


class SessionDeadError(Exception):
    pass


def _dead_error():
    return SessionDeadError(
        "ShioajiConnectionError: SubCode(SessionNotEstablished)")


def test_session_death_triggers_one_reconnect_and_records_events():
    from core.shioaji_session import SessionReconnectController

    events = []
    candidate = SimpleNamespace(name="replacement")
    controller = SessionReconnectController(
        max_attempts=3, backoff_seconds=(1.0, 2.0), sleep=lambda _: None)

    result = controller.reconnect(
        _dead_error(),
        api_factory=lambda: candidate,
        login=lambda api: None,
        restore=lambda api: None,
        verify=lambda api: True,
        emit=lambda event, **payload: events.append((event, payload)),
    )

    assert result is candidate
    assert [event for event, _ in events] == [
        "SESSION_RECONNECT_ATTEMPT", "SESSION_RECONNECT_OK"]
    assert events[0][1]["attempt"] == 1


def test_reconnect_failure_is_bounded_and_never_sends_orders():
    from core.shioaji_session import SessionReconnectController

    events = []
    sleeps = []
    created = []
    order_calls = []

    def factory():
        api = SimpleNamespace(
            place_order=lambda *args, **kwargs: order_calls.append(
                (args, kwargs)))
        created.append(api)
        return api

    controller = SessionReconnectController(
        max_attempts=3, backoff_seconds=(1.0, 2.0),
        sleep=sleeps.append)

    def fail_login(api):
        raise SessionDeadError("still unavailable")

    assert controller.reconnect(
        _dead_error(), api_factory=factory, login=fail_login,
        restore=lambda api: None, verify=lambda api: False,
        emit=lambda event, **payload: events.append((event, payload)),
    ) is None
    assert len(created) == 3
    assert sleeps == [1.0, 2.0]
    assert order_calls == []
    assert [event for event, _ in events].count(
        "SESSION_RECONNECT_FAILED") == 3

    # Circuit stays exhausted for this process/session incident.
    assert controller.reconnect(
        _dead_error(), api_factory=factory, login=fail_login,
        restore=lambda api: None, verify=lambda api: False,
        emit=lambda event, **payload: events.append((event, payload)),
    ) is None
    assert len(created) == 3


def test_successful_reconnect_restores_position_refresh():
    from core.shioaji_session import SessionReconnectController

    replacement = SimpleNamespace(ready=False)
    controller = SessionReconnectController(
        max_attempts=3, backoff_seconds=(1.0, 2.0), sleep=lambda _: None)

    def restore(api):
        api.ready = True
        api.list_positions = lambda account=None: [{"code": "TMFJ6"}]

    result = controller.reconnect(
        _dead_error(), api_factory=lambda: replacement,
        login=lambda api: None, restore=restore,
        verify=lambda api: api.list_positions(account="futures") == [
            {"code": "TMFJ6"}],
        emit=lambda event, **payload: None,
    )

    assert result is replacement
    assert result.list_positions(account="futures") == [{"code": "TMFJ6"}]


def test_refresh_session_death_requests_reconnect_once():
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._futures_refresh_timeout_s = 1.0
    monitor._append_mts_event = lambda *args, **kwargs: None
    monitor.order_mgr = None
    calls = []
    monitor._session_reconnect_callback = (
        lambda exc, **payload: calls.append((exc, payload)))

    class DeadApi:
        is_connected = True

        def update_status(self, *, account):
            raise _dead_error()

        def list_trades(self):
            raise AssertionError("must not continue after session death")

    result = monitor._refresh_futures_trade_view(DeadApi(), "futures")

    assert result["state"] == "REFRESH_EXCEPTION"
    assert len(calls) == 1
    assert calls[0][1]["source"] == "futures_trade_refresh"
