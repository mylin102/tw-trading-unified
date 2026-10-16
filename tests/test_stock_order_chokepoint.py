import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from strategies.stocks.order_chokepoint import StockOrderChokepoint


def _cfg():
    return {"stocks": {
        "capital_per_trade": 10000,
        "total_portfolio_budget": 30000,
        "bear_defense": {
            "max_daily_loss": 3000,
            "normal_max_positions": 3,
            "bear_max_positions": 1,
        },
    }}


def _intent(**overrides):
    value = {
        "decision_id": "decision-1", "bar_id": "bar-1", "symbol": "2330",
        "side": "BUY", "qty": 5, "price": 100.0,
        "strategy_version": "mean_reversion_enhanced",
        "current_exposure": 0, "open_positions": [], "daily_pnl": 0,
        "position_snapshot_ok": True, "price_limit_ok": True,
        "now": datetime(2026, 9, 15, 10, 0),
    }
    value.update(overrides)
    return value


def _gateway(tmp_path, broker=None, **kwargs):
    return StockOrderChokepoint(
        broker or SimpleNamespace(place_order=lambda *_: "TRADE"),
        intent_path=tmp_path / "intents.jsonl", config=_cfg(), mode="LIVE",
        **kwargs)


def test_authorization_rejects_limit_without_broker_call(tmp_path):
    broker = SimpleNamespace(place_order=lambda *_: pytest.fail("broker called"))
    result = _gateway(tmp_path, broker).submit_stock_order(
        intent=_intent(price=3000), contract=object(), order=object())
    assert result.accepted is False
    assert result.reason == "PER_ORDER_LIMIT"
    assert "RECEIVED" in (tmp_path / "intents.jsonl").read_text()


@pytest.mark.parametrize("strategy_version", ["mean_reversion", None])
def test_plain_or_missing_strategy_version_is_rejected(tmp_path, strategy_version):
    broker = SimpleNamespace(place_order=lambda *_: pytest.fail("broker called"))
    result = _gateway(tmp_path, broker).submit_stock_order(
        intent=_intent(strategy_version=strategy_version),
        contract=object(), order=object())
    assert result.accepted is False
    assert result.reason == "STRATEGY_VERSION_UNAUTHORIZED"


def test_enhanced_strategy_version_is_authorized(tmp_path):
    broker = SimpleNamespace(place_order=lambda *_: "TRADE")
    result = _gateway(tmp_path, broker).submit_stock_order(
        intent=_intent(strategy_version="mean_reversion_enhanced"),
        contract=object(), order=object())
    assert result.accepted is True
    assert result.reason == "BROKER_SUBMITTED"


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"current_exposure": 29600}, "TOTAL_EXPOSURE_LIMIT"),
    ({"open_positions": ["1101", "1102", "1103"]}, "MAX_POSITIONS_LIMIT"),
    ({"daily_pnl": -3001}, "DAILY_LOSS_LIMIT"),
    ({"now": datetime(2026, 9, 15, 8, 59)}, "OUTSIDE_TRADING_SESSION"),
    ({"price_limit_ok": False}, "PRICE_LIMIT_UNVERIFIED"),
])
def test_each_risk_boundary_rejects_without_broker_call(tmp_path, overrides, reason):
    broker = SimpleNamespace(place_order=lambda *_: pytest.fail("broker called"))
    result = _gateway(tmp_path, broker).submit_stock_order(
        intent=_intent(**overrides), contract=object(), order=object())
    assert result.reason == reason


def test_persist_failure_is_no_trade(tmp_path, monkeypatch):
    gateway = _gateway(tmp_path)
    monkeypatch.setattr(gateway, "_append_intent", lambda _: (_ for _ in ()).throw(OSError("disk")))
    result = gateway.submit_stock_order(intent=_intent(), contract=object(), order=object())
    assert result.reason == "INTENT_PERSIST_FAILED"


def test_duplicate_decision_bar_is_rejected(tmp_path):
    broker = SimpleNamespace(place_order=lambda *_: "TRADE")
    gateway = _gateway(tmp_path, broker)
    first = gateway.submit_stock_order(intent=_intent(), contract=object(), order=object())
    second = gateway.submit_stock_order(intent=_intent(), contract=object(), order=object())
    assert first.accepted is True
    assert second.reason == "DUPLICATE_INTENT"
    events = [json.loads(line) for line in (tmp_path / "intents.jsonl").read_text().splitlines()]
    assert events[-1]["status"] == "DUPLICATE"


def test_auth_error_is_fail_closed(tmp_path):
    broker = SimpleNamespace(place_order=lambda *_: pytest.fail("broker called"))
    result = _gateway(tmp_path, broker).submit_stock_order(
        intent=_intent(position_snapshot_ok=None), contract=object(), order=object())
    assert result.reason == "POSITION_SNAPSHOT_UNVERIFIED"


def test_paper_accepts_without_broker_call_and_keeps_intent(tmp_path):
    broker = SimpleNamespace(place_order=lambda *_: pytest.fail("broker called"))
    gateway = StockOrderChokepoint(
        broker, intent_path=tmp_path / "intents.jsonl", config=_cfg(), mode="PAPER")
    result = gateway.submit_stock_order(intent=_intent(), contract=object(), order=object())
    assert result.accepted is True
    assert result.reason == "PAPER_ACCEPTED"
    events = [json.loads(line) for line in (tmp_path / "intents.jsonl").read_text().splitlines()]
    assert events[0]["decision_id"] == "decision-1"
    assert events[0]["strategy_version"] == "mean_reversion_enhanced"
    assert events[-1]["status"] == "PAPER_ACCEPTED"


def test_source_has_no_stock_broker_order_calls_outside_chokepoint():
    from pathlib import Path
    source = Path("strategies/stocks/monitor.py").read_text()
    assert ".place_order(" not in source
    assert ".cancel_order(" not in source
