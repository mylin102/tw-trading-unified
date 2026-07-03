from types import SimpleNamespace

import pandas as pd

from core.signal import Signal
from core.strategy_base import StrategyBase
from strategies.futures.monitor import FuturesMonitor


class _FakeStrategy(StrategyBase):
    def __init__(self, name, signal=None):
        self._name = name
        self._signal = signal
        self.init_calls = 0

    @property
    def name(self) -> str:
        return self._name

    def init(self, context) -> None:
        self.init_calls += 1

    def on_bar(self, context):
        return self._signal


class _FakeRegistry:
    def __init__(self, strategies):
        self._strategies = strategies

    def get(self, name):
        return self._strategies.get(name)


def _make_bar(**overrides):
    bar = {
        "Open": 100.0,
        "High": 101.0,
        "Low": 99.0,
        "Close": 100.5,
        "Volume": 1000.0,
        "session": 2,
        "adx": 22.0,
        "breakout_strength": 0.20,
        "price_vs_vwap": -0.004,
        "trend_strength_raw": -0.0015,
        "sqz_on": False,
        "in_pb_zone": True,
        "in_bear_pb_zone": True,
        "in_bull_pb_zone": False,
        "bull_align": False,
        "bullish_align": False,
        "bear_align": True,
        "bearish_align": True,
        "opening_bearish": True,
        "opening_bullish": False,
        "ema_fast": 101.0,
        "ema_slow": 102.0,
        "vwap": 101.0,
        "atr": 20.0,
    }
    bar.update(overrides)
    return bar


def _make_frame(count=30, **overrides):
    df = pd.DataFrame([_make_bar(**overrides) for _ in range(count)])
    df.index = pd.date_range("2026-04-22 18:00:00", periods=count, freq="5min")
    return df


def _make_monitor(registry, *, pending_orders=None, ticker="TMF"):
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._registry = registry
    monitor._use_order_manager = True
    monitor._skew_engine = None  # [Skew Integration] prevent AttributeError
    monitor.order_mgr = SimpleNamespace(get_pending=lambda: pending_orders or [])
    monitor.ticker = ticker
    monitor.trader = SimpleNamespace(
        position=0,
        entry_price=0.0,
        current_stop_loss=None,
        unrealized_pnl=0.0,
    )
    monitor.has_tp1_hit = False
    monitor.cfg = {"strategy": {}, "params": {}}
    monitor._bar_counter = 12
    monitor._initialized_strategy_names = set()
    # [V-Model] SpreadLoader mock for enrich_bar call
    from core.spread_loader import get_spread_loader
    monitor._spread_loader = get_spread_loader()
    # 2026-06-26 Gemini CLI: Pass active ticker to prevent loading default MXF CSV files
    monitor._spread_loaded = monitor._spread_loader.load_latest_csv(monitor.ticker)

    # 2026-06-23 Gemini CLI: map deprecated _route_entry_signal to _route_signal in tests
    import types
    from core.market_regime import MarketRegime

    def fake_route_entry_signal(self, bar, df_5m, df_15m, ts, active_name=None):
        self._last_processed_data = {"5m": df_5m, "15m": df_15m}
        bar_copy = bar.copy()
        if hasattr(bar_copy, "name"):
            try:
                bar_copy.name = pd.Timestamp.now()
            except Exception:
                pass
        bar_copy["timestamp"] = pd.Timestamp.now()
        return self._route_signal(bar_copy, MarketRegime.NEUTRAL, active_name=active_name)

    monitor._route_entry_signal = types.MethodType(fake_route_entry_signal, monitor)
    return monitor


def test_route_entry_signal_uses_router_selected_fallback_strategy():
    adaptive = _FakeStrategy("adaptive_orb", None)
    spring = _FakeStrategy(
        "spring_upthrust",
        Signal("BUY", "SPRING", stop_loss=95.0, confidence=0.7),
    )
    monitor = _make_monitor(
        _FakeRegistry({"adaptive_orb": adaptive, "spring_upthrust": spring})
    )
    df_5m = _make_frame()
    df_15m = df_5m.copy()
    last_5m = df_5m.iloc[-1]

    decision, ctx, session_regime, bar_regime = monitor._route_entry_signal(
        last_5m, df_5m, df_15m, pd.Timestamp("2026-04-22 20:00:00"), "adaptive_orb"
    )

    assert decision.is_trade
    assert decision.selected_strategy == "spring_upthrust"
    assert decision.signal is not None
    assert decision.signal.reason == "SPRING"
    assert adaptive.init_calls == 0  # Blocked by regime policy
    assert spring.init_calls == 1
    assert str(session_regime).startswith("MarketRegime.")
    assert bar_regime.regime == "STRETCHED"
    assert ctx.position.size == 0


def test_route_entry_signal_blocks_when_working_order_exists():
    spring = _FakeStrategy(
        "spring_upthrust",
        Signal("BUY", "SPRING", stop_loss=95.0, confidence=0.7),
    )
    pending = [SimpleNamespace(symbol="TMF")]
    monitor = _make_monitor(
        _FakeRegistry({"spring_upthrust": spring}),
        pending_orders=pending,
    )
    df_5m = _make_frame()
    df_15m = df_5m.copy()
    last_5m = df_5m.iloc[-1]

    decision, _ctx, _session_regime, _bar_regime = monitor._route_entry_signal(
        last_5m, df_5m, df_15m, pd.Timestamp("2026-04-22 20:05:00"), "spring_upthrust"
    )

    assert not decision.is_trade
    assert decision.action == "BLOCKED"
    assert "working order" in decision.reason
    assert spring.init_calls == 1
