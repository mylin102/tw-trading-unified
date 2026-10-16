"""2026-10-06 Codex: real monitor.run PAPER-local startup authority."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from test_paper_flat_recovery import build


def prepare(tmp_path, monkeypatch, **options):
    mon, strat, state, ledger = build(tmp_path, monkeypatch, **options)
    mon.cfg["mts"]["enabled"] = True
    mon.dry_run, mon.POLL_INTERVAL, mon.order_mgr = False, 1, None
    mon.contract = SimpleNamespace(code="TMFJ6")
    mon.api = MagicMock()
    mon.api.list_positions.return_value = []
    monkeypatch.setattr(mon, "_refresh_runtime_status", MagicMock())
    monkeypatch.setattr(mon, "_rebuild_bars_from_raw_ticks", MagicMock())
    monkeypatch.setattr("threading.Thread", MagicMock())
    monkeypatch.setattr("core.diagnostic_engine.DiagnosticEngine", MagicMock())
    monkeypatch.setattr("strategies.futures.monitor.time.sleep", lambda _: None)
    def end_poll():
        mon._running = False
    monkeypatch.setattr(mon, "_strategy_tick", end_poll)
    return mon, strat, state, ledger


@pytest.mark.parametrize("memory_open", [True, False])
@pytest.mark.parametrize("market_open", [False, True])
def test_paper_run_uses_real_tick_and_writer_without_signal(tmp_path, monkeypatch, memory_open, market_open):
    mon, strat, state, ledger = prepare(tmp_path, monkeypatch, memory_open=memory_open)
    monkeypatch.setattr("strategies.futures.monitor.is_taifex_futures_market_open", lambda: market_open)
    # A startup empty bar must return before bar ingestion or order decisions.
    monkeypatch.setattr(mon, "_get_tick_bars_df", MagicMock(side_effect=AssertionError("startup must not trade")))
    before = ledger.read_bytes()
    mon.run()
    published = json.loads(state.read_text())
    assert published["has_position"] is False
    assert published["near_status"] == published["far_status"] == "CLOSED"
    assert published["near_upl"] == published["far_upl"] == published["total_upl"] == 0
    assert strat._has_position is False
    assert ledger.read_bytes() == before
    mon.api.list_positions.assert_not_called()
    mon._get_tick_bars_df.assert_not_called()


def test_paper_run_never_imports_broker_holdings(tmp_path, monkeypatch):
    mon, _, _, _ = prepare(tmp_path, monkeypatch)
    mon.run()
    mon.api.list_positions.assert_not_called()


@pytest.mark.parametrize("mode", ["live", "unknown"])
def test_nonpaper_run_preserves_broker_recovery(tmp_path, monkeypatch, mode):
    mon, strat, state, _ = prepare(tmp_path, monkeypatch, mode=mode)
    before = state.read_bytes()
    mon.run()
    mon.api.list_positions.assert_called_once_with(mon.api.futopt_account)
    assert state.read_bytes() == before
    assert strat._has_position is True


def test_missing_paper_history_stays_held_without_broker_fallback(tmp_path, monkeypatch):
    mon, strat, state, _ = prepare(tmp_path, monkeypatch, missing_ledger=True)
    before = state.read_bytes()
    mon.run()
    mon.api.list_positions.assert_not_called()
    assert state.read_bytes() == before
    assert strat._has_position is True
