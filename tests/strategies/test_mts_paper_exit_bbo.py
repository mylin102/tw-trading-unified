# 2026-10-12 Codex: real dispatcher must cross the BBO for PAPER exits.
from unittest.mock import MagicMock

import pytest

from core.signal import Signal
from tests.strategies.test_policy_j_combined_exit_execution import (
    DummyStrategy, make_mock_monitor,
)


@pytest.fixture
def paper_exit(monkeypatch, tmp_path):
    from strategies.futures import monitor as mod
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(mod, "is_taifex_futures_market_open", lambda: True)
    monkeypatch.setattr(mod, "_mts_intent_log_dir", lambda: str(tmp_path))
    monkeypatch.setattr(mod, "_mts_position_state_path", lambda: tmp_path / "state.json")
    mon = make_mock_monitor()
    mon.paper_fill_sim = MagicMock()
    mon._save_orders_file_wrapper = lambda: None
    mon.cfg = {"mts": {"params": {"max_quote_age_ms": 30000}}}
    return mon, DummyStrategy(), {
        "near_close": 44000.0, "far_close": 44100.0,
        "bbo_near_bid": 43998.0, "bbo_near_ask": 44003.0,
        "bbo_far_bid": 44096.0, "bbo_far_ask": 44106.0,
        "bbo_near_age_ms": 100.0, "bbo_far_age_ms": 200.0,
    }


def test_combined_paper_exit_crosses_ask_and_bid(paper_exit):
    mon, strat, bar = paper_exit
    mon._submit_mts_order_signal(Signal("EXIT", "TMF_COMBINED_EXIT"), strat, bar, ts=None)
    assert mon.order_mgr.submit.call_count == 2
    ticks = [c.args[0] for c in mon.paper_fill_sim.process_tick.call_args_list]
    assert [t.close for t in ticks] == [44003.0, 44096.0]


@pytest.mark.parametrize("key,value", [
    ("bbo_far_bid", None), ("bbo_far_bid", float("nan")),
    ("bbo_far_ask", 44090.0), ("bbo_far_age_ms", 30001.0),
    ("bbo_far_age_ms", None),
])
def test_bad_paper_exit_bbo_preserves_position_without_submission(paper_exit, key, value):
    mon, strat, bar = paper_exit
    bar[key] = value
    mon._submit_mts_order_signal(Signal("EXIT", "TMF_COMBINED_EXIT"), strat, bar, ts=None)
    assert mon.order_mgr.submit.call_count == 0
    assert mon.paper_fill_sim.process_tick.call_count == 0
    assert strat._has_position is True
