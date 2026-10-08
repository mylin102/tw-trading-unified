# 2026-10-08 Codex: actual strategy writer/reader and on_bar Z persistence.
import json
from datetime import datetime

import pytest

from core.strategy_context import MarketData, PositionView, StrategyContext
from strategies.plugins.futures.active import tmf_spread as mod
from tests.strategies.test_tmf_spread_atr import _make_bar


@pytest.fixture
def state_case(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(mod, "_get_state_file_path", lambda: path)
    monkeypatch.setattr(mod, "_MTS_FILL_LOG", str(tmp_path / "fills.jsonl"))
    monkeypatch.setattr(mod, "_MTS_EVENT_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(mod, "_PENDING_STATE_REWRITE", None)
    monkeypatch.delenv("MTS_BACKTEST", raising=False)
    config = {"ticker": "TMF"}
    def create():
        strategy = mod.TMFSpread()
        strategy.init(StrategyContext(
            market=MarketData(last_bar={}, ticker="TMF"),
            position=PositionView(size=0), config=config))
        return strategy
    return create, path, config


def enter(create, z, trade_id="Z-STATE"):
    strategy = create()
    strategy.sync_position(trade_id, "LONG", 49854., 50035., entry_spread_z=z)
    strategy.write_state("OPEN", "confirmed entry", spread_z=z,
                         near_last=49854., far_last=50035.)
    return strategy


@pytest.mark.parametrize("entry", [-4.656461385288359, 0.0, 4.656461385288359])
def test_entry_snapshot_survives_current_write_and_restore(state_case, entry):
    create, path, _ = state_case
    strategy = enter(create, entry)
    strategy.write_state("OPEN", "new market mark", spread_z=1.25,
                         near_last=49854., far_last=50035.)
    state = strategy._read_mts_state()
    assert state["entry_spread_z"] == round(entry, 2)
    assert state["current_spread_z"] == 1.25
    restored = create()
    assert restored._restore_position_state() is True
    assert restored._entry_spread_z == round(entry, 2)
    restored.write_state("OPEN", "after restore", spread_z=0.0)
    assert json.loads(path.read_text())["entry_spread_z"] == round(entry, 2)


@pytest.mark.parametrize("current,expected", [(0.0, 0.0), ("", None), (float("nan"), None), (None, None)])
def test_current_writer_replaces_old_value_including_unknown(state_case, current, expected):
    create, path, _ = state_case
    strategy = enter(create, -4.5)
    strategy.write_state("OPEN", "known market mark", spread_z=2.0)
    try:
        strategy.write_state("OPEN", "next market mark", spread_z=current)
    except (TypeError, ValueError) as exc:
        assert False, f"present blank/NaN Z must persist as unknown, not raise: {exc}"
    state = json.loads(path.read_text())
    assert state["entry_spread_z"] == -4.5
    assert state["current_spread_z"] == expected
    assert state["spread_z"] == expected


@pytest.mark.parametrize("current,expected", [(1.25, 1.25), (0.0, 0.0), ("", None), (float("nan"), None), (None, None)])
def test_on_bar_heartbeat_persists_current_z_without_rewriting_entry(state_case, current, expected):
    create, path, config = state_case
    strategy = enter(create, -4.5)
    bar = _make_bar(near_close=49854., far_close=50035.,
                    near_high=49854., near_low=49854.,
                    far_high=50035., far_low=50035., spread_z=current)
    result = strategy.on_bar(StrategyContext(
        market=MarketData(last_bar=bar, ticker="TMF"),
        position=PositionView(size=1), config=config))
    assert result is None
    state = json.loads(path.read_text())
    assert state["entry_spread_z"] == -4.5
    assert state["current_spread_z"] == expected
    assert state["spread_z"] == expected
    assert state["has_position"] is True


@pytest.mark.parametrize("legacy", ["", float("nan"), None])
def test_legacy_unknown_entry_roundtrips_without_today_backfill(state_case, legacy):
    create, path, _ = state_case
    strategy = enter(create, -4.5)
    state = json.loads(path.read_text())
    state["entry_spread_z"] = legacy  # Present field, real blank/NaN shape.
    path.write_text(json.dumps(state))
    restored = create()
    assert restored._restore_position_state() is True
    assert restored._entry_spread_z is None
    restored.write_state("OPEN", "today's mark is not historical entry", spread_z=6.25)
    state = restored._read_mts_state()
    assert state["entry_spread_z"] is None
    assert state["current_spread_z"] == 6.25


def test_flat_and_new_trade_reset_entry_snapshot(state_case):
    create, path, _ = state_case
    strategy = enter(create, -4.5, "OLD-Z")
    strategy._reset("confirmed flat", exit_ts=datetime.now())
    assert strategy._entry_spread_z is None
    assert json.loads(path.read_text())["entry_spread_z"] is None
    strategy.sync_position("NEW-Z", "SHORT", 49854., 50035., entry_spread_z=0.)
    strategy.write_state("OPEN", "new confirmed trade", spread_z=2.5)
    state = strategy._read_mts_state()
    assert state["trade_id"] == "NEW-Z"
    assert state["entry_spread_z"] == 0.
    assert state["current_spread_z"] == 2.5
