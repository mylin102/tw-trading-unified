import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


def _client(monkeypatch, snapshot):
    from strategies.futures.squeeze_futures.data.shioaji_client import ShioajiClient
    from core.mode_transition import ExecutionContext, ModeTransitionState

    client = ShioajiClient.__new__(ShioajiClient)
    client.api = SimpleNamespace()
    client.is_logged_in = True
    client._execution_context = ExecutionContext(
        requested_mode="live", effective_mode=ModeTransitionState.LIVE_READY.value,
        live_order_allowed=True,
    )
    client._broker_snapshot_provider = lambda: snapshot
    calls = []
    client._place_order_unchecked = lambda *args, **kwargs: calls.append((args, kwargs)) or "TRADE"
    return client, calls


def test_p0_2_monitor_has_no_direct_api_place_order():
    source = Path("strategies/futures/monitor.py").read_text()
    tree = ast.parse(source)
    direct = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Attribute) and owner.attr == "api" and node.func.attr == "place_order":
                direct.append(node.lineno)
    assert not direct, f"direct api.place_order bypasses client: {direct}"


def test_r1_flat_state_claiming_release_is_rejected(monkeypatch):
    from strategies.futures.squeeze_futures.data.shioaji_client import AdapterOrderError
    client, calls = _client(monkeypatch, {
        "source": "live_broker", "positions": [],
        "fetch_status": {"capture": "OK"},
    })
    with pytest.raises(AdapterOrderError, match="BROKER_PREMISE_MISMATCH"):
        client.place_order(SimpleNamespace(code="TMFJ6"), "BUY", 1,
                           premise={"kind": "HOLD_LEG", "leg": "near",
                                    "side": "SHORT", "qty": 1},
                           intent_action="RELEASE_NEAR")
    assert calls == []


def test_r2_held_leg_release_is_allowed(monkeypatch):
    client, calls = _client(monkeypatch, {
        "source": "live_broker", "positions": [
            {"code": "TMFJ6", "side": "SHORT", "qty": 1}],
        "fetch_status": {"capture": "OK"},
    })
    assert client.place_order(SimpleNamespace(code="TMFJ6"), "BUY", 1,
                              premise={"kind": "HOLD_LEG", "leg": "near",
                                       "side": "SHORT", "qty": 1},
                              intent_action="RELEASE_NEAR") == "TRADE"
    assert len(calls) == 1


def test_r3_unavailable_is_fail_closed(monkeypatch):
    from strategies.futures.squeeze_futures.data.shioaji_client import AdapterOrderError
    client, calls = _client(monkeypatch, {
        "source": "live_broker", "positions": None,
        "fetch_status": {"capture": "FAIL"},
    })
    with pytest.raises(AdapterOrderError, match="BROKER_QUERY_UNAVAILABLE"):
        client.place_order(SimpleNamespace(code="TMFJ6"), "BUY", 1,
                           premise={"kind": "HOLD_LEG", "leg": "near",
                                    "side": "SHORT", "qty": 1},
                           intent_action="RELEASE_NEAR")
    assert calls == []


def test_mutation_guard_is_not_optional(monkeypatch):
    from strategies.futures.squeeze_futures.data.shioaji_client import AdapterOrderError
    client, calls = _client(monkeypatch, {
        "source": "live_broker", "positions": [],
        "fetch_status": {"capture": "OK"},
    })
    with pytest.raises(AdapterOrderError):
        client.place_order(SimpleNamespace(code="TMFJ6"), "BUY", 1,
                           premise={"kind": "HOLD_LEG", "leg": "near",
                                    "side": "SHORT", "qty": 1},
                           intent_action="RELEASE_NEAR")
    assert calls == []
