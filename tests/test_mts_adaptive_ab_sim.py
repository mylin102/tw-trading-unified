"""2026-10-20 Hermes Agent: executable BBO-only A/B simulation contract."""
from datetime import datetime
from zoneinfo import ZoneInfo
import json

import pytest

from strategies.plugins.futures.active.mts_adaptive_ab import (
    AB_SIM_MAX_WAIT_SECS,
    advance_ab_simulation,
    begin_ab_simulation,
    log_ab_sim_pending,
    recover_ab_simulations,
)
from strategies.plugins.futures.active import tmf_spread
from strategies.plugins.futures.active.tmf_spread import TMFSpread


TW = ZoneInfo("Asia/Taipei")


def epoch(hour, minute=0, second=0):
    return datetime(2026, 10, 20, hour, minute, second, tzinfo=TW).timestamp()


def bar(ts, *, near_bid=100, near_ask=101, far_bid=90, far_ask=91,
        near_age=100, far_age=100, last=100):
    return {
        "timestamp": datetime.fromtimestamp(ts, TW),
        "near_bid": near_bid, "near_ask": near_ask,
        "far_bid": far_bid, "far_ask": far_ask,
        "near_bidask_age_ms": near_age, "far_bidask_age_ms": far_age,
        "near_entry_bbo_state": "OK", "far_entry_bbo_state": "OK",
        "near_close": last, "far_close": last, "close": last,
    }


def begin(*, side="SELL", leg="NEAR", decision_ts=None, limit=100.2, quote=None):
    return begin_ab_simulation(
        pair_id="pair-1", leg=leg, side=side,
        decision_ts=epoch(10) if decision_ts is None else decision_ts,
        adaptive_price=limit, bar=bar(epoch(10)) if quote is None else quote,
        point_value=10,
    )


def test_sell_requires_subsequent_bid_touch_and_ignores_last():
    pending, outcome = begin(limit=100)
    assert outcome is None
    assert advance_ab_simulation(pending, bar(epoch(10, 1), near_bid=99, last=100), epoch(10, 1)) is None
    outcome = advance_ab_simulation(pending, bar(epoch(10, 2), near_bid=100), epoch(10, 2))
    assert outcome["fill_status"] == "FILLED"
    assert outcome["adaptive_fill_px"] == 100


def test_buy_requires_subsequent_ask_touch_and_ignores_last():
    pending, outcome = begin(side="BUY", limit=100)
    assert outcome is None
    assert advance_ab_simulation(pending, bar(epoch(10, 1), near_ask=101, last=100), epoch(10, 1)) is None
    outcome = advance_ab_simulation(pending, bar(epoch(10, 2), near_ask=100), epoch(10, 2))
    assert outcome["fill_status"] == "FILLED"
    assert outcome["adaptive_fill_px"] == 100


def test_timeout_uses_current_executable_bbo():
    pending, _ = begin(limit=105)
    outcome = advance_ab_simulation(
        pending, bar(epoch(10, 5), near_bid=98), epoch(10) + AB_SIM_MAX_WAIT_SECS,
    )
    assert outcome["fill_status"] == "TIMEOUT_MARKET"
    assert outcome["adaptive_fill_px"] == 98


def test_session_close_uses_last_valid_bbo():
    pending, _ = begin(decision_ts=epoch(13, 44), quote=bar(epoch(13, 44), near_bid=99), limit=105)
    outcome = advance_ab_simulation(pending, bar(epoch(13, 45), near_bid=98), epoch(13, 45))
    assert outcome["fill_status"] == "SESSION_END_MARKET"
    assert outcome["adaptive_fill_px"] == 98
    pending, _ = begin(decision_ts=epoch(4, 59), quote=bar(epoch(4, 59)), limit=105)
    outcome = advance_ab_simulation(pending, bar(epoch(5)), epoch(5))
    assert outcome["fill_status"] == "SESSION_END_MARKET"


def test_missing_or_stale_decision_bbo_is_invalid():
    pending, outcome = begin(quote=bar(epoch(10), near_age=501))
    assert pending is None
    assert outcome["fill_status"] == "INVALID_BBO"
    pending, outcome = begin(quote={"near_bid": 100, "near_ask": 101})
    assert pending is None
    assert outcome["fill_status"] == "INVALID_BBO"
    pending, outcome = begin(leg="FAR", side="BUY", quote=bar(epoch(10), far_age=1501))
    assert pending is None
    assert outcome["fill_status"] == "INVALID_BBO"


@pytest.mark.parametrize(("side", "raw", "expected"), [
    ("SELL", 100.2, 101), ("BUY", 100.8, 100),
])
def test_adaptive_limit_rounds_conservatively(side, raw, expected):
    pending, outcome = begin(side=side, limit=raw)
    assert outcome is None
    assert pending["adaptive_limit"] == expected


@pytest.mark.parametrize(("side", "adaptive", "baseline", "expected"), [
    ("SELL", 105, 100, 5), ("BUY", 95, 100, 5),
    ("SELL", 95, 100, -5), ("BUY", 105, 100, -5),
])
def test_diff_sign_uses_exit_side(side, adaptive, baseline, expected):
    pending, _ = begin(side=side, limit=adaptive)
    pending["baseline_px"] = baseline
    from strategies.plugins.futures.active.mts_adaptive_ab import finish_ab_simulation
    outcome = finish_ab_simulation(pending, "FILLED", adaptive, epoch(10, 1))
    assert outcome["diff_points"] == expected
    assert outcome["diff_twd"] == expected * 10


def test_strategy_mount_never_calls_order_manager_or_changes_baseline_state(tmp_path, monkeypatch):
    class OrderManagerSpy:
        called = False

        def __getattr__(self, name):
            self.called = True
            raise AssertionError(f"order manager touched: {name}")

    spy = OrderManagerSpy()
    pending, _ = begin(limit=100)
    strategy = object.__new__(TMFSpread)
    strategy._adaptive_sim_pending = pending
    strategy._adaptive_sim_recovery_checked = True
    strategy._order_mgr = spy
    strategy._lifecycle = "OPEN"
    strategy._hvwap_baseline_clear_tick = True
    monkeypatch.setattr(tmf_spread, "_MTS_EVENT_LOG", str(tmp_path / "events.jsonl"))
    outcome = strategy._hvwap_ab_sim_tick(
        bar(epoch(10, 1), near_bid=100), datetime.fromtimestamp(epoch(10, 1), TW),
    )
    assert outcome["fill_status"] == "FILLED"
    assert strategy._lifecycle == "OPEN"
    assert strategy._hvwap_baseline_clear_tick is True
    assert spy.called is False


def test_restart_recovery_marks_unfinished_pair_abandoned(tmp_path):
    pending, _ = begin(limit=105)
    log_ab_sim_pending(pending, tmp_path)
    outcomes = recover_ab_simulations(tmp_path)
    assert len(outcomes) == 1
    assert outcomes[0]["pair_id"] == "pair-1"
    assert outcomes[0]["fill_status"] == "ABANDONED_RESTART"
    rows = [json.loads(line) for line in next(tmp_path.glob("*.jsonl")).read_text().splitlines()]
    assert [row["event_type"] for row in rows] == ["AB_SIM_PENDING", "AB_SIM_OUTCOME"]

