"""2026-10-06 Codex: actual tick -> ledger -> real reset -> state writer."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strategies.futures.monitor import FuturesMonitor
from strategies.futures.mts_ledger_authority import MtsLedgerProjection
from strategies.plugins.futures.active import tmf_spread


def build(tmp_path, monkeypatch, *, mode="paper", memory_open=True,
          missing_ledger=False, other_trade=False, unclosed_far=False,
          missing_entry=False, live_allowed=False):
    trade = "mts-auto-134000-323"
    rows = [
        dict(trade_id=trade, leg="NEAR", side="LONG", qty=1, price=49915,
             fill_type="ENTRY", timestamp="2026-10-05T13:40:00.623279"),
        dict(trade_id=trade, leg="FAR", side="SHORT", qty=1, price=50094,
             fill_type="ENTRY", timestamp="2026-10-05T13:40:00.639931"),
        dict(trade_id=trade, leg="NEAR", side="SELL", qty=1, price=50068,
             fill_type="COMBINED_EXIT", position_effect="CLOSE",
             timestamp="2026-10-05T16:49:41.496116"),
    ]
    if not unclosed_far:
        rows.append(dict(trade_id=trade, leg="FAR", side="BUY", qty=1,
                         price=50264, fill_type="COMBINED_EXIT",
                         position_effect="CLOSE", timestamp="2026-10-05T16:49:41.498056"))
    if missing_entry:
        rows = [r for r in rows if r["fill_type"] != "ENTRY"]
    for row in rows:
        row.update(mode="paper", paper_fill_id="PF-" + row["timestamp"] + "-" + row["leg"],
                   execution_source="PAPER_COMBO_FILL" if row["fill_type"] == "ENTRY" else "PAPER_EXIT_FILL")
    ledger = tmp_path / "fills.jsonl"
    if not missing_ledger:
        ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    state = tmp_path / "state.json"
    state.write_text(json.dumps(dict(has_position=True, trade_id="different" if other_trade else trade,
                                    ticker="TMF", near_side="SHORT", far_side="LONG",
                                    near_entry=49915, far_entry=50094,
                                    near_upl=-1880, far_upl=2020, total_upl=140)))
    monkeypatch.setattr(tmf_spread, "_get_state_file_path", lambda: str(state))
    monkeypatch.setattr(tmf_spread, "_MTS_FILL_LOG", str(ledger))
    monkeypatch.setattr(tmf_spread, "_abandoned_trade_ids", lambda: set())
    monkeypatch.delenv("MTS_BACKTEST", raising=False)
    monkeypatch.setattr("strategies.futures.monitor._mts_position_state_path", lambda: state)
    monkeypatch.setattr("strategies.futures.monitor.is_taifex_futures_market_open", lambda: False)
    strat = tmf_spread.TMFSpread.__new__(tmf_spread.TMFSpread)
    strat._ticker, strat._trade_id = "TMF", trade
    strat._has_position, strat._released_leg = memory_open, None
    strat._mts_recovery_state = tmf_spread.RecoveryState.RECOVERED
    strat._near_side, strat._far_side = "SHORT", "LONG"
    strat._near_entry, strat._far_entry = 49915, 50094
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon.cfg, mon.ticker = {"mts": {"strategy": "tmf_spread"}}, "TMF"
    mon.far_contract, mon.api, mon.dry_run, mon.live_trading = None, None, True, False
    mon.trader = SimpleNamespace(position=0)
    mon._registry = {"tmf_spread": strat}
    mon._execution_context = SimpleNamespace(requested_mode=mode,
                                             effective_mode="paper_active" if mode == "paper" else "live_quarantined",
                                             live_order_allowed=live_allowed)
    mon._ledger_projection = MtsLedgerProjection(str(ledger), source="PAPER")
    mon._oco_reconciled = True
    monkeypatch.setattr(mon, "_reconcile_pending_mts_combos", MagicMock())
    monkeypatch.setattr(mon, "_check_broker_snapshot_request", lambda: False)
    def broker(strategy):
        if mode == "paper":
            raise AssertionError("PAPER recovery must never query broker")
        return None
    monkeypatch.setattr(mon, "_refresh_live_broker_authority", broker)
    return mon, strat, state, ledger


@pytest.mark.parametrize("memory_open", [True, False])
def test_paper_tick_clears_closed_trade_state_without_broker(tmp_path, monkeypatch, memory_open):
    mon, strat, state, ledger = build(tmp_path, monkeypatch, memory_open=memory_open)
    before = ledger.read_bytes()
    mon._mts_tick()
    published = json.loads(state.read_text())
    assert published["has_position"] is False
    assert published["near_status"] == published["far_status"] == "CLOSED"
    assert published["near_upl"] == published["far_upl"] == published["total_upl"] == 0
    assert strat._has_position is False
    assert ledger.read_bytes() == before  # reset must not fabricate another exit


@pytest.mark.parametrize("options", [dict(mode="live"), dict(mode="unknown"),
                                   dict(missing_ledger=True), dict(other_trade=True),
                                   dict(unclosed_far=True), dict(missing_entry=True),
                                   dict(live_allowed=True)])
def test_paper_reset_does_not_weaken_unknown_live_or_open_authority(tmp_path, monkeypatch, options):
    mon, strat, state, _ = build(tmp_path, monkeypatch, **options)
    before = state.read_bytes()
    mon._mts_tick()
    assert state.read_bytes() == before
    assert strat._has_position is True
