"""2026-11-10 Hermes Agent: regression for monitor.py:18202's undefined `lots`.

_strategy_tick()'s "[GSD Upgrade] Apply Dynamic Position Scaling" block read
`lots * edge_res["pos_scale"]` where `lots` was never defined anywhere in the
function (grep-verified). Every entry where edge_model.evaluate() returned
has_edge=True with a pos_scale != 1.0 -- i.e. every entry that reached this
far -- raised NameError, uncaught by _strategy_tick's own code (no enclosing
except around this block), which would have killed the strategy loop.

Harness pattern follows tests/strategies/test_futures_monitor_router_integration.py's
_make_monitor(): build a minimal FuturesMonitor via __new__ + the production
_strategy_tick()/._route_signal() methods, not a reimplementation.
"""
import types
from datetime import datetime

import pandas as pd
import pytest

from core.signal import Signal
from core.strategy_base import StrategyBase
from strategies.futures.monitor import FuturesMonitor


class _FixedSignal(StrategyBase):
    """Always returns the same BUY signal with a fixed starting quantity."""

    name = "fixed_signal_strategy"

    def __init__(self, quantity=2):
        self._quantity = quantity

    def init(self, context):
        pass

    def on_bar(self, context):
        return Signal(action="BUY", quantity=self._quantity, reason="unit-test signal")


class _FakeRegistry:
    def __init__(self, strategies):
        self._strategies = strategies

    def get(self, name):
        return self._strategies.get(name)


def _make_bar_frame(count=30):
    bar = {
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5,
        "Volume": 1_000_000.0, "atr": 20.0, "momentum": 5.0,
        "breakout_strength": 0.3, "volume_spike": 1.0, "trend_strength_raw": 0.1,
        "mom_state": "UP", "vwap": 100.0, "sqz_on": False,
    }
    df = pd.DataFrame([bar] * count)
    df.index = pd.date_range("2026-11-10 09:00:00", periods=count, freq="5min")
    return df


@pytest.fixture
def real_tick_monitor(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("TRADING_FUTURES_MODE", "paper")
    monkeypatch.setenv("AGENT_NAME", "codex")

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.dry_run = False
    monitor.live_trading = False
    monitor.ticker = "TMF"
    monitor.cfg = {"mts": {"enabled": False},
                    "debug_gate_bypass": {"enabled": True, "paper_only": True,
                                          "disable_entry_score_gate": True,
                                          "disable_volume_gate": True}}
    monitor.STRATEGY = {"active_strategy": "fixed_signal_strategy", "entry_score": 0,
                        "volume_threshold": 0.0}
    monitor.RISK = {"stop_loss_pts": 60}
    monitor.ATR_MULT = 0
    monitor.counter_enabled = False
    monitor._last_exit_bar = None
    monitor._bar_counter = 1
    monitor._manual_trade_status = "READY"
    monitor.manual_trade_flag_path = str(tmp_path / "no_such_flag")
    monitor._registry = _FakeRegistry({"fixed_signal_strategy": _FixedSignal(quantity=2)})
    monitor._initialized_strategy_names = set()
    monitor._use_order_manager = False
    monitor._skew_engine = None
    monitor.order_mgr = types.SimpleNamespace(get_pending=lambda: [])
    monitor.trader = types.SimpleNamespace(position=0, entry_price=0.0,
                                           current_stop_loss=None, unrealized_pnl=0.0)
    monitor.has_tp1_hit = False
    monitor.is_trading_ready = True
    monitor.active_strategy_name = None
    monitor._last_bar_context = {}
    monitor._was_stale = False
    monitor._current_bar = {"ts": datetime(2026, 11, 10, 9, 0, 0), "open": 100.0,
                            "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1_000_000.0}
    monitor.PB_ARGS = {}
    monitor.MONITOR = {}
    monitor.STALE_WARN_SECS = 120
    monitor.last_tick_at = __import__("time").time()
    monitor.previous_session_type = "day"
    monitor.session_type = "day"
    monitor._bars_since_session_open = 10
    monitor._bars_since_trade = 10
    monitor._circuit_breaker = types.SimpleNamespace(
        check=lambda **kw: types.SimpleNamespace(value="OK"),
        is_active=lambda: False, record_loss=lambda *a, **kw: None)
    monitor.consecutive_losses = 0
    monitor.cooldown_bars = 0
    monitor.cooldown_until = 0
    monitor.counter_exit_vwap = None
    monitor.last_processed_bar = None
    monitor.market_data = {monitor.ticker: {"close": 100.5}}
    monitor.MGMT = {}
    monitor._prev_sqz_on = False
    monitor._atr_trail_peak = None
    monitor._trend_hold_active = lambda *a, **kw: False
    monitor._vwap_violation_bars = 0
    monitor._far_current_bar = {}
    monitor._scout_entry_bar = 0
    monitor._scout_time_stop_bars = 0
    monitor._signals_rejected = 0
    monitor._last_entry_reason = None
    monitor._mts_tick = lambda *a, **kw: None
    monitor._save_bar = lambda *a, **kw: None
    monitor._cancel_all_pending_orders = lambda *a, **kw: None
    monitor._apply_trend_hold_trail = lambda *a, **kw: None
    monitor._execute_trade = lambda *a, **kw: None
    monitor._hourly_no_trade_audit = lambda *a, **kw: None
    monitor._mts_tag_bar_session = lambda bar: bar
    monitor._resubscribe_after_session_transition = lambda *a, **kw: None
    monitor._format_router_audit_note = lambda *a, **kw: ""
    monitor._check_futures_contract_staleness = lambda *a, **kw: None
    monitor._refresh_runtime_status = lambda *a, **kw: None
    monitor.feed_health = None
    monitor.adaptive = None
    monitor._get_tick_bars_df = lambda: _make_bar_frame()
    monitor._periodic_backfill_bars = lambda: None
    monitor._check_canonical_freshness = lambda df: []
    monitor._check_tick_api_consistency = lambda *a, **kw: None
    monitor._audit_signal = lambda *a, **kw: None
    monitor._reload_config_if_changed = lambda: None
    monitor._process_live_upl_refresh_command = lambda: None
    monitor._reconcile_exit_only_authorization = lambda: None
    monitor._run_mts_watchdog = lambda: None
    monitor._sync_mts_status = lambda: None

    from core.spread_loader import get_spread_loader
    monitor._spread_loader = get_spread_loader()
    monitor._spread_loaded = monitor._spread_loader.load_latest_csv(monitor.ticker)

    return monitor


def test_strategy_tick_position_scaling_no_nameerror(real_tick_monitor, monkeypatch):
    """The real callsite: _strategy_tick() must apply edge_model's pos_scale
    to the SIGNAL's own quantity, not to an undefined `lots`."""
    monitor = real_tick_monitor

    signal = Signal(action="BUY", quantity=2, reason="router-selected")

    class _FakeDecision:
        is_trade = True
        action = "ENTER"
        selected_strategy = "fixed_signal_strategy"

    _FakeDecision.signal = signal

    monkeypatch.setattr(monitor, "_route_signal",
                        lambda bar, session_regime, active_name=None:
                        (_FakeDecision(), {}, session_regime,
                         types.SimpleNamespace(regime="NORMAL")))

    import core.edge_model as edge_model_mod
    monkeypatch.setattr(edge_model_mod.edge_model, "evaluate",
                        lambda score, context, name: {
                            "has_edge": True, "pos_scale": 2.5, "rank": "A"})

    # Drive the exact production method (not a reimplementation). Everything
    # above the router call is stubbed via the fixture; the scaling block at
    # monitor.py:18202 is the code under test.
    monitor._strategy_tick()

    assert monitor.active_strategy_name == "fixed_signal_strategy"
    # 2 lots * pos_scale 2.5 -> 5. Pre-fix this line raised NameError on `lots`.
    assert signal.quantity == 5, f"pos_scale not applied to signal.quantity: {signal.quantity}"

