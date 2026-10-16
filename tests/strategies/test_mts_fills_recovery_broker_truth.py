import json


def _strategy_with_open_fills(tmp_path, monkeypatch, broker_truth):
    from strategies.plugins.futures.active import tmf_spread as mod

    fills = tmp_path / "fills.jsonl"
    rows = [
        {"trade_id": "trade-open", "fill_type": "ENTRY", "leg": "NEAR",
         "side": "SELL", "qty": 1, "price": 46288.0,
         "timestamp": "2026-09-14T09:00:00"},
        {"trade_id": "trade-open", "fill_type": "ENTRY", "leg": "FAR",
         "side": "BUY", "qty": 1, "price": 46451.0,
         "timestamp": "2026-09-14T09:00:01"},
    ]
    fills.write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr(mod, "_MTS_FILL_LOG", str(fills))
    monkeypatch.setattr(mod, "_abandoned_trade_ids", lambda: set())

    events = []
    monkeypatch.setattr(
        mod, "_append_event",
        lambda event_type, **payload: events.append(
            {"event": event_type, **payload}),
    )
    strategy = mod.TMFSpread.__new__(mod.TMFSpread)
    strategy._has_position = False
    strategy._broker_truth_flat = broker_truth == "FLAT"
    strategy._fills_recovery_broker_truth = broker_truth
    strategy._read_mts_state = lambda: None
    return strategy, events


def test_broker_flat_refuses_open_fills_recovery(tmp_path, monkeypatch):
    strategy, events = _strategy_with_open_fills(
        tmp_path, monkeypatch, "FLAT")

    assert strategy._restore_from_fills_log() is False
    assert strategy._has_position is False
    assert events == [{
        "event": "MTS_FILLS_RECOVERY_REFUSED",
        "reason": "BROKER_FLAT",
        "broker_truth": "FLAT",
    }]


def test_matching_broker_position_allows_open_fills_recovery(
        tmp_path, monkeypatch):
    strategy, events = _strategy_with_open_fills(
        tmp_path, monkeypatch, "MATCHED_OPEN")

    assert strategy._restore_from_fills_log() is True
    assert strategy._has_position is True
    assert strategy._near_side == "SHORT"
    assert strategy._far_side == "LONG"
    assert strategy._near_entry == 46288.0
    assert strategy._far_entry == 46451.0
    assert not [e for e in events
                if e["event"] == "MTS_FILLS_RECOVERY_REFUSED"]


def test_unknown_broker_truth_refuses_open_fills_recovery(
        tmp_path, monkeypatch):
    strategy, events = _strategy_with_open_fills(
        tmp_path, monkeypatch, "UNKNOWN")

    assert strategy._restore_from_fills_log() is False
    assert strategy._has_position is False
    assert events == [{
        "event": "MTS_FILLS_RECOVERY_REFUSED",
        "reason": "BROKER_UNAVAILABLE",
        "broker_truth": "UNKNOWN",
    }]
