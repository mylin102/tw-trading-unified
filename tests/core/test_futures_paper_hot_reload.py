"""2026-10-02 Codex: actual PAPER reload must publish the applied config identity."""
import ast
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.core.test_mts_mode_data_isolation import isolated_runtime, _monitor


def _change_config(mon, *, live=False):
    path = Path(mon.config_path)
    path.write_text("ticker: TMF\nlive_trading: " + str(live).lower() +
                    "\ncooldown_bars: 17\nmonitoring:\n  use_order_manager: true\n")
    mon._config_mtime = 0
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_actual_paper_reload_publishes_applied_hash(isolated_runtime, monkeypatch):
    mon = _monitor(isolated_runtime, monkeypatch)
    prior = mon._execution_context
    mon.client = SimpleNamespace(_execution_context=prior)
    expected = _change_config(mon)
    assert expected != prior.config_hash
    mon._reload_config_if_changed()
    ctx = mon._execution_context
    assert mon.cooldown_bars == 17
    assert ctx.config_hash == expected
    assert mon.client._execution_context is ctx
    assert mon.order_mgr.execution_context is ctx
    assert ctx.requested_mode == "paper" and ctx.effective_mode == "paper_active"
    assert not ctx.live_order_allowed
    assert ctx.session_id == prior.session_id
    persisted = json.loads((isolated_runtime / "execution_context.json").read_text())
    assert persisted["config_hash"] == expected
    # Execute the actual dashboard classifier without starting Streamlit.
    source = Path(__file__).resolve().parents[2] / "ui/dashboard.py"
    node = next(n for n in ast.parse(source.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == "summarize_execution_context")
    ns = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), ns)
    truth = ns["summarize_execution_context"](
        persisted, {"futures.yaml (Paper baseline)": expected})
    assert truth["is_paper_runtime"] and truth["warning"] == ""


@pytest.mark.parametrize("requested,effective,allowed,live_config", [
    ("live", "live_ready", True, True),
    ("paper", "paper_draining", False, False),
    ("paper", "paper_active", False, True),
])
def test_actual_reload_does_not_rebind_other_authority(
        isolated_runtime, monkeypatch, requested, effective, allowed, live_config):
    mon = _monitor(isolated_runtime, monkeypatch)
    mon._execution_context = replace(mon._execution_context, requested_mode=requested,
                                     effective_mode=effective, live_order_allowed=allowed)
    prior = mon._execution_context
    mon.client = SimpleNamespace(_execution_context=prior)
    mon.order_mgr.execution_context = prior
    _change_config(mon, live=live_config)
    mon._reload_config_if_changed()
    assert mon._execution_context is prior
    assert mon.client._execution_context is prior
    assert mon.order_mgr.execution_context is prior


def test_actual_reload_persist_failure_keeps_prior_hash(isolated_runtime, monkeypatch):
    mon = _monitor(isolated_runtime, monkeypatch)
    prior = mon._execution_context
    mon.client = SimpleNamespace(_execution_context=prior)
    path = isolated_runtime / "execution_context.json"
    original = path.read_bytes()
    _change_config(mon)
    from core import execution_context_state
    def fail_persist(*args, **kwargs):
        raise OSError("fixture write refusal")
    monkeypatch.setattr(execution_context_state, "persist_execution_context", fail_persist)
    mon._reload_config_if_changed()
    assert mon._execution_context is prior
    assert mon.client._execution_context is prior
    assert mon.order_mgr.execution_context is prior
    assert path.read_bytes() == original
    assert mon._config_mtime == 0  # failed publication must remain retryable


@pytest.mark.parametrize("signal,prices,expected", [
    ("BUY", (48150.0, 48090.0), 48150.0),
    ("SELL", (47850.0, 47910.0), 47850.0),
])
def test_actual_strategy_tick_uses_paper_favorable_extreme(
        isolated_runtime, monkeypatch, signal, prices, expected):
    # 2026-10-02 Codex: full tick callsite and real PaperTrader, stop before order execution.
    import time
    from datetime import datetime
    import pandas as pd
    from strategies.futures import monitor as fm
    from core import exit_engine
    mon = _monitor(isolated_runtime, monkeypatch)
    mon.cfg["mts"] = {"enabled": False}
    mon.dry_run = False
    mon.is_trading_ready = True
    mon.last_tick_at = time.time()
    mon.manual_trade_flag_path = str(isolated_runtime / "no-manual-flag")
    mon.contract = SimpleNamespace(delivery_date="2099/01/01")
    mon._is_settlement_day = lambda _: False
    mon._tmf_feed_age_secs = lambda: 0
    mon._trend_hold_active = lambda *args: False
    mon._scout_entry_bar = -1
    for name in ("_process_live_upl_refresh_command", "_reconcile_exit_only_authorization",
                 "_run_mts_watchdog", "_sync_mts_status", "_check_futures_contract_staleness",
                 "_refresh_runtime_status", "_check_tick_api_consistency", "_hourly_no_trade_audit",
                 "_update_mtf_snapshot"):
        setattr(mon, name, lambda *args, **kwargs: None)
    mon._check_canonical_freshness = lambda _: []
    mon._periodic_backfill_bars = lambda: None
    monkeypatch.setattr("core.date_utils.is_day_session", lambda _: True)
    monkeypatch.setattr("core.date_utils.is_night_session", lambda _: False)
    monkeypatch.setattr(fm, "get_taifex_futures_hhmm", lambda: 1000)
    monkeypatch.setattr(fm, "get_taifex_futures_session_type", lambda: "day")
    monkeypatch.setattr(fm, "calculate_futures_squeeze", lambda frame, **kwargs: frame)
    monkeypatch.setattr(fm, "attach_bar_metadata", lambda frame: frame)
    now = datetime.now()
    result = mon.trader.execute_signal(signal, 48000.0, now)
    assert result and mon.trader.position == (1 if signal == "BUY" else -1)
    assert not hasattr(mon.trader, "peak_price") and not hasattr(mon.trader, "floor_price")
    observed = []
    class ReachedExitBoundary(Exception):
        pass
    def observe_exit(trade_state, context, market):
        observed.append(dict(trade_state))
        assert trade_state["side"] == ("LONG" if signal == "BUY" else "SHORT")
        assert trade_state["peak_price"] == expected
        raise ReachedExitBoundary
    monkeypatch.setattr(exit_engine, "should_exit", observe_exit)
    for price in prices:
        row = {"Open": price, "High": price, "Low": price, "Close": price,
               "Volume": 1.0, "vwap": price, "atr": 50.0, "atr_raw": 50.0,
               "momentum": 0.0, "sqz_on": False, "trading_day": now.strftime("%Y-%m-%d")}
        frame = pd.DataFrame([row, row], index=pd.date_range(now, periods=2, freq="5min"))
        mon._get_tick_bars_df = lambda: frame
        monkeypatch.setattr(fm, "build_preferred_canonical_bar_frames",
                            lambda *args, **kwargs: ({"5m": frame}, {"source": "tick-5m"}))
        with pytest.raises(ReachedExitBoundary):
            mon._strategy_tick()
        assert mon.trader.position == (1 if signal == "BUY" else -1)
        assert mon.trader._best_price == expected
    assert len(observed) == 2
