from datetime import datetime, timedelta

from core.strategy_context import MarketData, PositionView, StrategyContext
from strategies.plugins.futures.active import tmf_spread
from strategies.plugins.futures.active.tmf_spread import TMFSpread, _append_event as append_event


ENTRY_PARAMS = {"entry_z": 3.0, "min_atr": 1.0}


def _bar(ts=None, *, spread_z=-4.0, atr=20.0):
    ts = ts or datetime(2026, 11, 25, 20, 34)
    return {
        "timestamp": ts,
        "near_close": 39534.0,
        "far_close": 39698.0,
        "spread_z": spread_z,
        "atr": atr,
        "near_bid": 39533.0,
        "near_ask": 39535.0,
        "far_bid": 39697.0,
        "far_ask": 39699.0,
        "near_high": 39534.0,
        "near_low": 39534.0,
        "far_high": 39698.0,
        "far_low": 39698.0,
        "near_tick_age_ms": 10.0,
        "far_tick_age_ms": 10.0,
        "quote_age_ms": 10.0,
        "pair_skew_ms": 0.0,
    }


def _strategy(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("TRADING_RUNTIME_ID", raising=False)
    monkeypatch.delenv("MTS_STATE_PATH", raising=False)
    monkeypatch.delenv("MTS_BACKTEST", raising=False)

    events = []
    monkeypatch.setattr(
        tmf_spread, "_append_event",
        lambda event, **payload: events.append((event, payload)),
    )
    monkeypatch.setattr(tmf_spread, "_write_mts_state", lambda **kwargs: None)
    monkeypatch.setattr(tmf_spread, "_write_mts_telemetry", lambda **kwargs: None)

    bar = _bar()
    context = StrategyContext(
        position=PositionView(size=0),
        market=MarketData(ticker="TMF", last_bar=bar),
        config={"ticker": "TMF", "params": dict(ENTRY_PARAMS)},
    )
    strategy = TMFSpread()
    strategy.init(context)
    strategy._has_position = False
    strategy._lifecycle = "IDLE"
    strategy._trade_id = "mts-entry-gate-skip-test"
    return strategy, context, events


def _entry_skip_events(events):
    return [(name, payload) for name, payload in events if name == "ENTRY_GATE_SKIP"]


def test_t1_submitting_without_position_records_mts_busy(tmp_path, monkeypatch):
    strategy, context, events = _strategy(tmp_path, monkeypatch)
    strategy._lifecycle = "SUBMITTING"

    result = strategy.on_bar(context)

    assert result is None
    rows = _entry_skip_events(events)
    assert len(rows) == 1
    payload = rows[0][1]
    assert payload["skip_reason"] == "MTS_BUSY"
    assert payload["lifecycle"] == "SUBMITTING"
    assert payload["has_position"] is False
    assert payload["spread_z"] == -4.0
    assert payload["entry_z"] == 3.0
    assert payload["atr"] == 20.0
    assert payload["near_close"] == 39534.0
    assert payload["far_close"] == 39698.0
    assert payload["near_side"] in (None, "LONG", "SHORT")
    assert payload["far_side"] in (None, "LONG", "SHORT")
    assert payload["trade_id"] == "mts-entry-gate-skip-test"
    assert {"skip_reason", "lifecycle", "has_position", "spread_z", "entry_z",
            "atr", "near_close", "far_close", "near_side", "far_side", "trade_id"} <= payload.keys()


def test_t2_three_repeated_bars_are_rate_limited(tmp_path, monkeypatch):
    strategy, context, events = _strategy(tmp_path, monkeypatch)
    strategy._lifecycle = "SUBMITTING"

    for offset in range(3):
        bar = _bar(context.market.last_bar["timestamp"] + timedelta(seconds=offset * 5))
        strategy.on_bar(StrategyContext(
            position=context.position,
            market=MarketData(ticker="TMF", last_bar=bar),
            config=context.config,
        ))

    assert len(_entry_skip_events(events)) == 1


def test_t3_skip_reason_transition_emits_immediately(tmp_path, monkeypatch):
    strategy, _, events = _strategy(tmp_path, monkeypatch)
    strategy._record_entry_gate_skip("GATE_A", _bar())
    strategy._record_entry_gate_skip("GATE_B", _bar())

    rows = _entry_skip_events(events)
    assert [payload["skip_reason"] for _, payload in rows] == ["GATE_A", "GATE_B"]


def test_t4_triggered_entry_keeps_entry_audit_without_skip(tmp_path, monkeypatch):
    strategy, context, events = _strategy(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "core.channel_safety.get_safety_state",
        lambda: type("Safety", (), {"entry_allowed": lambda self, ticker: True,
                                    "entry_blocked_reason": ""})(),
    )
    monkeypatch.setattr(tmf_spread, "_maintenance_entry_lock_active", lambda: False)

    signal = strategy.on_bar(context)

    assert signal is not None
    assert signal.action in ("BUY_NEAR_SELL_FAR", "SELL_NEAR_BUY_FAR")
    assert len(_entry_skip_events(events)) == 0
    audits = [(name, payload) for name, payload in events if name == "ENTRY_AUDIT"]
    assert len(audits) == 1
    assert audits[0][1]["action"] == signal.action


def test_t5_backtest_mode_does_not_write_event_file(tmp_path, monkeypatch):
    strategy, _, _ = _strategy(tmp_path, monkeypatch)
    event_file = tmp_path / "mts_spread_events.jsonl"
    monkeypatch.setattr(tmf_spread, "_MTS_EVENT_LOG", str(event_file))
    monkeypatch.setattr(tmf_spread, "_append_event", append_event)
    monkeypatch.setenv("MTS_BACKTEST", "1")

    strategy._set_entry_gate_skip("MTS_BUSY", bar=_bar())

    assert not event_file.exists()
