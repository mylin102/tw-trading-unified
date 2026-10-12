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


def test_real_manager_simulator_callback_settles_executable_exit(paper_exit, monkeypatch, tmp_path):
    # 2026-10-12 Codex: full local order/fill/settlement seam, no broker calls.
    import json
    from core.order_management.order_manager import OrderManager
    from core.order_management.paper_fill import PaperFillSimulator
    from core.order_intent_gateway import OrderIntentGateway
    from strategies.plugins.futures.active import tmf_spread as mod
    mon, strat, bar = paper_exit
    monkeypatch.setenv("TRADING_FUTURES_MODE", "paper")
    mon.ticker = "TMF"
    mon.order_mgr = OrderManager(mode="paper")
    mon.paper_fill_sim = PaperFillSimulator(mon.order_mgr)
    mon.order_mgr.set_simulator(mon.paper_fill_sim)
    mon._order_intent_gateway = OrderIntentGateway()
    mon._applied_lifecycle_deals = set()
    mon._combined_exit_trackers = {}
    mon._mts_pending_fills = {}
    mon._registry = {"tmf_spread": strat}
    mon.trader = MagicMock(position=0)
    for name in ("_check_mts_multi_leg_fill", "_maybe_complete_emergency_command",
                 "_enter_post_exit_reconciliation", "_maybe_finalize_exit_only_reconciliation"):
        monkeypatch.setattr(mon, name, MagicMock())
    state = tmp_path / "source_state.json"
    state.write_text(json.dumps({"trade_id": strat._trade_id,
                                "near_side": "SHORT", "far_side": "LONG",
                                "near_entry": 44005.0, "far_entry": 44090.0}))
    fills = tmp_path / "fills.jsonl"
    monkeypatch.setattr(mod, "_get_state_file_path", lambda: str(state))
    monkeypatch.setattr(mod, "_MTS_FILL_LOG", str(fills))
    mon._wire_order_callbacks()
    mon._submit_mts_order_signal(Signal("EXIT", "TMF_COMBINED_EXIT"), strat, bar, ts=None)
    rows = [json.loads(line) for line in fills.read_text().splitlines()]
    exits = [row for row in rows if row["fill_type"] == "COMBINED_EXIT"]
    assert [(row["leg"], row["side"], row["price"]) for row in exits] == [
        ("NEAR", "BUY", 44003.0), ("FAR", "SELL", 44096.0)]
    assert sum(row["fill_type"] == "COMBINED_EXIT_COMPLETED" for row in rows) == 1
    assert strat._has_position is False
    assert mon.paper_fill_sim.get_pending_count() == 0
    mon._enter_post_exit_reconciliation.assert_called_once()
