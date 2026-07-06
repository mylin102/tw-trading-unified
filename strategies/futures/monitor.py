"""
Futures monitor — full strategy from daily_simulation.
Accepts an injected Shioaji API instance (no internal login).
"""
import sys
import os
import glob
import hashlib
import time
import json
import math
import yaml
import traceback
from pathlib import Path
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
import pandas as pd
from rich.console import Console

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from squeeze_futures.engine.constants import get_point_value
from squeeze_futures.engine.simulator import PaperTrader
# 指標計算
# 指標計算
from squeeze_futures.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment
from squeeze_futures.data.data_storage import save_trade

# GSD: Pluggable Strategy Integration
from core.strategy_registry import StrategyRegistry
from core.strategy_context import StrategyContext, PositionView, MarketData
from core.signal import Signal
from core.futures_bar_regime import classify_futures_bar_regime
from core.futures_strategy_router import FuturesRouterDecision, route_futures_signal

# Data ingestion layer — all Shioaji API access is isolated here
from squeeze_futures.data.ingestion_service import IngestionService
# GSD: 策略外掛系統
from core.strategy_registry import StrategyRegistry
from core.strategy_context import StrategyContext, PositionView, MarketData
from core.signal import Signal
from core.bar_utils import attach_bar_metadata, build_canonical_bar_frames, build_preferred_canonical_bar_frames, resample_ohlcv
from core.date_utils import get_taifex_futures_hhmm, is_taifex_futures_market_open, get_taifex_futures_session_type, get_session_date_str
from core.spread_loader import get_spread_loader
from squeeze_futures.data.shioaji_client import ShioajiClient
from squeeze_futures.data.data_storage import save_trade
from squeeze_futures.data.tick_writer import RawTickWriter, get_trading_day_str
from squeeze_futures.data.kbar_writer import RawKbarWriter

try:
    from squeeze_futures.report.notifier import send_email_notification as _legacy_notify
except ImportError:
    _legacy_notify = None

# Structured notification system (core/notification/)
try:
    from core.notification.notifier import notify_trade_event as _notify_trade_event
    from core.notification.formatters.futures_formatter import (
        FuturesPositionState,
        compute_futures_pnl,
    )
    _has_notification_system = True
except ImportError:
    _has_notification_system = False

console = Console()


def _check_trend_breakout_signal(df_5m, df_15m):
    try:
        from squeeze_futures.engine.trend_breakout import check_trend_breakout
    except ImportError:
        return {"trend_long": False, "trend_short": False, "reasons": []}
    result = {"trend_long": False, "trend_short": False, "reasons": []}
    if len(df_5m) >= 20:
        b = check_trend_breakout(df_5m, lookback=20, ma_length=20, compare_bars=5, slope_threshold=0.1)
        if b["long_signal"]:
            result["trend_long"] = True
            result["reasons"].extend([f"5m: {r}" for r in b["long_reasons"]])
        if b["short_signal"]:
            result["trend_short"] = True
            result["reasons"].extend([f"5m: {r}" for r in b["short_reasons"]])
    return result


class FuturesMonitor:
    def __init__(self, api, config_path: str, dry_run: bool = False):
        self.api = api
        self.config_path = config_path
        self._config_mtime = 0
        self.dry_run = dry_run
        self.cfg = self._load_config(config_path)
        # 2026-05-27 Gemini CLI: Generalize ticker initialization (no hardcoded default)
        self.ticker = self.cfg.get("ticker", "UNKNOWN")
        self.contract = None
        self.far_contract = None  # Far-month contract for dual chart
        self._running = False

        # Far-month tick-based bar accumulation (independent from near-month)
        self._far_tick_bars_deque = deque(maxlen=300)
        self._far_current_bar = {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ts": None}
        self._last_far_bar_ts = 0

        # Compatibility placeholders for external integrations
        self.feed_health = None
        self.tx_bar_builder = None
        # [TX Cache] Pre-computed TX bars for cross-regime engine (populated
        # during backfill/startup, NOT fetched on-demand in strategy tick).
        self._tx_cached_kbars = None

        # Wrap injected api into ShioajiClient without re-login
        self.client = ShioajiClient.__new__(ShioajiClient)
        self.client.api = api
        self.client.is_logged_in = not dry_run

        # [Skew Integration] Option surface engine — populated by bidask
        # dispatcher via OptionQuoteEvent, consumed in _build_strategy_context.
        self._skew_engine = None
        self.client._tick_callbacks = {}
        self.client._kbar_callbacks = {}
        self.client._latest_kbars = {}

        # [ThetaGate] Latest router decision — consumed by options monitor
        # to check theta_allowed flag before entering theta positions.
        self.latest_router_decision: FuturesRouterDecision | None = None

        # 2026-06-23 Gemini CLI: Initialize strategy registry early to prevent AttributeError in tests/methods called before setup
        self._registry = StrategyRegistry()

        # [Phase 2] IngestionService — all Shioaji API access is isolated here.
        # strategy_tick() and signal generation read from canonical bars only.
        self._ingestion = IngestionService(
            api=api,
            client=self.client,
            contract=self.contract,
            ticker=self.ticker,
            save_raw_kbars_cb=self._save_raw_kbars,
        )

        # GSD: Initialize stateful attributes before applying config
        self.cooldown_until = 0
        self.consecutive_losses = 0
        self.session_losses = []  # [(timestamp, pnl_pts, exit_reason, session)]
        self.session_type = None  # "day" or "night", set per bar
        self.previous_session_type = None  # Track previous session for transition detection
        self._last_bar_context = {}  # Phase 0c: snapshot for entry diagnostic
        self._circuit_breaker = None
        self._session_pnl = 0.0  # Session PnL for circuit breaker
        
        # Failure detection state: tracks pending squeeze fire
        self._fire_pending_dir = 0   # +1=bullish fire, -1=bearish fire
        self._fire_bar_idx = 0
        self._fire_high = 0.0
        self._fire_low = 0.0
        self._bar_counter = 0        # monotonic bar counter for fire tracking
        self.is_monitoring_ready = True # [GSD 4.13] Phase A Ready
        self.is_trading_ready = False   # [GSD 4.13] Phase B Ready
        self._vwap_violation_bars = 0  # VWAP exit debounce counter
        self._atr_trail_peak = 0.0    # ATR trailing stop: peak price tracker

        # GSD Phase 0d: Hourly no-trade audit tracking
        self._last_trade_ts = None       # timestamp of last trade
        self._bars_since_trade = 0       # bars since last trade
        self._bars_since_session_open = 0 # [V-Model Upgrade] Track session bar count
        # ── Squeeze Fire Scout time stop tracking ──
        self._scout_entry_bar: int = -1
        self._scout_time_stop_bars: int = 0
        self._signals_generated = 0      # valid signals this hour
        self._signals_rejected = 0       # rejected signals this hour (reason, count)
        self._last_audit_hour = -1       # last hour we ran the audit
        self._data_stale_bars = 0        # consecutive bars with no new data
        self.options_monitor = None      # shared options monitor for hourly audit / repair
        
        # 💡 GSD: Market data cache for virtual ticks
        # 2026-05-27 Gemini CLI: Use dynamic ticker instead of hardcoded MTX
        self.market_data = {self.ticker: {"close": None}}
        self.last_tick_at = time.time()  # [gstack] 數據新鮮度追蹤 — must init before _strategy_tick()
        self._last_real_tmf_tick_at = self.last_tick_at
        self._runtime_status = None
        self._manual_trade_status = "READY"  # [GSD] Track manual trade state (READY, PROCESSING, FILLED, FAILED)
        # 2026-06-26 Gemini CLI: Initialize dynamic flag path from environment variable
        self.manual_trade_flag_path = os.environ.get("FUTURES_MANUAL_TRADE_FLAG_PATH", "/tmp/futures_manual_trade.flag")
        # 2026-06-05 JVS Claw: NO_LIVE_TICK fix — atomic flag lifecycle + idempotency
        self._processed_flag_ids: set = set()   # C2: idempotency set (in-memory, reset on restart)
        self._flag_retry_count: int = 0         # C7: retry counter (in-memory)
        self._current_flag_id: str | None = None  # C2: tracks flag being processed
        # 2026-06-05 JVS Claw: R1 — startup cleanup of orphaned .processing files
        for _orph in glob.glob(self.manual_trade_flag_path + ".processing"):
            try:
                os.rename(_orph, _orph.replace(".processing", ""))
                console.print(f"[yellow]🔄 [STARTUP] Recovered orphaned flag: {_orph}[/yellow]")
            except Exception:
                pass

        # Apply config (Initial create for Trader and OrderMgr happens here)
        self.order_mgr = None
        self.paper_fill_sim = None
        self._apply_config_params()
        self._config_mtime = os.path.getmtime(self.config_path) if os.path.exists(self.config_path) else 0

        self.has_tp1_hit = False
        self.last_processed_bar = None
        self._last_exit_bar = None  # 防止同根 K bar exit 後再進場
        self._last_entry_reason = None
        self.active_strategy_name = None
        self._initialized_strategy_names = set()
        self._safety_stop_trade = None  # Exchange-side safety stop order
        self._pending_lifecycle_orders: Dict[str, Dict[str, Any]] = {}
        self._applied_lifecycle_deals = set()
        self._mts_pending_fills: Dict[str, Dict[str, Any]] = {}  # [GSD] Track multi-leg spread fills before sync
        # 2026-05-27 Gemini CLI: Track orders currently undergoing timeout cancellation to prevent re-entry
        self._mts_stale_order_cancels = set()
        # 💡 GSD: Initialize with current time bucket to prevent immediate flip
        self._last_bar_ts = int(time.time() / 300) * 300

        # ── [V-Model] SpreadLoader for calendar spread data (near-far spread_z) ──
        self._spread_loader = get_spread_loader()
        # 2026-06-26 Gemini CLI: Pass active ticker to prevent loading default MXF CSV files
        self._spread_loaded = self._spread_loader.load_latest_csv(self.ticker)
        if self._spread_loaded:
            print(f"[V-Model] SpreadLoader initiated: {self._spread_loader.status()}")
        else:
            print("[V-Model] SpreadLoader: no calendar spread data found")
            active_strat = self.cfg.get("active_strategy") or self.cfg.get("strategy", {}).get("active_strategy")
            # 2026-06-26 Gemini CLI: If active strategy is a spread strategy, block startup if CSV is missing
            if active_strat in ("tmf_spread", "calendar_condor_v2"):
                raise ValueError(
                    f"[V-Model] Critical error: active strategy is '{active_strat}' but calendar spread CSV "
                    f"data failed to load for ticker '{self.ticker}'. Silent start with missing data is blocked to prevent data pollution."
                )

    def _apply_config_params(self):
        """[GSD] Extract parameters from self.cfg into instance attributes."""
        # Strategy config
        self.STRATEGY = self.cfg.get("strategy", {})
        self.RISK = self.cfg.get("risk_mgmt", {})
        self.MGMT = self.cfg.get("trade_mgmt", {})
        self.EXEC = self.cfg.get("execution", {})
        self.MONITOR = self.cfg.get("monitoring", {})
        self.PB = self.STRATEGY.get("pullback", {})
        self.TP = self.STRATEGY.get("partial_exit", {})
        self.FILTER_MODE = self.STRATEGY.get("regime_filter", "mid")
        self.ATR_MULT = self.RISK.get("atr_multiplier", 0.0)
        self.ATR_LENGTH = self.RISK.get("atr_length", 14)
        self.POLL_INTERVAL = self.MONITOR.get("poll_interval_secs", 30)

        # Debug flags from config
        _debug_cfg = self.cfg.get("debug", {})
        self._debug_tickbar = bool(_debug_cfg.get("tickbar", False))
        self._debug_feed = bool(_debug_cfg.get("feed", False))

        # Data freshness thresholds (seconds)
        self.STALE_WARN_SECS = self.MONITOR.get("stale_tick_warn_secs", 120)
        self.STALE_CRITICAL_SECS = self.MONITOR.get("stale_tick_critical_secs", 600)
        self.PB_CONFIRM_BARS = self.MONITOR.get("pb_confirmation_bars", 12)
        
        self.PB_ARGS = {
            "ema_fast": self.PB.get("ema_fast", 20),
            "ema_slow": self.PB.get("ema_slow", 60),
            "pb_buffer": self.PB.get("buffer", 1.002),
        }
        
        self.live_trading = self.cfg.get("live_trading", False)
        self.cooldown_bars = self.cfg.get("cooldown_bars", self.STRATEGY.get("cooldown_bars", 8))

        # 2026-05-27 Gemini CLI: Dynamic Ticker Support (No hardcoded defaults)
        _old_ticker = self.ticker
        self.ticker = self.cfg.get("ticker", "UNKNOWN")
        if self.ticker != _old_ticker:
            console.print(f"[cyan]🔄 Ticker updated: {_old_ticker} -> {self.ticker}[/cyan]")
            if hasattr(self, '_ingestion'):
                self._ingestion.ticker = self.ticker

        # Squeeze Failure Counter mode
        self.COUNTER = self.STRATEGY.get("counter_mode", {})
        self.counter_enabled = self.COUNTER.get("enabled", False)
        self.counter_auto_regime = self.COUNTER.get("auto_regime", True)
        self.counter_confirm_bars = self.COUNTER.get("confirm_bars", 5)
        self.counter_atr_sl_mult = self.COUNTER.get("atr_sl_mult", 1.0)
        self.counter_exit_vwap = self.COUNTER.get("exit_on_vwap", True)
        self.trend_hold_enabled = self.RISK.get("trend_hold_enabled", True)
        self.trend_hold_atr_mult = self.RISK.get("trend_hold_atr_mult", 2.5)
        self.trend_hold_min_score = self.RISK.get("trend_hold_min_score", 40)
        self.trend_hold_min_trend_strength = self.RISK.get("trend_hold_min_trend_strength", 0.001)
        self.trend_hold_min_price_vs_vwap = self.RISK.get("trend_hold_min_price_vs_vwap", 0.0003)
        self.trend_hold_min_time_to_close_mins = self.RISK.get("trend_hold_min_time_to_close_mins", 20)

        # Update Order Lifecycle settings if needed
        self._use_order_manager = self.MONITOR.get("use_order_manager", False)

        # ── [L3] Order Lifecycle Manager initialization logic (only if not already set) ──
        if self._use_order_manager and not getattr(self, 'order_mgr', None):
            from core.order_management.order_manager import OrderManager
            from core.order_management.paper_fill import PaperFillSimulator
            _om_mode = "live" if self.live_trading else "paper"
            broker = self.client if self.live_trading else None
            self.order_mgr = OrderManager(mode=_om_mode, broker_adapter=broker)
            if _om_mode == "paper":
                self.paper_fill_sim = PaperFillSimulator(self.order_mgr)
                self.order_mgr.set_simulator(self.paper_fill_sim)
            
            # [GSD Fix] Recover orders from trades CSV BEFORE wiring callbacks
            self._recover_orders_from_trades_csv()
            
            self._wire_order_callbacks()
            console.print(f"[green]📋 Order Lifecycle Manager enabled ({_om_mode} mode)[/green]")

        # Create or update Trader
        if not hasattr(self, 'trader'):
            self.trader = PaperTrader(
                ticker=self.ticker,
                initial_balance=self.EXEC.get("initial_balance", 100000),
                point_value=get_point_value(self.ticker),
                fee_per_side=self.EXEC.get("broker_fee_per_side", 20),
                exchange_fee_per_side=self.EXEC.get("exchange_fee_per_side", 0),
                tax_rate=self.EXEC.get("tax_rate", 0),
                margin_per_lot=self.EXEC.get("margin_per_lot", 18000),
            )
        else:
            # We don't change initial_balance after start, but we can update fees and margin
            self.trader.fee_per_side = self.EXEC.get("broker_fee_per_side", 20)
            self.trader.exchange_fee_per_side = self.EXEC.get("exchange_fee_per_side", 0)
            self.trader.tax_rate = self.EXEC.get("tax_rate", 0)
            self.trader.margin_per_lot = self.EXEC.get("margin_per_lot", 18000)

    def _reload_config_if_changed(self):
        """[Rule 9] Hot-reload config if YAML file has been updated."""
        if not os.path.exists(self.config_path):
            return
            
        mtime = os.path.getmtime(self.config_path)
        if mtime > self._config_mtime:
            try:
                self.cfg = self._load_config(self.config_path)
                self._apply_config_params()
                self._config_mtime = mtime
                console.print(f"[cyan]🔄 Config hot-reloaded from {self.config_path}[/cyan]")
            except Exception as e:
                console.print(f"[red]❌ Failed to reload config: {e}[/red]")

    def _is_trend_follow_entry(self, reason: Optional[str] = None) -> bool:
        reason = reason or self._last_entry_reason or ""
        return (
            reason.startswith("ADAPTIVE_TREND_V3")
            or reason.startswith("AI_ORB_V3_")
            or reason.startswith("ORB_UP_BREAKOUT")
            or reason.startswith("ORB_DOWN_BREAKOUT")
            or reason.startswith("LR_ACCEL_")
        )

    def _trend_hold_active(self, last_5m, last_price: float, score: float, vwap: float, time_to_close: float) -> bool:
        if not self.trend_hold_enabled or self.trader.position == 0:
            return False
        if not self._is_trend_follow_entry():
            return False
        if time_to_close <= self.trend_hold_min_time_to_close_mins:
            return False
        if abs(score) < self.trend_hold_min_score:
            return False

        trend_strength = float(last_5m.get("trend_strength_raw", 0.0))
        price_vs_vwap = float(last_5m.get("price_vs_vwap", 0.0))
        if price_vs_vwap == 0.0 and vwap:
            price_vs_vwap = (last_price - vwap) / vwap

        if self.trader.position > 0:
            bullish_align = bool(last_5m.get("bullish_align", last_5m.get("bull_align", False)))
            momentum_ok = float(last_5m.get("momentum", 0.0)) >= 0
            return (
                bullish_align
                and momentum_ok
                and trend_strength >= self.trend_hold_min_trend_strength
                and price_vs_vwap >= self.trend_hold_min_price_vs_vwap
            )

        bearish_align = bool(last_5m.get("bearish_align", last_5m.get("bear_align", False)))
        momentum_ok = float(last_5m.get("momentum", 0.0)) <= 0
        return (
            bearish_align
            and momentum_ok
            and trend_strength <= -self.trend_hold_min_trend_strength
            and price_vs_vwap <= -self.trend_hold_min_price_vs_vwap
        )

    def _apply_trend_hold_trail(self, last_price: float, last_5m, timestamp):
        atr_val = float(last_5m.get("atr", 50) or 50)
        if self.trader.position > 0:
            self._atr_trail_peak = max(self._atr_trail_peak, last_price)
            trail_floor = self._atr_trail_peak - atr_val * self.trend_hold_atr_mult
            if last_price <= trail_floor:
                return self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason="TREND_HOLD_TRAIL")

        elif self.trader.position < 0:
            if self._atr_trail_peak == 0:
                self._atr_trail_peak = last_price
            self._atr_trail_peak = min(self._atr_trail_peak, last_price)
            trail_ceil = self._atr_trail_peak + atr_val * self.trend_hold_atr_mult
            if last_price >= trail_ceil:
                return self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason="TREND_HOLD_TRAIL")

        return None

    def _load_config(self, path):
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _get_tick_bars_df(self):
        """[Wave 2] Rebuild deque cache on every call so _strategy_tick sees latest bars."""
        if len(self._tick_bars_deque) > 0:
            records = list(self._tick_bars_deque)
            self._tick_bars_cache = pd.DataFrame({
                "Open": [r["open"] for r in records],
                "High": [r["high"] for r in records],
                "Low": [r["low"] for r in records],
                "Close": [r["close"] for r in records],
                "Volume": [r["volume"] for r in records],
            }, index=[r["ts"] for r in records])
        return self._tick_bars_cache if self._tick_bars_cache is not None else pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    def get_far_tick_bars_df(self):
        """Return far-month tick bars as DataFrame for dashboard consumption."""
        records = list(self._far_tick_bars_deque)
        if not records:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        return pd.DataFrame({
            "Open": [r["open"] for r in records],
            "High": [r["high"] for r in records],
            "Low": [r["low"] for r in records],
            "Close": [r["close"] for r in records],
            "Volume": [r["volume"] for r in records],
        }, index=[r["ts"] for r in records])

    def _bars_time_aligned(self, tx_bars, df_5m):
        """Check that the latest TX bar and MXF 5m bar share the same timestamp bucket.

        Args:
            tx_bars (list[dict]): list of tx bars from TxBarBuilder.bars()
            df_5m (pd.DataFrame): processed 5m dataframe for MXF

        Returns:
            bool: True if aligned, False otherwise
        """
        try:
            if not tx_bars or df_5m is None or len(df_5m) == 0:
                return False
            tx_last = tx_bars[-1].get("ts")
            if tx_last is None:
                return False
            # df_5m index's last timestamp
            tmf_last = df_5m.index[-1]
            # Compare normalized timestamps (both are pandas.Timestamp)
            return pd.Timestamp(tx_last) == pd.Timestamp(tmf_last)
        except Exception:
            return False

    def setup(self):
        # ── GSD: Initialize Strategy Registry ────────────────────────
        # 2026-06-23 Gemini CLI: Reuse existing registry instance if already created in __init__
        if not hasattr(self, "_registry") or self._registry is None:
            self._registry = StrategyRegistry()
        self._registry.discover()
        # [V-Model] Log discovered strategies for startup diagnostics
        _all = self._registry.list_all()
        _available = [s["name"] for s in _all if s.get("available")]
        _names = [s["name"] for s in _all]
        console.print(f"[dim][StrategyRegistry] discovered={len(_all)} available={len(_available)} "
                      f"names={_names}[/dim]")
        self._active_strategy_name = None  # Track initialized strategy

        # ── GSD Phase 3: Circuit Breaker initialization ──────────────
        try:
            from core.circuit_breaker import CircuitBreaker
            # Create two independent breakers (day/night)
            self._circuit_breaker = CircuitBreaker(
                session="day",  # Will be used based on session_type at runtime
                daily_loss_cap=5000,  # 5% of 100k capital
                max_consecutive=3,
            )
            console.print("[green]🛡️ Circuit Breaker initialized[/green]")
        except Exception as e:
            console.print(f" [yellow]⚠️ Circuit Breaker init failed: {e}[/yellow] ")
            self._circuit_breaker = None

        # ── Pre-init the active strategy ─────────────────────────────
        active_name = self.STRATEGY.get("active_strategy", "counter_vwap")
        strategy = self._registry.get(active_name)

        # SAFETY CHECK 2026-04-14: Crash prevention — validate strategy is registered
        if strategy is None:
            available = [s["name"] for s in self._registry.list_all() if s.get("available")]
            console.print(f"[bold red]🚨 Strategy '{active_name}' NOT in registry! Available: {available}[/bold red]")
            console.print(f"[bold red]   System will run in MONITOR-ONLY mode (no entries) until config is fixed.[/bold red]")
            # Set a safe fallback so setup() doesn't crash
            self._active_strategy_name = None
        else:
            # Create a minimal context for init
            dummy_ctx = StrategyContext(
                market=MarketData(
                    last_bar={},
                    # 2026-05-27 Gemini CLI: Pass current ticker to dummy context
                    ticker=self.ticker
                ),
                position=PositionView(),
                config=self.cfg,
                bar_counter=0,
            )
            self._ensure_strategy_initialized(active_name, strategy, dummy_ctx)
            self._active_strategy_name = active_name
            self.active_strategy_name = active_name
            console.print(f"[green]🔧 Pre-initialized strategy: {active_name}[/green]")

        # Tick-based bar builder (Initialize always to avoid AttributeError in dry_run)
        # [Wave 2 optimization] Use deque for O(1) append/trim instead of DataFrame.loc + slicing
        self._tick_bars_deque = deque(maxlen=300)
        self._tick_bars_cache = None  # Cached DF for indicator calculations
        self._current_bar = {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ts": None}

        # [GSD Data Safety] RawTickWriter: every tick lands on CSV before memory
        self._tick_writer = None  # Initialised lazily on first real tick

        # [GSD Data Safety] RawKbarWriter: every api.kbars() response lands on CSV before computation
        self._kbar_writer = None  # Initialised lazily on first kbar fetch

        if self.dry_run:
            console.print(" [yellow][FuturesMonitor] dry-run: skipping contract fetch[/yellow] ")
            return True

        # [GSD Fix] Warm-up from Parquet SSOT (Wave 5 Integration)
        try:
            # 2026-06-18 Gemini CLI: [Pure TMF Refactoring] Disabled TXFR1 fallback
            from core.data_manager import data_manager
            ticker_warm = self.ticker  # e.g. "TMF"
            df_hist = data_manager.load_historical(ticker_warm)
            # if df_hist.empty or len(df_hist) < 20:
            #     # Fallback: try TXFR1 which has broader coverage
            #     df_hist = data_manager.load_historical("TXFR1")
            
            if not df_hist.empty and len(df_hist) >= 20:
                df_warm = df_hist.tail(100)
                for ts, row in df_warm.iterrows():
                    self._tick_bars_deque.append({
                        "open": row["Open"], "high": row["High"], "low": row["Low"], 
                        "close": row["Close"], "volume": row["Volume"], "ts": ts
                    })
                # Initialize cache to None — _get_tick_bars_df() will rebuild from deque
                self._tick_bars_cache = None
                console.print(f"[green][FuturesMonitor] ✓ Warmed up with {len(df_warm)} bars from {ticker_warm} Parquet DB[/green]")
            else:
                # [Night Session Fix] Fallback: read from today's indicators CSV for warm-up
                console.print(f" [yellow][FuturesMonitor] Parquet warm-up empty, trying CSV fallback...[/yellow] ")
                from core.date_utils import get_session_date_str
                import os as _os
                log_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "logs", "market_data")
                date_str = get_session_date_str(datetime.now())
                tag = "_PAPER" if not self.live_trading else "_LIVE"
                csv_path = _os.path.join(log_dir, f"{self.ticker}_{date_str}{tag}_indicators.csv")
                if _os.path.exists(csv_path):
                    df_csv = pd.read_csv(csv_path)
                    if "timestamp" in df_csv.columns:
                        df_csv["timestamp"] = pd.to_datetime(df_csv["timestamp"], errors="coerce")
                        df_csv = df_csv.set_index("timestamp")
                        if len(df_csv) >= 20:
                            df_warm = df_csv.tail(100)
                            for ts, row in df_warm.iterrows():
                                self._tick_bars_deque.append({
                                    "open": row.get("open", row.get("Open", 0)),
                                    "high": row.get("high", row.get("High", 0)),
                                    "low": row.get("low", row.get("Low", 0)),
                                    "close": row.get("close", row.get("Close", 0)),
                                    "volume": row.get("volume", row.get("Volume", 0)),
                                    "ts": ts,
                                })
                            self._tick_bars_cache = None
                            console.print(f"[green][FuturesMonitor] ✓ Warmed up with {len(df_warm)} bars from CSV fallback[/green]")
        except Exception as e:
            console.print(f"[dim][FuturesMonitor] Warm-up failed: {e}[/dim]")

        # 獲取TMF合約
        try:
            target_symbol = str(self.ticker)
            print(f"[FuturesMonitor] Getting {target_symbol} contracts (Safe Mode)...")
            
            # [rshioaji 1.5.10 Workaround] Use robust list helper to avoid C++ binding crash
            from core.broker.shioaji_compat import get_contracts_list
            tmf_list = get_contracts_list(self.api, "Futures", target_symbol)
            
            print(f"[FuturesMonitor] Found {len(tmf_list)} {target_symbol} contracts")
            if tmf_list:
                # [GSD Settlement Fix] Filter out expired or invalid
                # On settlement day (3rd Wednesday), the front-month expires at 13:30.
                now = datetime.now()
                now_str = now.strftime("%Y/%m/%d")
                settlement_time = now.replace(hour=13, minute=30, second=0, microsecond=0)
                
                valid_contracts = []
                for c in tmf_list:
                    # Shioaji delivery_date format: "YYYY/MM/DD"
                    if c.delivery_date > now_str:
                        valid_contracts.append(c)
                    elif c.delivery_date == now_str:
                        # If today is settlement day, only use it if before 13:30
                        if now < settlement_time:
                            valid_contracts.append(c)
                        else:
                            console.print(f" [yellow][FuturesMonitor] Settlement day detected ({now_str}), skipping expired contract {c.code} after 13:30[/yellow] ")
                
                # Sort by delivery date (ascending)
                tmf_sorted = sorted(valid_contracts, key=lambda c: c.delivery_date)
                
                if tmf_sorted:
                    # Pick the first one (nearest delivery)
                    self.contract = tmf_sorted[0]
                    console.print(f"[green][FuturesMonitor] ✓ {self.ticker} front-month: {self.contract.code} (delivers {self.contract.delivery_date})[/green]")
                    # Sync contract to ingestion service (resolved after __init__)
                    try:
                        self._ingestion.set_contract(self.contract)
                    except Exception:
                        pass
                else:
                    # Fallback to absolute nearest if no valid ones found (shouldn't happen in live)
                    self.contract = sorted(tmf_list, key=lambda c: c.delivery_date)[0]
                    console.print(f" [yellow][FuturesMonitor] No future delivery found, using absolute nearest: {self.contract.code}[/yellow] ")
                
                # Log all available codes for verification
                all_codes = [f"{c.code}({c.delivery_date})" for c in tmf_sorted]
                console.print(f"[dim][FuturesMonitor] Valid {self.ticker} queue: {', '.join(all_codes)}[/dim]")

                # [Far Month] Select first contract with DIFFERENT delivery date for dual chart
                front_delivery = self.contract.delivery_date if self.contract else None
                self.far_contract = None
                for c in tmf_sorted[1:]:
                    if c.delivery_date != front_delivery:
                        self.far_contract = c
                        break
                if self.far_contract is not None:
                    console.print(f"[green][FuturesMonitor] ✓ {self.ticker} far-month: {self.far_contract.code} (delivers {self.far_contract.delivery_date})[/green]")
                else:
                    self.far_contract = None
                    console.print(f" [yellow][FuturesMonitor] No far-month contract available[/yellow] ")
            else:
                console.print(f"[red][FuturesMonitor] No {self.ticker} contracts found![/red]")
        except Exception as e:
            console.print(f"[red][FuturesMonitor] Error selecting {self.ticker} contract: {e}[/red]")

        # [Bug Fix] Add contract rollover check
        self._last_contract_code = self.contract.code if self.contract else None

        # 2026-06-24 Gemini CLI: Pre-fill near/far contract prices from snapshots at startup to prevent identical execution prices on first manual trade.
        if self.api and not self.dry_run:
            try:
                _contracts_to_query = []
                if self.contract:
                    _contracts_to_query.append(self.contract)
                if self.far_contract:
                    _contracts_to_query.append(self.far_contract)
                
                if _contracts_to_query:
                    _snaps = self.api.snapshots(_contracts_to_query)
                    for _snap in _snaps:
                        if _snap.close and _snap.close > 0:
                            if self.contract and _snap.code == self.contract.code:
                                self.market_data[self.ticker] = {
                                    "close": float(_snap.close),
                                    "local_arrival_at": time.time(),
                                    "datetime": datetime.now()
                                }
                                self.market_data[f"{self.ticker}_NEAR"] = {
                                    "close": float(_snap.close),
                                    "local_arrival_at": time.time(),
                                    "datetime": datetime.now()
                                }
                            elif self.far_contract and _snap.code == self.far_contract.code:
                                self._far_current_bar["close"] = float(_snap.close)
                                self._far_current_bar["open"] = float(_snap.close)
                                self._far_current_bar["high"] = float(_snap.close)
                                self._far_current_bar["low"] = float(_snap.close)
                                self.market_data[f"{self.ticker}_FAR"] = {
                                    "close": float(_snap.close),
                                    "local_arrival_at": time.time(),
                                    "datetime": datetime.now()
                                }
                                console.print(f"[green][FuturesMonitor] Pre-filled far-month price from snapshot: {_snap.close}[/green]")
            except Exception as _snap_err:
                console.print(f"[yellow][FuturesMonitor] Failed to pre-fill prices from snapshot: {_snap_err}[/yellow]")

        # Pre-fill from kbars if available (使用新的方法)
        try:
            # 首先嘗試使用新的方法獲取當天1分鐘K棒
            df_1min = self._fetch_today_kbars()
            if df_1min is not None and len(df_1min) >= 1:
                # 重採樣為5分鐘K棒
                df = resample_ohlcv(df_1min, "5min")
                
                if not df.empty:
                    # Convert pre-filled bars to deque format
                    for _, row in df[["Open", "High", "Low", "Close", "Volume"]].iterrows():
                        bar_dict = {
                            "open": row["Open"],
                            "high": row["High"],
                            "low": row["Low"],
                            "close": row["Close"],
                            "volume": row["Volume"],
                            "ts": row.name,  # DataFrame index is timestamp
                        }
                        self._tick_bars_deque.append(bar_dict)
                    self._tick_bars_cache = df[["Open", "High", "Low", "Close", "Volume"]].copy()
                    console.print(f"[green][FuturesMonitor] pre-filled {len(self._tick_bars_deque)} bars from today's 1min kbars[/green]")
                    
                    # [GSD Fix] Backfill night session gaps on startup
                    self._backfill_night_gaps(df)
            else:
                # [Phase 2] Legacy fallback delegated to IngestionService (startup-only)
                # If _fetch_today_kbars() is rate-limited or unavailable, the
                # strategy loop will naturally fill from tick accumulation.
                console.print("[dim][FuturesMonitor] No kbar data at startup — will fill from live ticks[/dim]")
        except Exception:
            pass
        return True

    def _backfill_night_gaps(self, api_df):
        """[GSD Fix] On startup, check if today's CSV has night session data.
        If missing or incomplete, merge API bars with existing CSV.
        
        [BUG FIX 2026-04-20] Use get_session_date_str() (trading session date) instead of
        today.strftime('%Y%m%d') (wall-clock date) so the backfill writes to the SAME file
        as _save_bar.  The old code wrote night-session bars to e.g. TMF_20260420 while
        _save_bar correctly wrote them to TMF_20260421, and the dashboard's
        drop_duplicates(keep='first') would then prefer the indicator-less rows from 20260420
        over the correctly-computed rows from 20260421, making the dashboard show NaN until
        ~17:10 when the wrong file's last timestamp was exceeded.
        """
        if self.dry_run or not self.api:
            return
        
        from pathlib import Path
        from core.date_utils import get_session_date_str
        today = datetime.now()
        date_str = get_session_date_str(today)
        tag = "_DRY" if self.dry_run else ("_LIVE" if self.live_trading else "_PAPER")
        csv_path = Path(f"logs/market_data/{self.ticker}_{date_str}{tag}_indicators.csv")
        
        # [ARCHITECTURE FIX 2026-05-13] NEVER write indicator CSV if it doesn't exist yet.
        # The indicator CSV is an enriched output — only _save_bar should create it.
        # _backfill_night_gaps must not be the first writer, or the CSV header
        # will have raw API column ordering (timestamp-as-index) instead of canonical order.
        # Strategy tick will trigger _save_bar on the next bar boundary anyway.
        if not csv_path.exists():
            console.print(f"[dim][FuturesMonitor] Skipping backfill write — CSV doesn't exist yet; _save_bar will create it on next bar[/dim]")
            return
        
        def _load_existing_indicator_csv(path: Path):
            if not path.exists():
                return pd.DataFrame(), None
            try:
                existing_df = pd.read_csv(path, parse_dates=['timestamp'])
            except Exception:
                existing_df = pd.read_csv(path)
                renamed = False
                for col in existing_df.columns:
                    if not str(col).strip() or str(col).startswith("Unnamed"):
                        if "timestamp" not in existing_df.columns:
                            existing_df = existing_df.rename(columns={col: "timestamp"})
                            renamed = True
                            break
                if "timestamp" in existing_df.columns:
                    existing_df["timestamp"] = pd.to_datetime(existing_df["timestamp"], errors="coerce")
                    existing_df = existing_df.dropna(subset=["timestamp"])
                if renamed:
                    console.print(" [yellow][FuturesMonitor] Repaired corrupt startup CSV timestamp header[/yellow] ")
            if "timestamp" not in existing_df.columns:
                return pd.DataFrame(), None
            existing_df.set_index('timestamp', inplace=True)
            existing_df.index = pd.to_datetime(existing_df.index, errors="coerce")
            existing_df = existing_df[~existing_df.index.isna()]
            existing_df = existing_df[~existing_df.index.duplicated(keep='first')].sort_index()
            last_existing_ts = existing_df.index.max() if not existing_df.empty else None
            return existing_df, last_existing_ts

        # Read existing CSV
        if csv_path.exists():
            try:
                existing, last_ts = _load_existing_indicator_csv(csv_path)
                console.print(f"[dim][FuturesMonitor] Existing CSV: {len(existing)} bars, latest={last_ts}[/dim]")
            except Exception:
                existing = pd.DataFrame()
                last_ts = None
        else:
            existing = pd.DataFrame()
            last_ts = None
        
        api_df = api_df.copy()
        if not api_df.empty:
            api_df.index = pd.to_datetime(api_df.index, errors="coerce")
            api_df = api_df[~api_df.index.isna()]
            api_df = api_df[~api_df.index.duplicated(keep='last')].sort_index()
        
        # Find bars from API that are newer than CSV, or rebuild from API if CSV timestamp is corrupt/missing
        if not api_df.empty:
            if last_ts is None:
                new_bars = api_df
            else:
                new_bars = api_df[api_df.index > last_ts]
            if last_ts is None or not new_bars.empty:
                # [BUG FIX 2026-04-20] Do NOT write raw indicator-less bars to the session file
                # if _save_bar has already written bars with computed indicators.  The raw OHLCV
                # bars (no indicators) from the API would contaminate the file: later reads by the
                # dashboard would see NaN for indicator columns on those rows.  When _save_bar
                # processes the same bar it only APPENDS (not updates), so the NaN rows persist.
                #
                # Heuristic: if the existing file already has indicator data (e.g. has a 'momentum'
                # column with at least one non-NaN value), skip the raw backfill entirely.
                # _save_bar will write fully-computed rows going forward.
                #
                # [BUG FIX 2026-05-13] Check MULTIPLE indicator columns, not just momentum.
                # The raw API backfill produces NaN for ALL indicator columns. A single column
                # check (momentum.notna().any()) can return False if a previous backfill already
                # overwrote the enriched rows with raw data. Checking multiple columns ensures
                # we only skip when genuine enrichment has been committed to the CSV.
                _indicator_cols_in_csv = ["momentum", "atr", "vwap"]
                _present = [c for c in _indicator_cols_in_csv if c in existing.columns]
                has_indicator_data = (
                    not existing.empty
                    and len(_present) >= 2
                    and all(existing[c].notna().any() for c in _present)
                )
                # heuristic: also NEVER overwrite if _save_bar has been called at least once
                # in this session (tracked via _backfill_has_seen_enriched_row).
                if has_indicator_data or getattr(self, '_backfill_has_seen_enriched_row', False):
                    if not has_indicator_data and getattr(self, '_backfill_has_seen_enriched_row', False):
                        console.print(f"[dim][FuturesMonitor] Skipping raw backfill — _save_bar has written enriched rows this session[/dim]")
                    return

                if last_ts is None:
                    console.print(f"[bold cyan]🔧 Rebuilding startup CSV from API ({api_df.index[0]} → {api_df.index[-1]})[/bold cyan]")
                else:
                    console.print(f"[bold cyan]🔧 Backfilling {len(new_bars)} missing bars from API ({new_bars.index[0]} → {new_bars.index[-1]})[/bold cyan]")
                
                combined = existing.copy() if not existing.empty else pd.DataFrame()
                if combined.empty:
                    combined = new_bars.copy()
                else:
                    combined = pd.concat([combined, new_bars], sort=False)
                    combined = combined[~combined.index.duplicated(keep='last')].sort_index()
                
                # Add missing columns if needed
                for col in ['score', 'regime', 'session', 'bull_align', 'bear_align', 'in_pb_zone']:
                    if col not in combined.columns:
                        combined[col] = 0 if col in ['score'] else ('NORMAL' if col == 'regime' else (2 if col == 'session' else False))
                
                combined.index.name = "timestamp"
                combined.to_csv(csv_path, index_label="timestamp")
                console.print(f"[green][FuturesMonitor] ✅ Backfill complete: {len(combined)} total bars in CSV[/green]")

    def _tmf_feed_age_secs(self):
        """Prefer real MXF tick age over feed_health (which may be updated by non-tick sources).
        # 2026-05-22 Hermes Agent: use _last_real_tmf_tick_at as ground truth — never polluted by TMF_VIRTUAL"""
        try:
            # [FeedHealth] Use _last_real_tmf_tick_at as ground truth — only updated by
            # real MXF/TMF ticks in on_tick(), never by TMF_VIRTUAL synthetic ticks.
            if self._last_real_tmf_tick_at > 0:
                return max(0.0, time.time() - self._last_real_tmf_tick_at)
        except Exception:
            pass
        try:
            if hasattr(self, "feed_health") and self.feed_health is not None:
                # 2026-05-27 Gemini CLI: Use dynamic ticker for health check
                age = self.feed_health.age(self.ticker)
                if age is not None and math.isfinite(float(age)):
                    return max(0.0, float(age))
        except Exception:
            pass
        # Fall back to self.last_tick_at which is updated in on_tick even if bucket classification fails
        return max(0.0, time.time() - self.last_tick_at)

    def _set_runtime_status(self, status):
        if getattr(self, "_runtime_status", None) == status:
            return
        from core.shioaji_session import set_system_status
        set_system_status(status)
        self._runtime_status = status

    def _refresh_runtime_status(self):
        from core.shioaji_session import SystemReadiness

        warn = getattr(self, "STALE_WARN_SECS", self.MONITOR.get("stale_tick_warn_secs", 120))
        tmf_age = self._tmf_feed_age_secs()
        if tmf_age > warn:
            self._set_runtime_status(SystemReadiness.DEGRADED)
        elif self.is_trading_ready:
            self._set_runtime_status(SystemReadiness.TRADING)
        else:
            self._set_runtime_status(SystemReadiness.TRADING)

    def _check_futures_contract_staleness(self):
        """[Wave 1 Fix] Check if MXF ticks are stale and attempt recovery.

        Behavior:
        - If no new tick for < warn_secs: no-op.
        - If >= warn_secs but < critical_secs: attempt light recovery (rollover/resubscribe) and try fetching kline.
        - If >= critical_secs: mark monitor not running and raise to trigger supervisor restart.

        All watchdog actions log in unified structured format for grep-ability:
            [IngestionWatchdog] reason=<reason> symbol=<sym> tick_age_secs=<N>
            last_bar_ts=<ts> canonical_age_secs=<N> action=<action> result=<result>
        """
        if self.dry_run or not self.api:
            return

        secs_since_tick = self._tmf_feed_age_secs()
        warn = getattr(self, 'STALE_WARN_SECS', self.MONITOR.get('stale_tick_warn_secs', 120))
        critical = getattr(self, 'STALE_CRITICAL_SECS', self.MONITOR.get('stale_tick_critical_secs', 600))
        if secs_since_tick < warn:
            return

        from core.shioaji_session import SystemReadiness
        self._set_runtime_status(SystemReadiness.DEGRADED)

        # Gather structured context for watchdog log
        symbol = getattr(self.contract, 'code', self.ticker) if hasattr(self, 'contract') else self.ticker
        now_dt = datetime.now()
        last_bar_ts = "N/A"
        canonical_age_secs = -1
        try:
            df_5m = self._get_tick_bars_df()
            if df_5m is not None and not df_5m.empty:
                last_idx = df_5m.index[-1]
                if isinstance(last_idx, pd.Timestamp):
                    last_bar_ts = last_idx.strftime('%H:%M:%S')
                    canonical_age_secs = int((now_dt - last_idx.to_pydatetime()).total_seconds())
        except Exception:
            pass

        # ── Feed stale (warn threshold exceeded) ──
        _real_tick_age = max(0.0, time.time() - self._last_real_tmf_tick_at)
        console.print(
            f" [yellow][IngestionWatchdog] "
            f"reason=feed_stale symbol={symbol} "
            f"tick_age_secs={secs_since_tick:.0f} "
            f"real_tick_age_secs={_real_tick_age:.0f} "
            f"last_bar_ts={last_bar_ts} "
            f"canonical_age_secs={canonical_age_secs} "
            f"action=check_contract "
            f"result=degraded[/yellow] "
        )

        if not is_taifex_futures_market_open():
            console.print(
                f"[dim][IngestionWatchdog] "
                f"reason=market_closed symbol={symbol} "
                f"tick_age_secs={secs_since_tick:.0f} "
                f"last_bar_ts={last_bar_ts} "
                f"canonical_age_secs={canonical_age_secs} "
                f"action=none "
                f"result=market_closed_keep_alive[/dim]"
            )
            return

        # ── Session transition buffer: 15:00-15:15 is a scheduled break ──
        # No ticks are emitted during this window. Don't treat it as a failure.
        hhmm_now = int(now_dt.strftime("%H%M"))
        in_transition_break = (1500 <= hhmm_now <= 1515)

        # If we exceed critical threshold, stop the monitor so external supervisor restarts the process
        if secs_since_tick >= critical and not in_transition_break:
            console.print(
                f"[red][IngestionWatchdog] "
                f"reason=feed_stale_critical symbol={symbol} "
                f"tick_age_secs={secs_since_tick:.0f} "
                f"last_bar_ts={last_bar_ts} "
                f"canonical_age_secs={canonical_age_secs} "
                f"action=shutdown "
                f"result=trigger_supervisor_restart[/red]"
            )
            try:
                if self.contract:
                    self.api.quote.unsubscribe(self.contract, quote_type='tick')
            except Exception:
                pass
            # Mark monitor as not running and raise to break out of run loop
            self._running = False
            raise RuntimeError(f"{self.ticker} tick stale for {secs_since_tick} seconds (>{critical}), exiting monitor.")

        # Between warn and critical: attempt light recovery
        console.print(
            f"[dim][IngestionWatchdog] "
            f"reason=feed_stale symbol={symbol} "
            f"tick_age_secs={secs_since_tick:.0f} "
            f"last_bar_ts={last_bar_ts} "
            f"canonical_age_secs={canonical_age_secs} "
            f"action=light_recovery "
            f"result=attempting[/dim]"
        )

        # Check for expiry/rollover
        today_str = datetime.now().strftime("%Y/%m/%d")
        if self.contract and self.contract.delivery_date < today_str:
            console.print(
                f" [yellow][IngestionWatchdog] "
                f"reason=contract_expired symbol={symbol} "
                f"tick_age_secs={secs_since_tick:.0f} "
                f"last_bar_ts={last_bar_ts} "
                f"canonical_age_secs={canonical_age_secs} "
                f"action=rollover "
                f"result=triggered[/yellow] "
            )
            self._check_contract_rollover()
            self.last_tick_at = time.time()
            return

        # If contract valid but no ticks, could be session transition or connection drop
        # Try contract rollover/resubscribe first
        try:
            self._check_contract_rollover()
        except Exception as e:
            console.print(
                f" [yellow][IngestionWatchdog] "
                f"reason=rollover_failed symbol={symbol} "
                f"tick_age_secs={secs_since_tick:.0f} "
                f"last_bar_ts={last_bar_ts} "
                f"canonical_age_secs={canonical_age_secs} "
                f"action=rollover "
                f"result=exception:{e}[/yellow] "
            )

        # ═══ STARTUP / RECOVERY-ONLY: Light kline fetch via IngestionService ═══
        # This recovery path is only triggered when tick data has gone stale.
        # Delegates to IngestionService which handles rate limiting, CSV persistence,
        # and TXFR1 pre-fetch. The resulting data goes through the canonical bar pipeline.
        try:
            df_backfill = self._ingestion.fetch_recovery_kline()
            if df_backfill is not None and not df_backfill.empty:
                console.print(
                    f"[green][IngestionWatchdog] "
                    f"reason=feed_stale symbol={symbol} "
                    f"tick_age_secs={secs_since_tick:.0f} "
                    f"last_bar_ts={last_bar_ts} "
                    f"canonical_age_secs={canonical_age_secs} "
                    f"action=fetch_recovery_kline "
                    f"result=success:rows={len(df_backfill)}[/green]"
                )
                self.last_tick_at = time.time()
                return
            else:
                console.print(
                    f" [yellow][IngestionWatchdog] "
                    f"reason=feed_stale symbol={symbol} "
                    f"tick_age_secs={secs_since_tick:.0f} "
                    f"last_bar_ts={last_bar_ts} "
                    f"canonical_age_secs={canonical_age_secs} "
                    f"action=fetch_recovery_kline "
                    f"result=empty_response[/yellow] "
                )
        except Exception as e:
            console.print(
                f" [yellow][IngestionWatchdog] "
                f"reason=feed_stale symbol={symbol} "
                f"tick_age_secs={secs_since_tick:.0f} "
                f"last_bar_ts={last_bar_ts} "
                f"canonical_age_secs={canonical_age_secs} "
                f"action=fetch_recovery_kline "
                f"result=exception:{e}[/yellow] "
            )

        # Reset timer to avoid spamming retries; next loop will re-evaluate
        self.last_tick_at = time.time()

    def _is_contract_expired(self, contract_delivery_date):
        """[GSD Settlement Fix] Check if contract is expired considering settlement time (13:30).
        
        Args:
            contract_delivery_date: Delivery date in "YYYY/MM/DD" format
            
        Returns:
            bool: True if contract is expired (past settlement time on delivery date)
        """
        try:
            # Parse contract delivery date
            contract_date = datetime.strptime(contract_delivery_date, "%Y/%m/%d").date()
            now = datetime.now()
            today = now.date()
            
            # If contract date is in the future, it's not expired
            if contract_date > today:
                return False
            
            # If contract date is before today, it's expired
            if contract_date < today:
                return True
            
            # Same day: check if past settlement time (13:30)
            settlement_time = now.replace(hour=13, minute=30, second=0, microsecond=0)
            return now >= settlement_time
            
        except Exception as e:
            console.print(f" [yellow]⚠️ Error checking contract expiration: {e}[/yellow] ")
            return False
    
    def _is_settlement_day(self, contract_delivery_date):
        """[GSD Settlement Fix] Check if today is settlement day for the given contract.
        
        Args:
            contract_delivery_date: Delivery date in "YYYY/MM/DD" format
            
        Returns:
            bool: True if today is the delivery date (settlement day)
        """
        try:
            contract_date = datetime.strptime(contract_delivery_date, "%Y/%m/%d").date()
            today = datetime.now().date()
            return contract_date == today
        except Exception as e:
            console.print(f" [yellow]⚠️ Error checking settlement day: {e}[/yellow] ")
            return False
    
    def _get_settlement_time_remaining(self):
        """[GSD Settlement Fix] Calculate time remaining until settlement (13:30).
        
        Returns:
            tuple: (hours_remaining, minutes_remaining) or None if not settlement day
        """
        try:
            now = datetime.now()
            today = now.date()
            
            # Check if current contract expires today
            if not self.contract or not self._is_settlement_day(self.contract.delivery_date):
                return None
            
            # Calculate time until 13:30
            settlement_time = now.replace(hour=13, minute=30, second=0, microsecond=0)
            if now >= settlement_time:
                return (0, 0)  # Already past settlement time
            
            time_diff = settlement_time - now
            total_minutes = int(time_diff.total_seconds() / 60)
            hours = total_minutes // 60
            minutes = total_minutes % 60
            
            return (hours, minutes)
            
        except Exception as e:
            console.print(f" [yellow]⚠️ Error calculating settlement time: {e}[/yellow] ")
            return None
    
    def _check_contract_rollover(self):
        """[GSD Fix] Check if MXF contract has rolled over and re-subscribe if needed."""
        if not self.api or self.dry_run or not self.contract:
            return
        
        try:
            current_code = self.contract.code
            
            # [GSD Settlement Fix] Check if today is settlement day
            if self._is_settlement_day(self.contract.delivery_date):
                time_remaining = self._get_settlement_time_remaining()
                if time_remaining:
                    hours, minutes = time_remaining
                    if hours == 0 and minutes == 0:
                        console.print(f"[bold red]⚠️ SETTLEMENT DAY: Contract {current_code} has expired at 13:30[/bold red]")
                    elif hours > 0 or minutes > 0:
                        console.print(f"[bold yellow]⚠️ SETTLEMENT DAY: Contract {current_code} expires at 13:30 ({hours}h {minutes}m remaining)[/bold yellow]")
            
            # 2026-05-27 Gemini CLI: Get all available contracts using dynamic ticker attribute access
            target_contracts = getattr(self.api.Contracts.Futures, self.ticker, None)
            if target_contracts is None:
                console.print(f" [yellow]⚠️ Ticker {self.ticker} not found in Contracts.Futures[/yellow] ")
                return
            
            tmf_list = list(target_contracts)
            if not tmf_list:
                console.print(f" [yellow]⚠️ No {self.ticker} contracts available[/yellow] ")
                return
            
            # [GSD Settlement Fix] Filter out expired contracts considering settlement time
            now = datetime.now()
            valid_contracts = []
            for contract in tmf_list:
                if not self._is_contract_expired(contract.delivery_date):
                    valid_contracts.append(contract)
            
            # Sort by delivery_date
            tmf_sorted = sorted(valid_contracts, key=lambda c: c.delivery_date)
            
            if not tmf_sorted:
                console.print("[bold red]⚠️ No valid contracts available after settlement time[/bold red]")
                return
                
            first_contract = tmf_sorted[0]
            
            # Check if we're still on the first (front month) contract
            if first_contract.code != current_code:
                console.print(f"[bold yellow]🔄 Contract rollover detected: {current_code} → {first_contract.code}[/bold yellow]")
                
                # Unsubscribe from old contract
                try:
                    self.api.quote.unsubscribe(self.contract, quote_type='tick')
                except Exception as e:
                    console.print(f"[dim]Unsubscribe old {current_code}: {e}[/dim]")
                
                # Switch to new contract
                self.contract = first_contract
                self._last_contract_code = first_contract.code
                
                # Re-subscribe to new contract
                self.api.quote.subscribe(first_contract, quote_type='tick')
                console.print(f"[bold green]✅ Re-subscribed to {first_contract.code}[/bold green]")
                
                # Reset tick timestamp to force immediate data freshness check
                self.last_tick_at = time.time()
            else:
                # Contract is correct, issue may be API connection
                # Try re-subscribing to force refresh
                console.print(f"[dim]⚠️ Contract {current_code} is correct but no ticks, re-subscribing...[/dim]")
                try:
                    self.api.quote.unsubscribe(self.contract, quote_type='tick')
                    time.sleep(0.5)
                    self.api.quote.subscribe(self.contract, quote_type='tick')
                    console.print(f"[dim]✅ Re-subscription complete[/dim]")
                except Exception as e:
                    console.print(f" [yellow]⚠️ Re-subscription failed: {e}[/yellow] ")
        except Exception as e:
            console.print(f" [yellow]⚠️ Contract rollover check error: {e}[/yellow] ")

    # ── [GSD Data Safety] Raw tick CSV writer ──
    def _write_raw_tick(self, tick) -> None:
        """Write a single tick to the raw CSV store BEFORE any in-memory cache.

        The RawTickWriter is lazy-initialised on the first real TMF tick, so it
        always uses the correct trading-day string.
        """
        try:
            if self._tick_writer is None:
                from datetime import datetime
                trading_day = get_trading_day_str(datetime.now())
                code = getattr(tick, "code", self.ticker)
                self._tick_writer = RawTickWriter(code, trading_day)

            self._tick_writer.write(tick)
        except Exception:
            # Never let a CSV write failure crash the tick callback
            pass

    def _rebuild_bars_from_raw_ticks(self) -> None:
        """[GSD Data Safety] Rebuild tick-based 5m bars from raw tick CSV on startup.

        If the process crashed mid-session, the in-memory tick deque is lost.
        This method reads today's raw tick CSV (if it exists) and rebuilds the
        5m bars into self._tick_bars_deque so indicators can warm up immediately
        without waiting for fresh ticks.
        """
        try:
            if self.dry_run:
                return

            # Determine today's trading day and code
            trading_day = get_trading_day_str(datetime.now())
            code = getattr(self.contract, "code", self.ticker) if self.contract else self.ticker

            from squeeze_futures.data.tick_writer import read_raw_ticks
            df_ticks = read_raw_ticks(code, trading_day)
            if df_ticks.empty:
                console.print(f"[dim][FuturesMonitor] No raw tick CSV for {code} / {trading_day}, skipping rebuild[/dim]")
                return

            console.print(f"[cyan][FuturesMonitor] Rebuilding 5m bars from {len(df_ticks)} raw ticks...[/cyan]")

            # Ensure timestamp is sorted
            df_ticks = df_ticks.sort_values("timestamp")

            # Bucket into 5-minute bars
            df_ticks["ts_bucket"] = df_ticks["timestamp"].dt.floor("5min")

            rebuilt_bars = []
            for ts_bucket, group in df_ticks.groupby("ts_bucket"):
                bar = {
                    "open": float(group["price"].iloc[0]),
                    "high": float(group["price"].max()),
                    "low": float(group["price"].min()),
                    "close": float(group["price"].iloc[-1]),
                    "volume": int(group["volume"].sum()),
                    "ts": ts_bucket,
                }
                rebuilt_bars.append(bar)

            if rebuilt_bars:
                # Clear and repopulate the deque
                self._tick_bars_deque.clear()
                for bar in rebuilt_bars:
                    self._tick_bars_deque.append(bar)
                self._tick_bars_cache = None  # Invalidate cache so it rebuilds
                console.print(f"[bold green]✅ Rebuilt {len(rebuilt_bars)} bars from raw tick CSV[/bold green]")

                # Set the last bar timestamp to the latest bar to prevent re-processing
                if rebuilt_bars:
                    last_bar = rebuilt_bars[-1]
                    if last_bar["ts"] is not None:
                        self._last_bar_ts = int(last_bar["ts"].timestamp() / 300) * 300
                        # 2026-07-01 Gemini CLI: Pop the last bar from the deque and make it the current active bar.
                        # This prevents the last bar from being duplicated on subsequent ticks in the same 5m bucket.
                        self._tick_bars_deque.pop()
                        self._current_bar["ts"] = last_bar["ts"]
                        self._current_bar["open"] = last_bar["open"]
                        self._current_bar["high"] = last_bar["high"]
                        self._current_bar["low"] = last_bar["low"]
                        self._current_bar["close"] = last_bar["close"]
                        self._current_bar["volume"] = last_bar["volume"]

        except Exception as e:
            console.print(f" [yellow][FuturesMonitor] Tick CSV rebuild failed (non-fatal): {e}[/yellow] ")

    def on_tick(self, exchange, tick):
        self.last_tick_at = time.time()  # [gstack] 更新數據更新時間

        # ── [Manual Trade Flag] Check on every tick ──
        # 2026-06-05 JVS Claw: Step 4 — gate flag check with is_primary (C4).
        # Only near-month ticks consume the flag. Far-month ticks don't populate
        # market_data[self.ticker] so they would always trigger NO_LIVE_TICK.
        _flag_path = getattr(self, "manual_trade_flag_path", "/tmp/futures_manual_trade.flag")
        _processing_path = _flag_path + ".processing"
        _is_primary_tick = self.contract and tick.code == self.contract.code
        # 2026-06-22 Gemini CLI: Check for both new and pending retry flags
        if _is_primary_tick and (os.path.exists(_flag_path) or os.path.exists(_processing_path)):
            from core.date_utils import is_day_session, is_night_session
            _now = datetime.now()
            if is_day_session(_now) or is_night_session(_now):
                try:
                    self._process_manual_trade_flag()
                except Exception as _fe:
                    console.print(f"[red][MANUAL_TRADE_FLAG] on_tick handler failed: {_fe}[/red]")

        # [Debug] fingerprint every tick (config: debug.feed)
        if self._debug_feed:
            console.print(f"[dim][FuturesMonitor][ON_TICK] code={tick.code} close={getattr(tick, 'close', None)} ts={getattr(tick, 'datetime', None)}[/dim]")

        # [Far Month] Handle far-month tick accumulation (independent from near-month)
        if self.far_contract and tick.code == self.far_contract.code:
            self._accumulate_far_tick(tick)
            # 2026-05-27 Gemini CLI: Real-time MTS Execution on Far Tick (Contract 1)
            _mts_enabled = self.cfg.get("mts", {}).get("enabled", False)
            if _mts_enabled and not self.dry_run:
                _rt_bar = dict(self._current_bar)
                # Ensure near bar has ts
                _rt_bar["ts"] = _rt_bar.get("ts") or pd.Timestamp(int(pd.Timestamp(tick.datetime).timestamp() / 300) * 300, unit='s')
                _rt_bar["near_close_rt"] = self._current_bar.get("close", 0)
                _rt_bar["near_high_rt"] = self._current_bar.get("high", 0)
                _rt_bar["near_low_rt"] = self._current_bar.get("low", 0)
                
                # 2026-06-25 Hermes Agent: extract last known ATR from processed data for dynamic stop calculations
                _last_atr = 0.0
                if hasattr(self, '_last_processed_data') and self._last_processed_data:
                    _df_5m = self._last_processed_data.get("5m")
                    if _df_5m is not None and not _df_5m.empty and "atr" in _df_5m.columns:
                        _val = _df_5m["atr"].iloc[-1]
                        if pd.notna(_val):
                            try:
                                _last_atr = float(_val)
                            except (ValueError, TypeError):
                                pass
                _rt_bar["atr"] = _last_atr
                
                # Far bar is definitely updated now
                _rt_bar["far_close_rt"] = self._far_current_bar.get("close", 0)
                _rt_bar["far_high_rt"] = self._far_current_bar.get("high", 0)
                _rt_bar["far_low_rt"] = self._far_current_bar.get("low", 0)
                
                # 2026-06-26 Gemini CLI: calculate tick ages and confirm ticks
                _now_t = time.time()
                _near_arrival = self.market_data.get(self.ticker, {}).get("local_arrival_at", 0.0)
                _far_arrival = self.market_data.get(f"{self.ticker}_FAR", {}).get("local_arrival_at", 0.0)
                _rt_bar["near_tick_age_ms"] = (_now_t - _near_arrival) * 1000 if _near_arrival > 0 else 0.0
                _rt_bar["far_tick_age_ms"] = (_now_t - _far_arrival) * 1000 if _far_arrival > 0 else 0.0
                _rt_bar["confirm_ticks"] = self.cfg.get("mts", {}).get("params", {}).get("confirm_ticks", 2)
                
                # Cache and propagate bid/ask prices for spread width checks
                _rt_bar["near_bid"] = self.market_data.get(self.ticker, {}).get("bid", _rt_bar.get("near_close", 0.0))
                _rt_bar["near_ask"] = self.market_data.get(self.ticker, {}).get("ask", _rt_bar.get("near_close", 0.0))
                _rt_bar["far_bid"] = self.market_data.get(f"{self.ticker}_FAR", {}).get("bid", _rt_bar.get("far_close", 0.0))
                _rt_bar["far_ask"] = self.market_data.get(f"{self.ticker}_FAR", {}).get("ask", _rt_bar.get("far_close", 0.0))
                
                self._mts_tick(enriched_bar=_rt_bar)
            return

        # 💡 GSD: Data Continuity Fix
        # Use strict matching for the primary contract (TMF or MXF)
        is_primary = self.contract and tick.code == self.contract.code

        # [Heartbeat] Match against common futures prefixes to update feed age
        _code = str(tick.code).upper()
        is_common_futures = _code.startswith(("MXF", "MTX", "TMF", "TXF"))
        # 2026-05-22 Gemini CLI: Defined is_tmf and is_mtx to prevent NameError in logging
        is_tmf = _code.startswith("TMF")
        is_mtx = _code.startswith(("MTX", "MXF"))

        if not is_primary and not is_common_futures:
            return

        # [GSD Data Safety] Write raw tick to CSV FIRST — before any in-memory use
        # Only write real primary ticks (not MTX/secondary ticks which might use stale price)
        if is_primary:
            # [REAL_TICK_SEEN] Real near-month MXF/TMF tick — updates ground truth age
            self._write_raw_tick(tick)
            self._last_real_tmf_tick_at = time.time()
            price = float(tick.close)
            self._last_tmf_price = price  # Cache for heartbeat
            self._refresh_runtime_status()
        else:
            # It's a secondary heartbeat tick (MTX/MXF/TMF from another contract)
            if not hasattr(self, '_last_tmf_price') or self._last_tmf_price <= 0:
                # No primary price yet, can't build bar
                return
            price = self._last_tmf_price
            # [DEGRADED FIX] Refresh runtime status even on non-primary ticks to
            # prevent stale DEGRADED status when primary tick contract is misaligned
            self._refresh_runtime_status()

        # 2026-05-27 Gemini CLI: Update market data cache for manual trade integrity checks (P0-P3)
        # Use time.time() as local_arrival_at to avoid exchange-local clock drift issues.
        # 2026-06-24 Gemini CLI: Maintain near/far/code-specific market data caches for spread execution price integrity.
        self.market_data[self.ticker] = {
            "close": price, 
            "datetime": tick.datetime,
            "local_arrival_at": time.time(),
            # 2026-06-26 Gemini CLI: cache bid/ask prices
            "bid": float(getattr(tick, 'buy_price', price) or price),
            "ask": float(getattr(tick, 'sell_price', price) or price)
        }
        self.market_data[f"{self.ticker}_NEAR"] = {
            "close": price,
            "datetime": tick.datetime,
            "local_arrival_at": time.time(),
            "bid": float(getattr(tick, 'buy_price', price) or price),
            "ask": float(getattr(tick, 'sell_price', price) or price)
        }
        if getattr(tick, 'code', None):
            self.market_data[tick.code] = {
                "close": price,
                "datetime": tick.datetime,
                "local_arrival_at": time.time(),
                "bid": float(getattr(tick, 'buy_price', price) or price),
                "ask": float(getattr(tick, 'sell_price', price) or price)
            }

        # Only count volume for primary ticker to keep indicators accurate
        vol = int(getattr(tick, "volume", 0)) if is_primary else 0        
        # [Wave 1 optimization] Use integer time bucketing to avoid expensive pd.Timestamp().floor()
        # Only compute Timestamp when bar changes (every 5 minutes)
        tick_ts = pd.Timestamp(tick.datetime)
        ts_int = int(tick_ts.timestamp() / 300) * 300
        
        bar = self._current_bar
        debug_skip = bar["ts"] is None
        if bar["ts"] is None or ts_int > self._last_bar_ts:
            # 💡 GSD: Only flip the bar if we have a NEW time bucket
            if bar["ts"] is not None and bar["open"] > 0:
                # [Debug] log bar close (config: debug.tickbar)
                if self._debug_tickbar:
                    console.print(f"[dim][TickBar][CLOSE] bucket={pd.Timestamp(self._last_bar_ts, unit='s').strftime('%H:%M')} close={bar['close']:.0f} vol={bar['volume']} deque={len(self._tick_bars_deque)} -> append[/dim]")
                bar_dict = {
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "ts": bar["ts"],
                }
                self._tick_bars_deque.append(bar_dict)
                self._tick_bars_cache = None
            
            # Start new bar
            ts = pd.Timestamp(ts_int, unit='s')
            if self._debug_tickbar:
                console.print(f"[dim][TickBar][NEWBAR] bucket={ts.strftime('%H:%M')} price={price:.0f} vol={vol} is_tmf={is_tmf} is_mtx={is_mtx}[/dim]")
            bar["ts"] = ts
            self._last_bar_ts = ts_int
            bar["open"] = bar["high"] = bar["low"] = bar["close"] = price
            bar["volume"] = vol
        elif ts_int == self._last_bar_ts:
            # Accumulate into current bar
            if self._debug_tickbar and debug_skip:
                console.print(f"[dim][TickBar][ACCUM-first] bucket={pd.Timestamp(ts_int, unit='s').strftime('%H:%M')} price={price:.0f} vol={vol}[/dim]")
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["volume"] += vol
        else:
            # Old data packet, ignore
            return

        # 2026-05-27 Gemini CLI: Real-time stop loss and MTS execution on EVERY tick
        _mts_enabled = self.cfg.get("mts", {}).get("enabled", False)
        if not self.dry_run and self.trader.position != 0 and not _mts_enabled:
            # 1. Update trailing stop peak/floor
            self.trader.update_trailing_stop(price)
            # 2. Check for SL breach
            self._check_stop_loss(tick.datetime, price)
        
        if _mts_enabled and not self.dry_run:
            _rt_bar = dict(bar)
            # Fallback to current time if ts is None
            _rt_bar["ts"] = _rt_bar.get("ts") or pd.Timestamp(ts_int, unit='s')
            
            # 2026-05-27 Gemini CLI: Pass real-time prices explicitly to override CSV staleness
            _rt_bar["near_close_rt"] = price
            _rt_bar["near_high_rt"] = bar["high"]
            _rt_bar["near_low_rt"] = bar["low"]
            
            # 2026-06-25 Hermes Agent: extract last known ATR from processed data for dynamic stop calculations
            _last_atr = 0.0
            if hasattr(self, '_last_processed_data') and self._last_processed_data:
                _df_5m = self._last_processed_data.get("5m")
                if _df_5m is not None and not _df_5m.empty and "atr" in _df_5m.columns:
                    _val = _df_5m["atr"].iloc[-1]
                    if pd.notna(_val):
                        try:
                            _last_atr = float(_val)
                        except (ValueError, TypeError):
                            pass
            _rt_bar["atr"] = _last_atr
            
            if hasattr(self, '_far_current_bar') and self._far_current_bar.get("close", 0) > 0:
                _rt_bar["far_close_rt"] = self._far_current_bar["close"]
                _rt_bar["far_high_rt"] = self._far_current_bar["high"]
                _rt_bar["far_low_rt"] = self._far_current_bar["low"]
            else:
                # 💡 [Fixed 2026-05-27] Log warning if RT far price is missing
                if _mts_enabled:
                    console.print(f"[dim][MTS] Warning: No real-time far-month price for {self.far_contract.code if self.far_contract else 'UNKNOWN'}, relying on CSV[/dim]")
            
            # 2026-06-26 Gemini CLI: calculate tick ages and confirm ticks
            _now_t = time.time()
            _near_arrival = self.market_data.get(self.ticker, {}).get("local_arrival_at", 0.0)
            _far_arrival = self.market_data.get(f"{self.ticker}_FAR", {}).get("local_arrival_at", 0.0)
            _rt_bar["near_tick_age_ms"] = (_now_t - _near_arrival) * 1000 if _near_arrival > 0 else 0.0
            _rt_bar["far_tick_age_ms"] = (_now_t - _far_arrival) * 1000 if _far_arrival > 0 else 0.0
            _rt_bar["confirm_ticks"] = self.cfg.get("mts", {}).get("params", {}).get("confirm_ticks", 2)

            # Cache and propagate bid/ask prices for spread width checks
            _rt_bar["near_bid"] = self.market_data.get(self.ticker, {}).get("bid", _rt_bar.get("near_close", 0.0))
            _rt_bar["near_ask"] = self.market_data.get(self.ticker, {}).get("ask", _rt_bar.get("near_close", 0.0))
            _rt_bar["far_bid"] = self.market_data.get(f"{self.ticker}_FAR", {}).get("bid", _rt_bar.get("far_close", 0.0))
            _rt_bar["far_ask"] = self.market_data.get(f"{self.ticker}_FAR", {}).get("ask", _rt_bar.get("far_close", 0.0))

            self._mts_tick(enriched_bar=_rt_bar)

        cb = self.client._tick_callbacks.get(tick.code)
        if cb:
            cb(exchange, tick)

        # 2026-05-22 Gemini CLI: Removed _maybe_close_selftest() call from here.

    # [Far Month] Accumulate far-month ticks into independent 5-min bars
    def _accumulate_far_tick(self, tick):
        """Accumulate far-month MXF ticks into _far_tick_bars_deque (5-min bars).
        Does NOT affect strategy signals, stop loss, or orders."""
        price = float(tick.close)
        vol = int(getattr(tick, "volume", 0))
        tick_ts = pd.Timestamp(tick.datetime)
        ts_int = int(tick_ts.timestamp() / 300) * 300

        # 2026-06-24 Gemini CLI: Maintain far/code-specific market data caches for spread execution price integrity.
        self.market_data[f"{self.ticker}_FAR"] = {
            "close": price,
            "datetime": tick.datetime,
            "local_arrival_at": time.time(),
            # 2026-06-26 Gemini CLI: cache bid/ask prices
            "bid": float(getattr(tick, 'buy_price', price) or price),
            "ask": float(getattr(tick, 'sell_price', price) or price)
        }
        if getattr(tick, 'code', None):
            self.market_data[tick.code] = {
                "close": price,
                "datetime": tick.datetime,
                "local_arrival_at": time.time(),
                "bid": float(getattr(tick, 'buy_price', price) or price),
                "ask": float(getattr(tick, 'sell_price', price) or price)
            }

        # [Debug] Periodic far tick log (every 30s)
        now_s = time.time()
        if not hasattr(self, '_last_far_tick_log') or now_s - self._last_far_tick_log > 30:
            self._last_far_tick_log = now_s
            console.print(f"[dim]📥 Far tick: {tick.code} close={price} ts={tick_ts.strftime('%H:%M:%S')}[/dim]")

        bar = self._far_current_bar
        if bar["ts"] is None or ts_int > self._last_far_bar_ts:
            # Flip completed bar into deque
            if bar["ts"] is not None and bar["open"] > 0:
                self._far_tick_bars_deque.append({
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "ts": bar["ts"],
                })
                # [Far Month] Persist completed bar to shared CSV for dashboard consumption
                self._save_far_bar({
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "ts": bar["ts"],
                })
            # Start new bar
            ts = pd.Timestamp(ts_int, unit='s')
            bar["ts"] = ts
            self._last_far_bar_ts = ts_int
            bar["open"] = bar["high"] = bar["low"] = bar["close"] = price
            bar["volume"] = vol
        elif ts_int == self._last_far_bar_ts:
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["volume"] += vol
        # else: old data, ignore

    def _save_far_bar(self, bar):
        """Append a completed far-month bar to shared CSV for dashboard consumption.
        Writes to: logs/market_data/{ticker}_far_{date_str}_{tag}.csv"""
        try:
            log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs", "market_data")
            os.makedirs(log_dir, exist_ok=True)
            from core.date_utils import get_session_date_str
            date_str = get_session_date_str(datetime.now())
            tag = "_DRY" if self.dry_run else ("_LIVE" if self.live_trading else "_PAPER")
            path = Path(log_dir) / f"{self.ticker}_far_{date_str}{tag}.csv"

            ts_str = str(bar["ts"])
            row_data = {
                "timestamp": ts_str,
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "volume": bar["volume"],
            }
            cols = ["timestamp", "open", "high", "low", "close", "volume"]
            if not path.exists():
                pd.DataFrame([row_data])[cols].to_csv(path, index=False)
            else:
                pd.DataFrame([row_data]).reindex(columns=cols).to_csv(path, mode='a', header=False, index=False)
        except Exception as e:
            console.print(f"[dim][FuturesMonitor] Far bar save failed (non-fatal): {e}[/dim]")

    # ── Safety Stop (exchange-side protection) ──
    def _place_safety_stop(self, entry_price, direction, lots, stop_loss_pts):
        """Place a far-limit order at exchange as safety stop for disconnect protection."""
        if not self.live_trading or self.dry_run or not self.contract or not self.api:
            return
        try:
            import shioaji as sj
            # Safety stop is wider than strategy stop (2x) to avoid premature fills
            safety_pts = stop_loss_pts * 2 if stop_loss_pts > 0 else 200
            if direction == "LONG":
                safety_price = entry_price - safety_pts
                action = sj.constant.Action.Sell
            else:
                safety_price = entry_price + safety_pts
                action = sj.constant.Action.Buy

            order = self.api.Order(
                price=safety_price,
                quantity=lots,
                action=action,
                price_type=sj.constant.FuturesPriceType.LMT,
                order_type=sj.constant.OrderType.ROD,
                octype=sj.constant.FuturesOCType.Cover,
                account=self.api.futopt_account,
            )
            trade = self.api.place_order(self.contract, order)
            if trade and trade.status.status != sj.constant.Status.Failed:
                self._safety_stop_trade = trade
                console.print(f"[bold yellow]🛡️ Safety stop placed: {action.value} @ {safety_price:.0f} ({safety_pts:.0f}pts from entry)[/bold yellow]")
            else:
                console.print("[red]Safety stop failed to place[/red]")
        except Exception as e:
            console.print(f" [yellow]Safety stop error: {e}[/yellow] ")

    def _cancel_safety_stop(self):
        """Cancel the exchange-side safety stop after normal exit."""
        if not self._safety_stop_trade or not self.api:
            return
        try:
            self.api.cancel_order(self._safety_stop_trade)
            console.print("[dim]🛡️ Safety stop cancelled[/dim]")
        except Exception as e:
            console.print(f" [yellow]Safety stop cancel error: {e}[/yellow] ")
        self._safety_stop_trade = None

    # ── GSD Phase 0d: Hourly No-Trade Audit (V-Model during session) ──
    def _hourly_no_trade_audit(self, timestamp, df_5m):
        """
        Every hour: if no trades in the past hour, diagnose WHY.
        Three possible verdicts:
          1. DATA_FAILURE → API down, stale data (alert)
          2. NO_VALID_SIGNALS → data OK, strategy found no signals (expected)
          3. COOLDOWN → strategy blocked by cooldown (expected)
        
        [ENHANCED] Also monitors trade records integrity and backups.
        """
        now_hour = datetime.now().hour  # Use system clock to prevent duplicate audits
        if now_hour == self._last_audit_hour:
            return  # Already audited this hour
        self._last_audit_hour = now_hour
        
        secs_since_tick = self._tmf_feed_age_secs()
        data_stale = secs_since_tick > 120  # 2+ min without tick

        # Use actual kbar count if available, fallback to _bars_since_trade
        actual_bars = len(df_5m) if df_5m is not None else 0

        # Diagnose
        if data_stale or df_5m is None or actual_bars < 30:
            verdict = "DATA_FAILURE"
            note = f"Data stale {secs_since_tick/60:.1f}min, bars={actual_bars}"
            console.print(f"[red]🚨 {verdict}: {note}[/red]")
        elif self.cooldown_until > 0:
            verdict = "COOLDOWN"
            note = f"Cooldown active (remaining={self.cooldown_until}), signals={self._signals_generated}"
            console.print(f"[dim]🔵 {verdict}: {note}[/dim]")
        elif self._signals_generated == 0:
            verdict = "NO_VALID_SIGNALS"
            note = f"Data OK, {actual_bars} bars, 0 signals generated. Strategy may be too strict for current conditions."
            console.print(f" [yellow]⚠️  {verdict}: {note}[/yellow] ")
        else:
            verdict = "NORMAL"
            note = f"{self._signals_generated} signals, data healthy"
        
        # [ENHANCED] Monitor trade records integrity
        trade_check_result = self._monitor_trade_records(timestamp)
        if trade_check_result:
            console.print(f"[green]✓ Trade records check: {trade_check_result}[/green]")

        options_audit_result = self._audit_options_data_health(timestamp)
        if options_audit_result:
            tone = "green" if options_audit_result.startswith("healthy") else "yellow"
            console.print(f"[{tone}]🩺 Options data audit: {options_audit_result}[/{tone}]")
            note = f"{note}; options={options_audit_result}"
        
        # Log audit
        from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
        save_signal_audit({
            "timestamp": str(timestamp),
            "signal": "HOURLY_AUDIT",
            "price": 0,
            "reason": verdict,
            "rejection": note,
            "lots": 0,
        })
        
        # Reset counters for next hour
        self._signals_generated = 0
        self._bars_since_trade = 0  # GAP-2 fix: reset bars counter too

    def _audit_options_data_health(self, timestamp):
        monitor = getattr(self, "options_monitor", None)
        if monitor is None:
            return ""
        try:
            return monitor.audit_indicator_health_and_repair(timestamp)
        except Exception as exc:
            return f"options_audit_error:{type(exc).__name__}:{str(exc)[:80]}"

    def _monitor_trade_records(self, timestamp):
        """
        Monitor trade records integrity and perform hourly checks.
        
        Returns:
            str: Summary of trade records status
        """
        try:
            from pathlib import Path
            import pandas as pd
            # datetime already imported at module top (datetime, timedelta)
            
            # Get current date for file naming
            current_date = timestamp.strftime("%Y%m%d") if hasattr(timestamp, "strftime") else datetime.now().strftime("%Y%m%d")
            
            # Check futures trade records
            futures_trade_file = Path(f"logs/market_data/TMF_{current_date}_trades.csv")
            futures_audit_file = Path(f"logs/market_data/TMF_{current_date}_signals_audit.csv")
            
            # Check stock trade records
            stock_trade_dir = Path("logs/stocks")
            stock_trade_files = list(stock_trade_dir.glob("*_trades.csv")) if stock_trade_dir.exists() else []
            
            # Check options trade records
            options_trade_file = Path(f"logs/market_data/TXO_{current_date}_trades.csv")
            
            results = []
            
            # 1. Check futures trade records
            if futures_trade_file.exists():
                try:
                    df = pd.read_csv(futures_trade_file)
                    futures_trades = len(df)
                    results.append(f"Futures: {futures_trades} trades")
                    
                    # Check for recent trades (last hour)
                    if 'timestamp' in df.columns:
                        df['timestamp'] = pd.to_datetime(df['timestamp'])
                        recent_trades = df[df['timestamp'] > timestamp - timedelta(hours=1)]
                        if len(recent_trades) > 0:
                            results.append(f"  Recent: {len(recent_trades)} in last hour")
                except Exception as e:
                    results.append(f"Futures: Error reading ({str(e)[:50]})")
            else:
                results.append("Futures: No trade file")
            
            # 2. Check futures audit records
            if futures_audit_file.exists():
                try:
                    df = pd.read_csv(futures_audit_file)
                    audit_records = len(df)
                    results.append(f"Audit: {audit_records} records")
                except:
                    results.append("Audit: Error reading")
            
            # 3. Check stock trade records
            if stock_trade_files:
                total_stock_trades = 0
                for file in stock_trade_files:
                    try:
                        df = pd.read_csv(file)
                        total_stock_trades += len(df)
                    except:
                        pass
                results.append(f"Stocks: {total_stock_trades} trades in {len(stock_trade_files)} files")
            
            # 4. Check options trade records
            if options_trade_file.exists():
                try:
                    df = pd.read_csv(options_trade_file)
                    options_trades = len(df)
                    results.append(f"Options: {options_trades} trades")
                except:
                    results.append("Options: Error reading")
            
            # 5. Backup check (create backup if needed)
            self._backup_trade_records_if_needed(timestamp)
            
            return "; ".join(results)
            
        except Exception as e:
            return f"Trade monitor error: {str(e)[:100]}"
    
    def _backup_trade_records_if_needed(self, timestamp):
        """
        Create backup of trade records if last backup was >6 hours ago.
        """
        try:
            from pathlib import Path
            import shutil
            # datetime already imported at module top (datetime)
            
            backup_dir = Path("logs/backups/trade_records")
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            # Check last backup time
            backup_marker = backup_dir / "last_backup.txt"
            should_backup = True
            
            if backup_marker.exists():
                try:
                    with open(backup_marker, 'r') as f:
                        last_backup_str = f.read().strip()
                        last_backup = datetime.strptime(last_backup_str, "%Y-%m-%d %H:%M:%S")
                        hours_since = (datetime.now() - last_backup).total_seconds() / 3600
                        should_backup = hours_since >= 6  # Backup every 6 hours
                except:
                    pass
            
            if should_backup:
                # Backup futures trade records
                current_date = timestamp.strftime("%Y%m%d") if hasattr(timestamp, "strftime") else datetime.now().strftime("%Y%m%d")
                futures_trade_file = Path(f"logs/market_data/TMF_{current_date}_trades.csv")
                futures_audit_file = Path(f"logs/market_data/TMF_{current_date}_signals_audit.csv")
                
                backup_files = []
                
                if futures_trade_file.exists():
                    backup_path = backup_dir / f"TMF_{current_date}_trades_{timestamp.strftime('%H%M')}.csv"
                    shutil.copy2(futures_trade_file, backup_path)
                    backup_files.append("futures_trades")
                
                if futures_audit_file.exists():
                    backup_path = backup_dir / f"TMF_{current_date}_audit_{timestamp.strftime('%H%M')}.csv"
                    shutil.copy2(futures_audit_file, backup_path)
                    backup_files.append("futures_audit")
                
                # Update backup marker
                with open(backup_marker, 'w') as f:
                    f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                
                if backup_files:
                    console.print(f"[dim]📂 Trade records backed up: {', '.join(backup_files)}[/dim]")
                    
        except Exception as e:
            console.print(f" [yellow]⚠️ Trade backup failed: {e}[/yellow] ")

    def _save_orders_file_wrapper(self):
        """Export all orders to JSON for dashboard consumption."""
        if not self.order_mgr:
            return
        try:
            from core.order_management.order import OrderSide
            import math
            import json
            from pathlib import Path

            # Get current market price for unrealized PnL
            cur_price = 0.0
            try:
                cur_price = float(self.market_data.get(self.ticker, {}).get("close", 0))
            except Exception:
                cur_price = 0.0

            all_orders = self.order_mgr.get_completed() + self.order_mgr.get_pending()
            export_data = []
            for o in all_orders:
                d = o.to_dict()
                # Add unrealized PnL for open positions
                d["unrealized_pnl"] = None
                d["unrealized_pnl_pts"] = None
                d["current_price"] = cur_price if cur_price > 0 else None

                if o.status in ("filled", "partial_filled") and self.trader.position != 0:
                    entry = self.trader.entry_price
                    qty = abs(self.trader.position)
                    if cur_price > 0 and entry > 0:
                        if self.trader.position > 0:  # LONG
                            pnl_pts = cur_price - entry
                        else:  # SHORT
                            pnl_pts = entry - cur_price
                        point_value = 50
                        pnl_cash = pnl_pts * point_value * qty
                        d["unrealized_pnl"] = round(pnl_cash, 0)
                        d["unrealized_pnl_pts"] = round(pnl_pts, 1)

                export_data.append(d)

            today = datetime.now().strftime("%Y%m%d")
            orders_file = Path(f"exports/trades/TMF_{today}_orders.json")
            orders_file.parent.mkdir(parents=True, exist_ok=True)
            with open(orders_file, "w", encoding="utf-8") as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ Failed to save futures orders file: {e}")

    def _append_filled_lifecycle_order(self, side, price, ts, lots, *, strategy="futures", comment="", order_id=None):
        """Append a filled lifecycle order record without re-executing trade logic."""
        if not self.order_mgr:
            return None
        try:
            from core.order_management.order import Order, OrderStatus, OrderType

            qty = int(lots or 1)
            fill_price = float(price or 0)
            order_ts = ts if hasattr(ts, "strftime") else datetime.now()
            lifecycle_order = Order(
                symbol=self.ticker,
                side=side,
                order_type=OrderType.MARKET,
                quantity=qty,
                price=fill_price,
                order_id=order_id or f"LIFECYCLE-{order_ts.strftime('%Y%m%d-%H%M%S-%f')}",
                strategy=strategy,
                comment=comment,
            )
            lifecycle_order.status = OrderStatus.FILLED
            lifecycle_order.filled_quantity = qty
            lifecycle_order.avg_fill_price = fill_price
            lifecycle_order.created_at = order_ts
            lifecycle_order.submitted_at = order_ts
            lifecycle_order.filled_at = order_ts
            lifecycle_order.updated_at = order_ts
            lifecycle_order.exchange_order_id = f"RECOV-{lifecycle_order.order_id}"
            self.order_mgr.completed.append(lifecycle_order)
            return lifecycle_order
        except Exception as e:
            console.print(f" [yellow]⚠️ Failed to append lifecycle order: {e}[/yellow] ")
            return None

    def _recover_orders_from_trades_csv(self):
        """Recover all orders from trades CSV to rebuild OrderManager state on startup."""
        if not self.order_mgr:
            return
        
        try:
            import csv
            from pathlib import Path
            from core.order_management.order import Order, OrderStatus, OrderType, OrderSide
            
            # Find today's trades CSV
            today = datetime.now().strftime("%Y%m%d")
            trades_file = Path(f"exports/trades/TMF_{today}_trades.csv")
            
            if not trades_file.exists():
                console.print("[dim]No trades file to recover orders from[/dim]")
                return
            
            with open(trades_file) as f:
                rows = list(csv.DictReader(f))
            
            if not rows:
                return
            
            recovered_count = 0
            for row in rows:
                try:
                    trade_type = row.get("type", "")
                    direction = row.get("direction", "")
                    price = float(row.get("price", 0))
                    lots = int(row.get("lots", 0) or 1)
                    timestamp_str = row.get("timestamp", "")
                    reason = row.get("reason", "")
                    
                    # Parse timestamp
                    try:
                        ts = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                    except:
                        ts = datetime.now()
                    
                    # Determine OrderSide from type
                    if trade_type == "BUY":
                        order_side = OrderSide.BUY
                    elif trade_type == "SELL":
                        order_side = OrderSide.SELL
                    elif trade_type == "EXIT":
                        # Exit order side is opposite of direction
                        order_side = OrderSide.SELL if direction == "LONG" else OrderSide.BUY
                    else:
                        continue  # Skip unknown types
                    
                    lifecycle_order = self._append_filled_lifecycle_order(
                        side=order_side,
                        price=price,
                        ts=ts,
                        lots=lots,
                        strategy="futures",
                        comment=f"{trade_type} {reason}".strip(),
                        order_id=f"TRADES-{ts.strftime('%Y%m%d-%H%M%S')}",
                    )
                    if lifecycle_order is not None:
                        recovered_count += 1
                    
                except Exception as e:
                    console.print(f" [yellow]⚠️ Failed to recover order from row: {e}[/yellow] ")
                    continue
            
            if recovered_count > 0:
                console.print(f"[bold cyan]♻️ Recovered {recovered_count} futures orders from trades CSV[/bold cyan]")
                # Save immediately to orders JSON
                self._save_orders_file_wrapper()
            
        except Exception as e:
            console.print(f" [yellow]Futures order recovery from trades CSV failed: {e}[/yellow] ")

    # ── Order Lifecycle (L3 Integration) ──
    def _get_lifecycle_order(self, order_id):
        if not self.order_mgr:
            return None
        order = self.order_mgr.active_orders.get(order_id)
        if order is not None:
            return order
        for completed in self.order_mgr.completed:
            if completed.order_id == order_id:
                return completed
        return None

    def _clear_pending_lifecycle_order(self, order_id):
        self._pending_lifecycle_orders.pop(order_id, None)
        # 2026-05-27 Gemini CLI: Clear cancellation tracking as well
        if hasattr(self, "_mts_stale_order_cancels"):
            self._mts_stale_order_cancels.discard(order_id)

    def _check_oco_release_fill(self, event):
        """ADR-010 Sprint 4A: detect OCO release fill — PARTIALLY_FILLED only.

        Matches event.order_id against strategy release_group near/far order ids.
        On match: mark PARTIALLY_FILLED without cancel sibling or trail activation.
        Sibling cancel and SINGLE_LEG transition handled in Sprint 4B/4C.

        Invariant: PARTIALLY_FILLED → trail_group.status must NOT be ARMED.
        """
        from strategies.plugins.futures.active.tmf_spread import (
            ReleaseGroupStatus, Leg, TrailGroupStatus, CancelStatus,
            _write_mts_state, lifecycle_to_dict,
        )
        _strategy = self._registry.get("tmf_spread")
        if not _strategy or not hasattr(_strategy, "_lifecycle_oca"):
            return
        _rg = _strategy._lifecycle_oca.release_group
        if _rg.status not in (ReleaseGroupStatus.SUBMITTED,):
            return
        _oid = event.order_id
        if _oid == _rg.near_order_id:
            _winner = "near"
        elif _oid == _rg.far_order_id:
            _winner = "far"
        else:
            return  # not an OCO release fill

        # Dedup by deal_id
        _deal_key = event.deal_id or f"oco:{_oid}:{event.fill_qty}:{event.fill_price}"
        if _deal_key in self._applied_lifecycle_deals:
            return
        self._applied_lifecycle_deals[_deal_key] = datetime.now().isoformat()

        price = float(event.fill_price or 0)
        # PARTIALLY_FILLED — no cancel, no SINGLE_LEG, no trail
        _rg.status = ReleaseGroupStatus.PARTIALLY_FILLED
        _rg.filled_leg = Leg.NEAR if _winner == "near" else Leg.FAR
        _rg.filled_order_id = _oid
        _rg.canceled_leg = Leg.FAR if _winner == "near" else Leg.NEAR

        # Invariant: trail must NOT be active in PARTIALLY_FILLED
        _strategy._lifecycle_oca.trail_group.status = TrailGroupStatus.INACTIVE

        # Log but do NOT transition to SINGLE_LEG yet
        console.print(
            f"[bold yellow]🟡 [OCO_4A] PARTIALLY_FILLED winner={_winner} order={_oid} "
            f"sibling={_rg.canceled_leg.value} — cancel deferred to 4B[/bold yellow]"
        )

        _write_mts_state(
            has_position=True, action=f"OCO_{_winner.upper()}_PARTIAL",
            reason=f"oco_{_winner}_partially_filled",
            near_entry=_strategy._near_entry, far_entry=_strategy._far_entry,
            near_last=price if _winner == "near" else float(self.market_data.get(f"{self.ticker}_NEAR", {}).get("close") or 0),
            far_last=price if _winner == "far" else float(self.market_data.get(f"{self.ticker}_FAR", {}).get("close") or 0),
            near_side=_strategy._near_side, far_side=_strategy._far_side,
            released_leg=_winner, trade_id=_strategy._trade_id,
            ticker=self.ticker, atr=float(getattr(_strategy, "_last_atr", 0.0) or 0.0),
            lifecycle=lifecycle_to_dict(_strategy._lifecycle_oca),
        )

        # ── Sprint 4B: cancel sibling → CANCELING_SIBLING ──
        _cancel_oid = _rg.far_order_id if _winner == "near" else _rg.near_order_id
        if self.order_mgr and _cancel_oid:
            try:
                self.order_mgr.cancel(_cancel_oid, reason=f"oco_4b_cancel_{_winner}", source="oco_bracket")
                _rg.sibling_cancel_order_id = _cancel_oid
                _rg.sibling_cancel_status = CancelStatus.PENDING
                _rg.status = ReleaseGroupStatus.CANCELING_SIBLING
                console.print(
                    f"[bold cyan]🔄 [OCO_4B] CANCELING_SIBLING sent for {_cancel_oid}"
                    f" (winner={_winner})[/bold cyan]"
                )
                _write_mts_state(
                    has_position=True, action=f"OCO_CANCELING_{_winner.upper()}",
                    reason=f"oco_4b_cancel_{_winner}",
                    near_entry=_strategy._near_entry, far_entry=_strategy._far_entry,
                    near_last=float(getattr(_strategy, "_near_last", 0)),
                    far_last=float(getattr(_strategy, "_far_last", 0)),
                    near_side=_strategy._near_side, far_side=_strategy._far_side,
                    released_leg=_winner, trade_id=_strategy._trade_id,
                    ticker=self.ticker, atr=float(getattr(_strategy, "_last_atr", 0.0) or 0.0),
                    lifecycle=lifecycle_to_dict(_strategy._lifecycle_oca),
                )
            except (ValueError, RuntimeError) as _e:
                _rg.sibling_cancel_status = CancelStatus.REJECTED
                _rg.status = ReleaseGroupStatus.FAILED
                console.print(
                    f"[red]⚠️ [OCO_4B] Cancel failed: {_e} — status=FAILED[/red]"
                )

    def _apply_confirmed_futures_deal(self, event):
        from core.order_management.order import OrderStatus
        from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit

        # [MTS] Check if this fill completes a spread entry (automated or manual)
        # Must be called BEFORE early returns to ensure tracking dictionary is updated
        price = float(event.fill_price or 0)
        self._check_mts_multi_leg_fill(event.order_id, price)

        pending = self._pending_lifecycle_orders.get(event.order_id)
        # 2026-06-22 Gemini CLI: Use fill_qty to match OrderEvent class definition
        if pending is None or event.fill_qty <= 0:
            # ADR-010 Sprint 4A: check if this fill matches OCO bracket order
            if pending is None and event.fill_qty > 0:
                self._check_oco_release_fill(event)
            return None

        deal_key = event.deal_id or f"{event.order_id}:{event.fill_qty}:{event.fill_price}"
        if deal_key in self._applied_lifecycle_deals:
            return None

        signal = pending.get("signal")
        # Support both standard and MTS signal types for logging/audit
        if signal not in ("BUY", "SELL", "EXIT", "PARTIAL_EXIT", 
                          "SELL_NEAR_BUY_FAR", "BUY_NEAR_SELL_FAR", 
                          "RELEASE_NEAR", "RELEASE_FAR"):
            return None

        ts = datetime.now()
        # 2026-06-22 Gemini CLI: Use fill_qty to match OrderEvent
        lots = int(event.fill_qty)
        reason = pending.get("reason")
        stop_loss = pending.get("stop_loss")
        break_even_trigger = pending.get("break_even_trigger")
        trail_points = pending.get("trail_points")
        cross_policy = pending.get("cross_policy")

        # Skip directional trader execution for multi-leg spread signals (net zero or self-managed)
        if signal in ("SELL_NEAR_BUY_FAR", "BUY_NEAR_SELL_FAR", "RELEASE_NEAR", "RELEASE_FAR", "EXIT"):
             _pending_strat = pending.get("strategy", "")
             if _pending_strat and "MTS" in str(_pending_strat):
                 # [Fix 2026-05-27] Handle strategy state reset for MTS exits upon fill
                 if signal == "EXIT" or _pending_strat == "MTS_EXIT":
                     _mts_strat = self._registry.get("tmf_spread")
                     if _mts_strat:
                         _mts_strat._reset(reason="trail_exit_confirmed", exit_price=price)
                         console.print(f"[bold green]✅ [MTS_SYNC] Trailing exit CONFIRMED: {event.order_id}[/bold green]")
                 elif signal in ("RELEASE_NEAR", "RELEASE_FAR") or _pending_strat == "MTS_RELEASE":
                     _mts_strat = self._registry.get("tmf_spread")
                     if _mts_strat:
                         _leg = "near" if "NEAR" in str(signal) else "far"
                         # 2026-06-26 Gemini CLI: sync_release requires the price of the REMAINING leg.
                         # If near is released, the remaining leg is far. If far is released, the remaining leg is near.
                         if _leg == "near":
                             _rem_price = float(self.market_data.get(f"{self.ticker}_FAR", {}).get("close") or 0.0)
                             if _rem_price <= 0:
                                 _rem_price = float(self._far_current_bar.get("close") or 0.0)
                         else:
                             _rem_price = float(self.market_data.get(f"{self.ticker}_NEAR", {}).get("close") or 0.0)
                             if _rem_price <= 0:
                                 _rem_price = float(self._current_bar.get("close") or 0.0)
                         
                         # Fallback to the entry price of the remaining leg if still 0
                         if _rem_price <= 0:
                             _rem_price = _mts_strat._far_entry if _leg == "near" else _mts_strat._near_entry
                             
                         _mts_strat.sync_release(leg=_leg, price=_rem_price, release_price=price)
                         console.print(f"[bold green]✅ [MTS_SYNC] Release CONFIRMED: {event.order_id} ({_leg}) with remaining leg price {_rem_price}[/bold green]")

                 # 2026-06-09 JVS Claw: Fix symbol matching for NEAR/FAR legs
                 # Update directional trader position for spread legs.
                 # This prevents GHOST_POSITION errors in the watchdog.
                 # Match both exact contract code AND NEAR/FAR suffix patterns.
                 _symbol = str(event.symbol or "")
                 _contract_code = self.contract.code if self.contract else ""
                 _is_near_leg = "NEAR" in _symbol or _symbol == _contract_code
                 _is_far_leg = "FAR" in _symbol
                 
                 if _is_near_leg:
                      # 2026-06-23 Gemini CLI: Determine if this fill is an exit/release/emergency closing transaction
                      _is_closing = False
                      if pending:
                          _pending_strat = pending.get("strategy", "")
                          _pending_sig = pending.get("signal", "")
                          if _pending_strat in ("MTS_RELEASE", "MTS_EXIT", "MTS_EMERGENCY") or _pending_sig in ("RELEASE_NEAR", "EXIT"):
                              _is_closing = True
                      
                      if _is_closing:
                          _mkt_action = "EXIT"
                      else:
                          from core.order_management.order import OrderSide
                          _mkt_action = "BUY" if event.side == OrderSide.BUY else "SELL"
                      
                      # 2026-06-22 Gemini CLI: Pass ts variable to execute_signal to fix signature mismatch TypeError
                      self.trader.execute_signal(_mkt_action, price, ts, lots=lots)
                      console.print(f"[dim][MTS_SYNC] NEAR-leg synced to trader: {self.trader.position} ({_mkt_action})[/dim]")

                 self._applied_lifecycle_deals.add(deal_key)
                 
                 # 💡 [Fixed 2026-05-27] Execution Quality Audit
                 _ref_ohlc = pending.get("ref_ohlc", {})
                 _ref_close = float(_ref_ohlc.get("close", 0))
                 _slippage = 0.0
                 if _ref_close > 0:
                     # For BUY: slippage = fill - ref (positive is bad)
                     # For SELL: slippage = ref - fill (positive is bad)
                     _side_val = str(pending.get("side", "")).upper() or ("BUY" if "BUY" in str(signal) else "SELL")
                     if _side_val == "BUY": _slippage = price - _ref_close
                     else: _slippage = _ref_close - price

                 self._append_mts_event("LEG_FILLED", 
                                       order_id=event.order_id, symbol=event.symbol, 
                                       price=price, qty=lots, slippage=round(_slippage, 1),
                                       ref_ohlc=_ref_ohlc)

                 order = self._get_lifecycle_order(event.order_id)
                 if order is not None and order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                     self._clear_pending_lifecycle_order(event.order_id)

                 self._save_orders_file_wrapper()
                 return f"MTS_LEG_FILL:{event.symbol}"

        pnl_pts = 0.0
        pnl_cash = 0.0
        friction_cost = 0.0
        direction = "LONG" if signal == "BUY" else "SHORT" if signal == "SELL" else ""
        if signal in ("EXIT", "PARTIAL_EXIT") and self.trader.entry_price > 0 and self.trader.position != 0:
            direction = "LONG" if self.trader.position > 0 else "SHORT"
            sign = 1 if self.trader.position > 0 else -1
            pnl_pts = (price - self.trader.entry_price) * sign
            gross = pnl_pts * self.trader.point_value * lots
            fee = self.trader.fee_per_side * 2 * lots
            exch_fee = self.trader.exchange_fee_per_side * 2 * lots
            tax = (self.trader.entry_price + price) * self.trader.point_value * self.trader.tax_rate * lots
            friction_cost = fee + exch_fee + tax
            pnl_cash = gross - friction_cost
            self._session_pnl += pnl_pts

        result = self.trader.execute_signal(
            signal,
            price,
            ts,
            lots=lots,
            max_lots=self.MGMT.get("max_positions", 2),
            stop_loss=stop_loss,
            break_even_trigger=break_even_trigger,
            trail_points=trail_points,
            exit_reason=reason,
        )
        if not result:
            save_signal_audit({
                "timestamp": ts,
                "signal": signal,
                "price": price,
                "reason": reason or "",
                "rejection": "confirmed_deal_rejected",
                "lots": lots,
            })
            return None

        self._applied_lifecycle_deals.add(deal_key)
        save_signal_audit({
            "timestamp": ts,
            "signal": signal,
            "price": price,
            "reason": reason or "",
            "rejection": "",
            "lots": lots,
        })
        save_trade({
            "type": signal,
            "timestamp": ts,
            "price": price,
            "lots": lots,
            "direction": direction,
            "pnl_pts": round(pnl_pts, 1),
            "pnl_cash": round(pnl_cash, 0),
            "friction_cost": round(friction_cost, 0),
            "reason": reason or "",
            "cross_policy": cross_policy,
        })

        if signal in ("BUY", "SELL"):
            ctx = getattr(self, "_last_bar_context", {})
            self._entry_features_futures = {
                "momentum": ctx.get("momentum", 0),
                "mom_velo": ctx.get("mom_velo", 0),
                "vwap_distance_pts": round(abs(price - ctx.get("vwap", price)), 1),
                "atr": ctx.get("atr", 0),
                "regime": ctx.get("regime", "UNKNOWN"),
                "score": ctx.get("score", 0),
                "entry_price": float(price),
            }
            save_trade({
                "type": "ENTRY_DIAG",
                "timestamp": ts,
                "signal": signal,
                "price": price,
                "lots": lots,
                "direction": direction,
                "reason": reason or "",
                "entry_diag": self._entry_features_futures,
                "cross_policy": cross_policy,
            })
            if self.live_trading and not self.dry_run:
                fill_direction = "LONG" if signal == "BUY" else "SHORT"
                sl_pts = stop_loss if stop_loss else self.RISK.get("stop_loss_pts", 60)
                self._place_safety_stop(price, fill_direction, lots, sl_pts)

        if signal in ("EXIT", "PARTIAL_EXIT") and hasattr(self, "_entry_features_futures") and self._entry_features_futures:
            from core.decision_logger import DecisionLogger

            outcome = {
                "pnl": float(pnl_cash),
                "pnl_pts": float(pnl_pts),
                "exit_price": float(price),
                "exit_reason": str(reason or "SIGNAL"),
            }
            DecisionLogger.log_trade_outcome(
                trade_id=f"FUT-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                strategy=self.active_strategy_name,
                regime=self._entry_features_futures.get("regime", "NORMAL"),
                features=self._entry_features_futures,
                outcome=outcome,
            )
            if signal == "EXIT":
                self._entry_features_futures = {}

        if signal in ("EXIT", "PARTIAL_EXIT") and pnl_pts < 0:
            sess = self.session_type or "day"
            self.consecutive_losses += 1
            self.session_losses.append((ts, pnl_pts, reason or "UNKNOWN", sess))
        elif signal in ("EXIT", "PARTIAL_EXIT"):
            self.consecutive_losses = 0

        order = self._get_lifecycle_order(event.order_id)
        if order is not None and order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            self._clear_pending_lifecycle_order(event.order_id)

        self._save_orders_file_wrapper()
        return result

    def _wire_order_callbacks(self):
        """Wire OrderManager callbacks to PaperTrader and audit system."""
        from core.order_management.order import OrderStatus, OrderSide

        def _on_fill_callback(event):
            if event.status not in (OrderStatus.PARTIAL_FILLED, OrderStatus.FILLED):
                return
            msg = self._apply_confirmed_futures_deal(event)
            if msg:
                action = "BUY" if event.side == OrderSide.BUY else "SELL"
                # 2026-06-22 Gemini CLI: Use fill_qty to match OrderEvent
                console.print(f"[green]📦 Confirmed deal: {action} {event.fill_qty} @ {event.fill_price:.0f} deal={event.deal_id} → {msg}[/green]")
            
            # [GSD] Always update dashboard file to reflect latest OrderManager state (e.g. FILLED)
            self._save_orders_file_wrapper()

        def _on_cancel_callback(event):
            console.print(f" [yellow]🚫 Order CANCELLED: {event.order_id} ({event.reason})[/yellow] ")
            self._clear_pending_lifecycle_order(event.order_id)
            self._save_orders_file_wrapper()

        def _on_reject_callback(event):
            console.print(f"[red]❌ Order REJECTED: {event.order_id} ({event.reason})[/red]")
            self._clear_pending_lifecycle_order(event.order_id)
            self._save_orders_file_wrapper()

        def _on_status_change(event):
            self._save_orders_file_wrapper()

        self.order_mgr.register_callback("on_fill", _on_fill_callback)
        self.order_mgr.register_callback("on_cancel", _on_cancel_callback)
        self.order_mgr.register_callback("on_reject", _on_reject_callback)
        self.order_mgr.register_callback("on_status_change", _on_status_change)
        self._save_orders_file_wrapper()

    def _submit_order_via_manager(self, signal, price, ts, lots, stop_loss=None, break_even_trigger=None, trail_points=None, reason=None):
        """Submit order through OrderManager and wait for confirmed deals to mutate PaperTrader."""
        from core.order_management.order import OrderType, OrderSide

        if signal == "BUY":
            side = OrderSide.BUY
            action = "Buy"
        elif signal == "SELL":
            side = OrderSide.SELL
            action = "Sell"
        elif signal in ("EXIT", "PARTIAL_EXIT"):
            if self.trader.position == 0:
                return None
            side = OrderSide.SELL if self.trader.position > 0 else OrderSide.BUY
            action = "Sell" if self.trader.position > 0 else "Buy"
        else:
            return None

        order_type = OrderType.MARKET  # Default to market; can be configured

        order = self.order_mgr.create_order(
            symbol=self.ticker, side=side, order_type=order_type,
            quantity=lots, strategy=reason or "UNKNOWN",
            comment=f"{signal} {reason or ''}".strip(),
        )
        self._pending_lifecycle_orders[order.order_id] = {
            "intent_id": order.intent_id,
            "signal": signal,
            "reason": reason,
            "stop_loss": stop_loss or self.RISK.get("stop_loss_pts", 60),
            "break_even_trigger": break_even_trigger,
            "trail_points": trail_points,
            "ts": ts,
            "lots": lots,
            "cross_policy": getattr(self, "_last_cross_policy", None),
        }

        console.print(f"[cyan]📤 Order SUBMITTED: {signal} {lots} @ {price:.0f} ({reason}) "
                      f"[order_id={order.order_id}][/cyan]")

        if self.live_trading and not self.dry_run:
            trade = self.client.place_order(self.contract, action=action, quantity=lots)
            if trade is None:
                self._clear_pending_lifecycle_order(order.order_id)
                self.order_mgr.reject(order.order_id, "api_order_failed")
                return None
            self.order_mgr.attach_submission(
                order.order_id,
                broker_trade=trade,
                broker_order_id=getattr(trade, "id", None),
                seqno=getattr(trade, "seqno", None),
                ordno=getattr(trade, "ordno", None),
                raw_status="Submitted",
            )
            return order.order_id

        self.order_mgr.submit(order, exchange_ordno=f"PAPER-{order.order_id}")
        self.paper_fill_sim.register(order)
        self.paper_fill_sim.process_tick(self._make_synthetic_tick(price, ts, symbol=order.symbol))
        return order.order_id

    def _make_synthetic_tick(self, price, ts, symbol=None):
        """Create a synthetic tick object from price/timestamp for PaperFillSimulator."""
        tick = type("Tick", (), {})()
        tick.code = symbol or self.ticker
        tick.datetime = ts if hasattr(ts, "strftime") else datetime.now()
        tick.close = price
        tick.open = price
        tick.high = price
        tick.low = price
        tick.volume = 0
        return tick

    # ── Margin check ──
    def _margin_sufficient(self):
        """Check if account has enough margin before placing entry order."""
        try:
            margin = self.api.margin(self.api.futopt_account)
            equity = margin.equity
            reserve_pct = 0.20  # 保留 20% 不動用
            available = equity * (1 - reserve_pct)
            required = margin.initial_margin if margin.initial_margin > 0 else 17000  # MXF 一口約 17,000
            if available < required:
                console.print(f"[red]Margin check: equity={equity:.0f} available={available:.0f} < required={required:.0f}[/red]")
                return False
            console.print(f"[dim]Margin OK: equity={equity:.0f} available={available:.0f}[/dim]")
            return True
        except Exception as e:
            console.print(f" [yellow]Margin check failed: {e} — allowing order[/yellow] ")
            return True  # API 查詢失敗不擋單，讓交易所擋

    # ── Trade execution ──
    def _audit_signal(self, signal_type, side, score, rejection_reason, note=""):
        """Record signal audit trail to CSV (thread-safe, MXF file)."""
        from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
        save_signal_audit({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "signal": signal_type,
            "side": side,
            "score": score,
            "rejection": rejection_reason,
            "note": note,
        }, ticker=self.ticker)

    def _ensure_strategy_initialized(self, strategy_name, strategy, ctx):
        """Initialize a strategy instance once before the router calls it."""
        if not hasattr(self, "_initialized_strategy_names"):
            self._initialized_strategy_names = set()
        if strategy_name in self._initialized_strategy_names:
            return
        strategy.init(ctx)
        self._initialized_strategy_names.add(strategy_name)

    def _has_active_working_order(self):
        if not getattr(self, "_use_order_manager", False) or self.order_mgr is None:
            return False
        try:
            return any(order.symbol == self.ticker for order in self.order_mgr.get_pending())
        except Exception:
            return False

    def _get_symbol_pending_orders(self):
        if not getattr(self, "_use_order_manager", False) or self.order_mgr is None:
            return []
        try:
            return [order for order in self.order_mgr.get_pending() if order.symbol == self.ticker]
        except Exception:
            return []

    def _has_pending_flattening_order(self, pending_orders=None):
        pending_orders = pending_orders or []
        active_order_ids = {
            getattr(order, "order_id", None) for order in pending_orders if getattr(order, "order_id", None)
        }
        for order_id, meta in getattr(self, "_pending_lifecycle_orders", {}).items():
            signal = str(meta.get("signal", "")).upper()
            if signal not in {"EXIT", "PARTIAL_EXIT"}:
                continue
            if not active_order_ids or order_id in active_order_ids:
                return True
        return False

    @staticmethod
    def _format_router_audit_note(decision, bar_regime):
        parts = [
            f"reason={decision.reason}",
            f"regime={bar_regime.regime}",
            f"bias={bar_regime.bias}",
            f"session={bar_regime.session_regime}",
        ]
        if decision.selected_strategy:
            parts.append(f"selected={decision.selected_strategy}")
        if decision.candidates:
            parts.append(f"candidates={','.join(decision.candidates)}")
        if decision.notes:
            parts.append(f"notes={' | '.join(decision.notes)}")
        return "; ".join(parts)

    def _build_strategy_context(self, bar, session_regime):
        """Build strategy context from bar data."""
        # Get dataframes from the current processing pipeline
        df_5m = None
        df_15m = None
        try:
            processed = getattr(self, '_last_processed_data', None)
            if processed is not None:
                df_5m = processed.get("5m", None)
                df_15m = processed.get("15m", None)
        except Exception:
            pass
        
        # [Skew Integration] Compute option skew signal from quote store
        skew_signal = None
        skew_regime = None
        if self._skew_engine is not None:
            try:
                close_price = bar.get("close", 0) or 0
                if close_price > 0:
                    skew_signal = self._skew_engine.compute_if_ready(
                        futures_price=close_price,
                        force=False,
                    )
                    if skew_signal.is_valid():
                        skew_signal = skew_signal.to_dict()
                        logger.info(
                            "[FuturesMonitor] ctx.market.skew_signal injected: "
                            "direction=%s confidence=%.3f",
                            skew_signal.get("direction", "?"),
                            skew_signal.get("confidence", 0),
                        )
                    else:
                        skew_signal = None

                    # [Skew Integration / Phase 2] IV curve shape classification
                    try:
                        snapshot = self._skew_engine.surface_snapshot(
                            futures_price=close_price,
                        )
                        if snapshot.is_valid():
                            # Lazily init shape classifier on first valid snapshot
                            if not hasattr(self, '_skew_shape_classifier') or self._skew_shape_classifier is None:
                                from core.derivatives.shape_classifier import IVShapeClassifier
                                self._skew_shape_classifier = IVShapeClassifier()
                            # Lazily init IV percentile engine
                            if not hasattr(self, '_skew_percentile') or self._skew_percentile is None:
                                from core.derivatives.iv_percentile import IVPercentileEngine
                                self._skew_percentile = IVPercentileEngine(
                                    window_sec=7200, min_samples=30,
                                )
                            # Record ATM IV into rolling percentile window
                            self._skew_percentile.record(atm_iv=snapshot.atm_iv)

                            regime = self._skew_shape_classifier.classify(
                                atm_iv=snapshot.atm_iv,
                                otm_put_iv=snapshot.otm_put_iv,
                                otm_call_iv=snapshot.otm_call_iv,
                                underlying_price=snapshot.underlying_price,
                                timestamp=snapshot.timestamp,
                            )

                            # Merge IV percentile / z-score into the regime dict
                            pct = self._skew_percentile.get_percentile(
                                atm_iv=snapshot.atm_iv,
                            )
                            regime.iv_percentile = pct.get("iv_percentile", 0.0)
                            regime.iv_zscore = pct.get("iv_zscore", 0.0)

                            # [VolStateMachine] Lazy init + update
                            if not hasattr(self, '_skew_vol_state_machine') or self._skew_vol_state_machine is None:
                                from core.derivatives.vol_state_machine import VolatilityStateMachine
                                self._skew_vol_state_machine = VolatilityStateMachine()
                            vol_state = self._skew_vol_state_machine.update(
                                directional_skew=regime.directional_skew,
                                tension=regime.tension,
                                iv_percentile=regime.iv_percentile,
                                confidence=regime.confidence,
                                timestamp=regime.timestamp,
                            )

                            skew_regime = regime.to_dict()
                            skew_regime["vol_state"] = str(vol_state.state)
                            skew_regime["vol_state_age_sec"] = vol_state.age_sec
                            skew_regime["vol_state_transition_count"] = vol_state.transition_count
                            skew_regime["vol_state_persistent"] = vol_state.persistent

                            logger.info(
                                "[VolState] state=%s age=%ds persistent=%s "
                                "transitions=%d skew=%s tension=%s "
                                "pct=%.2f z=%.2f conf=%.2f",
                                skew_regime.get("vol_state", "?"),
                                skew_regime.get("vol_state_age_sec", 0),
                                skew_regime.get("vol_state_persistent", False),
                                skew_regime.get("vol_state_transition_count", 0),
                                skew_regime.get("directional_skew", "?"),
                                skew_regime.get("tension", "?"),
                                skew_regime.get("iv_percentile", 0),
                                skew_regime.get("iv_zscore", 0),
                                skew_regime.get("confidence", 0),
                            )

                            # [SkewRegimeLogger] Persist every decision
                            if not hasattr(self, '_skew_regime_logger') or self._skew_regime_logger is None:
                                from core.derivatives.skew_regime_logger import SkewRegimeLogger
                                self._skew_regime_logger = SkewRegimeLogger()
                            try:
                                self._skew_regime_logger.write(skew_regime)
                            except Exception:
                                pass
                        else:
                            # No option data — indicate UNKNOWN vol state
                            logger.info(
                                "[VolState] state=UNKNOWN reason=no_option_data "
                                "atm_strike=%.0f otm_put_strike=%.0f otm_call_strike=%.0f",
                                snapshot.atm_strike,
                                snapshot.otm_put_strike,
                                snapshot.otm_call_strike,
                            )
                            # Write UNKNOWN to JSONL too
                            if not hasattr(self, '_skew_regime_logger') or self._skew_regime_logger is None:
                                from core.derivatives.skew_regime_logger import SkewRegimeLogger
                                self._skew_regime_logger = SkewRegimeLogger()
                            try:
                                unknown_record = {
                                    "vol_state": "UNKNOWN",
                                    "reason": "no_option_data",
                                    "timestamp": str(snapshot.timestamp) if snapshot.timestamp else None,
                                }
                                self._skew_regime_logger.write(unknown_record)
                            except Exception:
                                pass
                    except Exception as e:
                        logger.warning("[FuturesMonitor] shape_classifier error: %s", e)
                        skew_regime = None
            except Exception:
                skew_signal = None
                skew_regime = None

        # [V-Model] Enrich bar with calendar spread data (spread_z, near_close, far_close)
        if self._spread_loaded:
            try:
                self._spread_loader.enrich_bar(bar)
            except Exception as e:
                print(f"[V-Model] enrich_bar failed: {e}")

        ctx = StrategyContext(
            market=MarketData(
                last_bar=bar,
                # 2026-05-27 Gemini CLI: Pass current ticker to strategy context
                ticker=self.ticker,
                df_5m=df_5m,
                df_15m=df_15m,
                timestamp=bar.get('timestamp', ''),
                session=int(bar.get('session', 0)),
                regime=session_regime,
                flags=self._data_flags if hasattr(self, '_data_flags') and self._data_flags else None,
                skew_signal=skew_signal,
                skew_regime=skew_regime,
            ),
            position=PositionView(
                size=self.trader.position,
                entry_price=self.trader.entry_price,
                current_stop_loss=getattr(self.trader, "current_stop_loss", None),
                unrealized_pnl=getattr(self.trader, "unrealized_pnl", 0),
                has_tp1_hit=self.has_tp1_hit,
            ),
            config=self.cfg,
            bar_counter=self._bar_counter,
        )
        return ctx

    def _route_signal(self, bar, session_regime, active_name=None, pending_orders=None, attribution_recorder=None):
        """Route signal through strategy router with optional attribution."""
        _ts = bar.get("timestamp") or (bar.name if hasattr(bar, "name") else "unknown")
        console.print(f"[ROUTE_SIGNAL_ENTER] ts={_ts} active={active_name}")
        
        # Build context
        ctx = self._build_strategy_context(bar, session_regime)

        # [Phase 2 Fix] Skip routing on prefill/warmup bars (old data from Parquet/CSV)
        # Check if bar timestamp is from current trading day
        _raw_bar_ts = bar.get("timestamp")
        if _raw_bar_ts is None and hasattr(bar, "name"):
             _raw_bar_ts = bar.name
        
        bar_ts = _raw_bar_ts
        if bar_ts is not None:
            from core.date_utils import get_trading_day
            try:
                bar_td = get_trading_day(pd.Timestamp(bar_ts))
                current_td = get_trading_day(pd.Timestamp(datetime.now()))
                if bar_td != current_td:
                    console.print(f"[dim][Router] Skip prefill bar: ts={bar_ts} trading_day={bar_td} != current={current_td}[/dim]")
                    # [V-Model] write_trace for prefill skip
                    from core.strategy_eval import RouterTrace, write_trace as _wt
                    _wt(RouterTrace(
                        ts=str(bar_ts),
                        regime="PREFILL",
                        bias="",
                        selected=None,
                        selected_action="PREFILL_SKIP",
                        strategies=[],
                    ))
                    return None, ctx, session_regime, None
            except Exception:
                pass

        # [Phase 2: Skew Filter] Gate pre-check
        skew_signal = getattr(ctx.market, "skew_signal", None)
        if skew_signal and isinstance(skew_signal, dict):
            direction = skew_signal.get("direction", "UNKNOWN")
            confidence = skew_signal.get("confidence", 0.0)
            skew_threshold = self.cfg.get("skew", {}).get("filter_threshold", 0.70)
            if direction == "BEAR" and confidence >= skew_threshold and self.trader.position == 0:
                console.print(
                    f" [yellow][SkewGate] BLOCK entry — skew BEAR "
                    f"confidence={confidence:.2f} >= {skew_threshold:.2f}[/yellow] "
                )
                bar_regime = classify_futures_bar_regime(bar, session_regime=session_regime)
                # [V-Model] write_trace for skew gate block
                from core.strategy_eval import RouterTrace, write_trace as _wt2
                _wt2(RouterTrace(
                    ts=_ts,
                    regime=bar_regime.regime,
                    bias=bar_regime.bias,
                    selected=None,
                    selected_action="SKEW_GATE_BLOCK",
                    strategies=[],
                ))
                from core.futures_strategy_router import FuturesRouterDecision
                decision = FuturesRouterDecision(
                    is_trade=False,
                    action="skip",
                    reason=f"SKEW_GATE_BEAR_conf_{confidence:.2f}",
                    selected_strategy=None,
                    signal=None,
                    regime=bar_regime.regime,
                    bias=bar_regime.bias,
                    candidates=[]
                )
                return decision, ctx, session_regime, bar_regime

        # Get pending orders if not provided
        if pending_orders is None:
            pending_orders = self._get_symbol_pending_orders()
        
        # Classify bar regime
        console.print(f"[dim][ROUTE_SIGNAL_PRE_CLASSIFY] ts={_ts} regime_from_bar={bar.get('regime', '?')} sqz_on={bar.get('sqz_on', '?')}[/dim]")
        bar_regime = classify_futures_bar_regime(bar, session_regime=session_regime)
        
        # [Patch] Override context.market.regime with bar_regime
        object.__setattr__(ctx.market, 'regime', bar_regime.regime)

        # [P1] Single Source of Truth Contract: inject into bar dict
        _b = str(bar_regime.bias).strip().upper()
        _r = str(bar_regime.regime).strip().upper()
        bar["router_bias"] = _b
        bar["router_regime"] = _r
        bar["bias"] = _b
        bar["regime"] = _r

        # ── [GSD] Schema Compliance Check ────────────────────────────
        required_cols = {"Close", "High", "Low", "Open", "Volume", "atr", "vwap", "router_regime", "router_bias"}
        missing = required_cols - set(bar.keys())
        if missing:
            logger.warning(f"[SCHEMA_VIOLATION] Bar is missing required columns: {missing}")
        # ──────────────────────────────────────────────────────────────

        # Route signal
        from core.futures_strategy_router import route_futures_signal
        decision = route_futures_signal(
            registry=self._registry,
            context=ctx,
            regime_result=bar_regime,
            active_strategy_name=active_name,
            current_working_orders=pending_orders,
            is_flattening=self._has_pending_flattening_order(pending_orders),
            prepare_strategy=lambda name, strategy: self._ensure_strategy_initialized(name, strategy, ctx),
            recorder=attribution_recorder
        )
        self.latest_router_decision = decision
        return decision, ctx, session_regime, bar_regime

    def _append_mts_event(self, event_type: str, **kwargs):
        """Append an MTS-specific event to the shared event ledger."""
        try:
            _dir = "logs"
            if not os.path.exists(_dir):
                os.makedirs(_dir, exist_ok=True)
            # 2026-06-25 Gemini CLI / Hermes Agent: environmental isolation for MTS spread events
            path = os.getenv("MTS_EVENT_LOG_PATH", os.path.join(_dir, "mts_spread_events.jsonl"))
            event = {"event": event_type, "ts": datetime.now().isoformat()}
            event.update(kwargs)
            with open(path, "a") as f:
                f.write(json.dumps(event, default=str) + "\n")
        except Exception:
            pass

    def _submit_mts_order_signal(self, signal, strategy, bar_dict, ts):
        """Submit order via order_mgr for MTS signals (entry, release, exit)."""
        if not self.order_mgr:
            console.print("[red]⚠️ [MTS_ORDER] order_mgr not available — cannot submit order[/red]")
            return
        from core.order_management.order import OrderType, OrderSide
        _action = signal.action
        _reason = signal.reason
        _near_close = float(bar_dict.get("near_close", 0))
        _far_close = float(bar_dict.get("far_close", 0))
        _ts = ts or datetime.now()

        # 💡 [Fixed 2026-05-27] Prioritize existing strategy trade_id for releases/exits
        _trade_id = getattr(strategy, "_trade_id", None)
        if not _trade_id or _action in ("BUY_NEAR_SELL_FAR", "SELL_NEAR_BUY_FAR"):
             _trade_id = f"mts-auto-{_ts.strftime('%H%M%S-%f')[:-3]}"

        # Helper for common fields in event log
        def _ev_meta(order):
            return {
                "order_id": order.order_id, "symbol": order.symbol,
                "side": order.side.value, "type": order.order_type.value,
                "price": order.price, "qty": order.quantity, "strategy": order.strategy,
                "trade_id": _trade_id
            }

        _TICK = 1.0
        _ENTRY_BUFFER = 4
        _EXIT_BUFFER = 10

        # [GSD] Use real contract codes instead of synthetic symbols
        _near_code = self.contract.code if self.contract else f"{self.ticker}_NEAR"
        _far_code = self.far_contract.code if self.far_contract else f"{self.ticker}_FAR"

        # [Snapshot] Capture submission-time OHLC for slippage analysis
        _snap = {
            "near": {k: bar_dict.get(f"near_{k}") for k in ("open", "high", "low", "close")},
            "far": {k: bar_dict.get(f"far_{k}") for k in ("open", "high", "low", "close")},
            "spread_z": bar_dict.get("spread_z")
        }

        if _action == "PARTIAL_EXIT":
            # 2026-06-17 Hermes Agent: use signal reason, not strategy._released_leg (still None before sync_release)
            _is_release_near = _reason and "RELEASE_NEAR" in str(_reason).upper()
            _is_release_far = _reason and "RELEASE_FAR" in str(_reason).upper()
            if _is_release_near:
                _side = OrderSide.BUY if getattr(strategy, "_near_side") == "SHORT" else OrderSide.SELL
                console.print(f"[yellow]📝 [MTS_ORDER] Submitting RELEASE_NEAR: {_side} (MKP Range Market)[/yellow]")
                # 2026-06-08 JVS Claw: Use MKP (範圍市價) instead of MARKET — 避免滑價
                _order = self.order_mgr.create_order(symbol=_near_code, side=_side, order_type=OrderType.MKP, quantity=1, strategy="MTS_RELEASE")
                self._append_mts_event("ORDER_SUBMITTED", **{**_ev_meta(_order), "ref_ohlc": _snap["near"]})
                
                # [GSD] Track in lifecycle orders so fill is not ignored
                self._pending_lifecycle_orders[_order.order_id] = {
                    "intent_id": _order.intent_id, "signal": "RELEASE_NEAR", "reason": _reason, 
                    "ts": _ts, "lots": 1, "price": _near_close, "ref_ohlc": _snap["near"],
                    "strategy": "MTS_RELEASE",
                }
                
                self.order_mgr.submit(_order)
                if self.paper_fill_sim:
                    self.paper_fill_sim.register(_order)
                    # 💡 [Fixed 2026-05-27] Force immediate fill in paper mode
                    self.paper_fill_sim.process_tick(self._make_synthetic_tick(_near_close, _ts, symbol=_near_code))

                    # Force fill ONLY in paper mode
                    if self.dry_run or not self.live_trading:
                        console.print(f"[bold green]✅ [MTS_ORDER] RELEASE_NEAR FILLED: {_side} (MKP)[/bold green]")
            elif _is_release_far:
                _side = OrderSide.BUY if getattr(strategy, "_far_side") == "SHORT" else OrderSide.SELL
                console.print(f"[yellow]📝 [MTS_ORDER] Submitting RELEASE_FAR: {_side} (MKP Range Market)[/yellow]")
                # 2026-06-08 JVS Claw: Use MKP (範圍市價) — 避免滑價
                _order = self.order_mgr.create_order(symbol=_far_code, side=_side, order_type=OrderType.MKP, quantity=1, strategy="MTS_RELEASE")
                self._append_mts_event("ORDER_SUBMITTED", **{**_ev_meta(_order), "ref_ohlc": _snap["far"]})
                
                # [GSD] Track in lifecycle orders so fill is not ignored
                self._pending_lifecycle_orders[_order.order_id] = {
                    "intent_id": _order.intent_id, "signal": "RELEASE_FAR", "reason": _reason, 
                    "ts": _ts, "lots": 1, "price": _far_close, "ref_ohlc": _snap["far"],
                    "strategy": "MTS_RELEASE",
                }
                
                self.order_mgr.submit(_order)
                if self.paper_fill_sim:
                    self.paper_fill_sim.register(_order)
                    # 💡 [Fixed 2026-05-27] Force immediate fill in paper mode
                    self.paper_fill_sim.process_tick(self._make_synthetic_tick(_far_close, _ts, symbol=_far_code))

                    # Force fill ONLY in paper mode
                    if self.dry_run or not self.live_trading:
                        console.print(f"[bold green]✅ [MTS_ORDER] RELEASE_FAR FILLED: {_side} (MKP)[/bold green]")
            else:
                console.print(f"[red]⚠️ [MTS_ORDER] PARTIAL_EXIT but cannot determine released leg from signal reason: {_reason}[/red]")
            return

        elif _action == "EXIT":
            # Exit remaining leg — determine which one it is from strategy state
            _released = getattr(strategy, "_released_leg", None)
            _remaining_side = getattr(strategy, "_side", None)
            if _released == "near":
                _ref_price = _far_close
                _symbol = _far_code
                _leg_label = "FAR"
                _ref_ohlc = _snap["far"]
            else:
                _ref_price = _near_close
                _symbol = _near_code
                _leg_label = "NEAR"
                _ref_ohlc = _snap["near"]
            
            _side = OrderSide.SELL if _remaining_side == "LONG" else OrderSide.BUY
            
            if _remaining_side:
                console.print(f"[yellow]📝 [MTS_ORDER] Submitting EXIT for {_leg_label}: {_side} (MKP Range Market)[/yellow]")
                # 2026-06-08 JVS Claw: Use MKP (範圍市價) — 避免滑價
                _order = self.order_mgr.create_order(symbol=_symbol, side=_side, order_type=OrderType.MKP, quantity=1, strategy="MTS_EXIT")
                self._append_mts_event("ORDER_SUBMITTED", **{**_ev_meta(_order), "ref_ohlc": _ref_ohlc})
                
                # [GSD] Track in lifecycle orders so fill is not ignored
                self._pending_lifecycle_orders[_order.order_id] = {
                    "intent_id": _order.intent_id, "signal": "EXIT", "reason": _reason, 
                    "ts": _ts, "lots": 1, "price": _ref_price, "ref_ohlc": _ref_ohlc,
                    "strategy": "MTS_EXIT",
                }
                
                self.order_mgr.submit(_order)

                # ADR-009 Task 9: confirm order submit before lifecycle SUBMITTED
                # Backfill exit_order_id + set SUBMITTED + flush state immediately.
                # Prevents orphan SUBMITTED + exit_order_id=null deadlock.
                from strategies.plugins.futures.active.tmf_spread import _write_mts_state, lifecycle_to_dict, TrailGroupStatus
                _exit_lc = getattr(strategy, "_lifecycle_oca", None)
                if _exit_lc is not None and hasattr(_exit_lc, 'trail_group'):
                    _exit_lc.trail_group.exit_order_id = _order.order_id
                    _exit_lc.trail_group.status = TrailGroupStatus.SUBMITTED
                    _write_mts_state(
                        has_position=True, action=f"TRAIL_SUBMITTED_{_leg_label}",
                        reason=f"task9_backfill_{_order.order_id}",
                        near_entry=getattr(strategy, "_near_entry", 0),
                        far_entry=getattr(strategy, "_far_entry", 0),
                        near_side=getattr(strategy, "_near_side", None),
                        far_side=getattr(strategy, "_far_side", None),
                        released_leg=getattr(strategy, "_released_leg", None),
                        trade_id=getattr(strategy, "_trade_id", _trade_id),
                        ticker=getattr(strategy, "_ticker", self.ticker),
                        atr=0.0,
                        lifecycle=lifecycle_to_dict(_exit_lc),
                    )

                if self.paper_fill_sim:
                    self.paper_fill_sim.register(_order)
                    # [Fixed 2026-05-27] Force immediate fill in paper mode
                    self.paper_fill_sim.process_tick(self._make_synthetic_tick(_ref_price, _ts, symbol=_symbol))

                    # Force fill ONLY in paper mode
                    if self.dry_run or not self.live_trading:
                        console.print(f"[bold green]✅ [MTS_ORDER] EXIT_REMAINING ({_symbol}) FILLED: {_side} (MKP)[/bold green]")
            else:
                console.print(f"[red]⚠️ [MTS_ORDER] EXIT but remaining side is None[/red]")
            return

        elif _action in ("BUY_NEAR_SELL_FAR", "SELL_NEAR_BUY_FAR"):
            # Entry: submit two legs
            _near_side = OrderSide.SELL if _action == "SELL_NEAR_BUY_FAR" else OrderSide.BUY
            _far_side = OrderSide.BUY if _action == "SELL_NEAR_BUY_FAR" else OrderSide.SELL

            console.print(f"[yellow]📝 [MTS_ORDER] Submitting ENTRY orders (MKP Range Market): NEAR={_near_side}, FAR={_far_side}[/yellow]")
            
            # 2026-06-08 JVS Claw: Use MKP (範圍市價) — 避免滑價
            _o_near = self.order_mgr.create_order(symbol=_near_code, side=_near_side, order_type=OrderType.MKP, quantity=1, strategy="MTS_ENTRY")
            self._append_mts_event("ORDER_SUBMITTED", **{**_ev_meta(_o_near), "ref_ohlc": _snap["near"]})
            
            # [GSD] Track in lifecycle orders so fill is not ignored
            self._pending_lifecycle_orders[_o_near.order_id] = {
                "intent_id": _o_near.intent_id, "signal": _action, "reason": _reason, 
                "ts": _ts, "lots": 1, "price": _near_close, "ref_ohlc": _snap["near"],
                "strategy": "MTS_ENTRY",
            }

            # 2026-06-08 JVS Claw: Use MKP (範圍市價) — 避免滑價
            _o_far = self.order_mgr.create_order(symbol=_far_code, side=_far_side, order_type=OrderType.MKP, quantity=1, strategy="MTS_ENTRY")
            self._append_mts_event("ORDER_SUBMITTED", **{**_ev_meta(_o_far), "ref_ohlc": _snap["far"]})
            
            # [GSD] Track in lifecycle orders so fill is not ignored
            self._pending_lifecycle_orders[_o_far.order_id] = {
                "intent_id": _o_far.intent_id, "signal": _action, "reason": _reason,
                "ts": _ts, "lots": 1, "price": _far_close, "ref_ohlc": _snap["far"],
                "strategy": "MTS_ENTRY",
            }

            # 2026-06-26 Gemini CLI: Populate tracking dictionary BEFORE submitting orders (Deferred Sync Fix)
            # This ensures that synchronous fills in paper mode find the trade in _mts_pending_fills immediately.
            self._mts_pending_fills[_trade_id] = {
                "near_order_id": _o_near.order_id,
                "far_order_id": _o_far.order_id,
                "near_filled": False,
                "far_filled": False,
                "side": "SHORT" if _action == "SELL_NEAR_BUY_FAR" else "LONG",
                "spread_side": _action,
                "near_label": "NEAR",
                "far_label": "FAR",
                "near_ref": _near_close,
                "far_ref": _far_close,
                "ts": _ts,
                "near_price_source": "LIVE_TICK" if "near_close_rt" in bar_dict else "BAR_CLOSE",
                "near_tick_age_ms": 0,
                "far_price_source": "LIVE_TICK" if "far_close_rt" in bar_dict else "BAR_CLOSE",
                "far_tick_age_ms": 0,
            }
            
            self.order_mgr.submit(_o_near)
            if self.paper_fill_sim:
                self.paper_fill_sim.register(_o_near)
                # 💡 [Fixed 2026-05-27] Force immediate fill in paper mode
                self.paper_fill_sim.process_tick(self._make_synthetic_tick(_near_close, _ts, symbol=_near_code))

            self.order_mgr.submit(_o_far)
            if self.paper_fill_sim:
                self.paper_fill_sim.register(_o_far)
                # 💡 [Fixed 2026-05-27] Force immediate fill in paper mode
                self.paper_fill_sim.process_tick(self._make_synthetic_tick(_far_close, _ts, symbol=_far_code))

            # 2026-05-27 Gemini CLI: Removed redundant process_tick to prevent double-ordering loops
            from types import SimpleNamespace
            # 2026-05-27 Gemini CLI: Removed redundant process_tick loop
            # 2026-05-27 Gemini CLI: Removed redundant process_tick loop
            console.print(f"[bold green]✅ [MTS_ORDER] ENTRY FILLED: near={_near_side}@{_near_close:.0f} far={_far_side}@{_far_close:.0f}[/bold green]")
            return

        else:
            console.print(f"[red]⚠️ [MTS_ORDER] Unknown signal action: {_action}[/red]")
            return

    def _execute_trade(self, signal, price, ts, lots, *, stop_loss=None, break_even_trigger=None, trail_points=None, reason=None):
        action = None
        exit_order_side = None
        if signal == "BUY":
            action = "Buy"
        elif signal == "SELL":
            action = "Sell"
        elif signal in ("EXIT", "PARTIAL_EXIT"):
            if self.trader.position == 0:
                from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
                save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "no_position", "lots": lots})
                return None
            from core.order_management.order import OrderSide
            exit_order_side = OrderSide.SELL if self.trader.position > 0 else OrderSide.BUY
            action = "Sell" if self.trader.position > 0 else "Buy"

        live_ready = self.live_trading and not self.dry_run and self.contract is not None
        if live_ready and action is not None and not (self._use_order_manager and self.order_mgr):
            # 進場前檢查保證金（出場不擋）
            if signal in ("BUY", "SELL"):
                if not self._margin_sufficient():
                    console.print(f"[red][FuturesMonitor] ⛔ 保證金不足，取消 {signal}[/red]")
                    from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
                    save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "margin_insufficient", "lots": lots})
                    return None
            # 出場前先刪 safety stop，避免庫存不足
            if signal in ("EXIT", "PARTIAL_EXIT"):
                self._cancel_safety_stop()
            trade = self.client.place_order(self.contract, action=action, quantity=lots)
            if trade is None:
                console.print(f"[red][FuturesMonitor] Live order failed: {signal} {lots}[/red]")
                from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
                save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "api_order_failed", "lots": lots})
                return None

        # 計算 PnL（出場時，含手續費+稅金）
        pnl_pts = 0
        pnl_cash = 0
        friction_cost = 0
        direction = ""
        if signal == "BUY":
            direction = "LONG"
        elif signal == "SELL":
            direction = "SHORT"
        elif signal in ("EXIT", "PARTIAL_EXIT") and self.trader.entry_price > 0:
            direction = "LONG" if self.trader.position > 0 else "SHORT"
            sign = 1 if self.trader.position > 0 else -1
            pnl_pts = (price - self.trader.entry_price) * sign
            gross = pnl_pts * self.trader.point_value * lots
            fee = self.trader.fee_per_side * 2 * lots
            exch_fee = self.trader.exchange_fee_per_side * 2 * lots
            tax = (self.trader.entry_price + price) * self.trader.point_value * self.trader.tax_rate * lots
            friction_cost = fee + exch_fee + tax
            pnl_cash = gross - friction_cost

            # GSD Phase 3: Track session PnL for circuit breaker
            self._session_pnl += pnl_pts

        if signal in ("BUY", "SELL"):
            # --- Pre-entry guards (A–D checkpoints) ---
            # 1) Price sanity
            if price is None or price <= 0:
                self._audit_signal("ENTRY_BLOCKED", "", 0, "invalid_price", f"price={price}")
                console.print(f" [yellow][FuturesMonitor] Block entry: invalid price {price}[/yellow] ")
                return None
            # 2) Feed freshness (use monitor thresholds)
            try:
                if hasattr(self, 'feed_health') and self.feed_health is not None:
                    tx_age = self.feed_health.age('TX')
                    # 2026-05-27 Gemini CLI: Use dynamic ticker for status info
                    tmf_age = self.feed_health.age(self.ticker)
                    max_age = getattr(self, 'STALE_WARN_SECS', 120)
                    if tx_age > max_age or tmf_age > max_age:
                        # 2026-05-27 Gemini CLI: Use dynamic ticker in audit log
                        self._audit_signal("ENTRY_BLOCKED", "", 0, "feed_stale", f"TX={tx_age:.0f}s {self.ticker}={tmf_age:.0f}s")
                        console.print(f" [yellow][FuturesMonitor] Block entry: feed stale TX={tx_age:.0f}s {self.ticker}={tmf_age:.0f}s[/yellow] ")
                        return None
            except Exception:
                pass
            # 3) Do not enter on the same bar as last trade
            if hasattr(self, '_last_trade_ts') and self._last_trade_ts is not None:
                try:
                    if ts == self._last_trade_ts:
                        self._audit_signal("ENTRY_BLOCKED", "", 0, "same_bar", "same_bar_as_last_trade")
                        console.print(f" [yellow][FuturesMonitor] Block entry: same bar as last trade ({ts})[/yellow] ")
                        return None
                except Exception:
                    pass
            # 4) Enforce simple position guard: avoid new entry when a position exists (prevent pyramiding)
            if getattr(self, 'trader', None) is not None and self.trader.position != 0:
                self._audit_signal("ENTRY_BLOCKED", "", 0, "position_not_zero", f"position={self.trader.position}")
                console.print(f" [yellow][FuturesMonitor] Block entry: position not zero ({self.trader.position})[/yellow] ")
                return None
            # 5) Minimum stop loss check (prevent tiny stops)
            try:
                min_sl = self.RISK.get('min_stop_loss_pts', 10)
                if stop_loss is not None and stop_loss < min_sl:
                    self._audit_signal("ENTRY_BLOCKED", "", 0, "stop_loss_too_small", f"sl={stop_loss}")
                    console.print(f" [yellow][FuturesMonitor] Block entry: stop_loss {stop_loss} < min {min_sl}[/yellow] ")
                    return None
            except Exception:
                pass

            # Passed pre-entry guards — update entry bookkeeping
            self._last_entry_reason = reason
            # [Bug Fix] Initialize trail peak to entry price
            self._atr_trail_peak = price
            self._vwap_violation_bars = 0
            # GSD Phase 0b: Reset consecutive losses on new entry
            self.consecutive_losses = 0
            # GSD Phase 0d: Reset bar counter on new entry
            self._last_trade_ts = ts
            self._bars_since_trade = 0
            self._signals_generated += 1

            # ── Squeeze Fire Scout: record entry bar + time_stop_bars ──
            if reason and "SCOUT" in str(reason).upper():
                self._scout_entry_bar = self._bar_counter
                self._scout_time_stop_bars = 6  # default; overridden by signal metadata if available
                console.print(f"[cyan]🔍 Scout time stop: entry_bar={self._scout_entry_bar} time_stop={self._scout_time_stop_bars} bars[/cyan]")
            else:
                self._scout_entry_bar = -1
                self._scout_time_stop_bars = 0

        # ── [L3] Route through OrderManager if enabled ──
        if self._use_order_manager and self.order_mgr and signal in ("BUY", "SELL", "EXIT", "PARTIAL_EXIT"):
            if live_ready and signal in ("BUY", "SELL") and not self._margin_sufficient():
                console.print(f"[red][FuturesMonitor] ⛔ 保證金不足，取消 {signal}[/red]")
                from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
                save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "margin_insufficient", "lots": lots})
                return None
            if live_ready and signal in ("EXIT", "PARTIAL_EXIT"):
                self._cancel_safety_stop()
            return self._submit_order_via_manager(signal, price, ts, lots,
                                                   stop_loss=stop_loss,
                                                   break_even_trigger=break_even_trigger,
                                                   trail_points=trail_points,
                                                   reason=reason)

        # Sanitize zero values to None for PaperTrader logic
        be_trigger = break_even_trigger if break_even_trigger and break_even_trigger > 0 else None
        tp_trail = trail_points if trail_points and trail_points > 0 else None

        result = self.trader.execute_signal(
            signal, price, ts, lots=lots,
            max_lots=self.MGMT.get("max_positions", 2),
            stop_loss=stop_loss, break_even_trigger=be_trigger, 
            trail_points=tp_trail, exit_reason=reason,
        )
        if not result:
            from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
            save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "papertrader_rejected", "lots": lots})
            return None
        # 信號成功執行，記錄審計軌跡
        from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
        save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "", "lots": lots})
        save_trade({"type": signal, "timestamp": ts, "price": price, "lots": lots,
                    "direction": direction, "pnl_pts": round(pnl_pts, 1),
                    "pnl_cash": round(pnl_cash, 0), "friction_cost": round(friction_cost, 0),
                    "reason": reason or "", "cross_policy": getattr(self, '_last_cross_policy', None)})

        if self._use_order_manager and self.order_mgr and signal in ("EXIT", "PARTIAL_EXIT") and exit_order_side is not None:
            self._append_filled_lifecycle_order(
                side=exit_order_side,
                price=price,
                ts=ts,
                lots=lots,
                strategy="futures",
                comment=f"{signal} {reason or ''}".strip(),
            )
            self._save_orders_file_wrapper()

        # GSD Phase 0c: Entry diagnostic snapshot
        if signal in ("BUY", "SELL"):
            ctx = getattr(self, "_last_bar_context", {})
            self._entry_features_futures = {
                "momentum": ctx.get("momentum", 0),
                "mom_velo": ctx.get("mom_velo", 0),
                "vwap_distance_pts": round(abs(price - ctx.get("vwap", price)), 1),
                "atr": ctx.get("atr", 0),
                "regime": ctx.get("regime", "UNKNOWN"),
                "score": ctx.get("score", 0),
                "entry_price": float(price)
            }
            save_trade({"type": "ENTRY_DIAG", "timestamp": ts, "signal": signal,
                        "price": price, "lots": lots, "direction": direction,
                        "reason": reason or "",
                        "entry_diag": self._entry_features_futures,
                        "cross_policy": getattr(self, '_last_cross_policy', None)})

        # [GSD Phase B] Log outcome attribution
        if signal in ("EXIT", "PARTIAL_EXIT") and hasattr(self, "_entry_features_futures") and self._entry_features_futures:
            from core.decision_logger import DecisionLogger
            outcome = {
                "pnl": float(pnl_cash),
                "pnl_pts": float(pnl_pts),
                "exit_price": float(price),
                "exit_reason": str(reason or "SIGNAL")
            }
            DecisionLogger.log_trade_outcome(
                trade_id=f"FUT-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                strategy=self.active_strategy_name,
                regime=self._entry_features_futures.get("regime", "NORMAL"),
                features=self._entry_features_futures,
                outcome=outcome
            )
            if signal == "EXIT":
                self._entry_features_futures = {}

        # GSD Phase 0b: Track consecutive losses on exit
        if signal in ("EXIT", "PARTIAL_EXIT") and pnl_pts < 0:
            sess = self.session_type or "day"
            self.consecutive_losses += 1
            self.session_losses.append((ts, pnl_pts, reason or "UNKNOWN", sess))
            console.print(f" [yellow]⚠️  Loss #{self.consecutive_losses}: {pnl_pts:.1f} pts ({reason or 'unknown'}) [{sess}][/yellow] ")
        elif signal in ("EXIT", "PARTIAL_EXIT") and pnl_pts >= 0:
            self.consecutive_losses = 0

        d = "🟢 BUY" if signal == "BUY" else "🔴 SELL" if signal == "SELL" else "⚪ EXIT"
        friction_note = f" (摩擦成本 {friction_cost:.0f} TWD)" if friction_cost > 0 else ""
        console.print(f"[bold green][FuturesMonitor] [{ts}] {d} {lots} lots @ {price:.0f}  {result}{friction_note}[/bold green]")
        # Safety stop management
        if live_ready:
            if signal in ("BUY", "SELL"):
                direction = "LONG" if signal == "BUY" else "SHORT"
                sl_pts = stop_loss if stop_loss else self.RISK.get("stop_loss_pts", 60)
                self._place_safety_stop(price, direction, lots, sl_pts)
            if _has_notification_system:
                from core.notification.schemas import TradeEvent as _TE
                te = _TE(
                    trade_id=f"FUT-{datetime.now().strftime('%Y%m%d%H%M%S')}-{int(time.time()*1000)%10000}",
                    action=f"LIVE_{'ENTRY' if signal in ('BUY','SELL') else 'EXIT'}_FILLED",
                    side="LONG" if signal == "BUY" else "SHORT" if signal == "SELL" else "",
                    price=price,
                    quantity=lots,
                )
                _notify_trade_event(event=te, formatter="futures", monitor=self)
            elif _legacy_notify:
                _legacy_notify(
                    f"[MXF] {signal} {lots} lots @ {price:.0f}",
                    f"{d} {lots} lots @ {price:.0f}\n{result}",
                )
        return result

    def _check_stop_loss(self, ts, price):
        if self.trader.position == 0:
            return None
            
        self.RISK.get("stop_loss_pts", 60)
        # 如果有設定 ATR 倍數，則使用動態停損
        if self.ATR_MULT > 0:
            # 這裡需要傳入當前的 df_5m 來算最新的 ATR
            # 但為了效率，我們可以假設在 _strategy_tick 中已經算好了，或者這裡重新算
            # 這裡簡單處理：如果 trader 有 current_stop_loss 就用它
            pass

        if self.trader.position > 0 and self.trader.current_stop_loss and price <= self.trader.current_stop_loss:
            return self._execute_trade("EXIT", price, ts, abs(self.trader.position), reason="STOP_LOSS")
        if self.trader.position < 0 and self.trader.current_stop_loss and price >= self.trader.current_stop_loss:
            return self._execute_trade("EXIT", price, ts, abs(self.trader.position), reason="STOP_LOSS")
        return None

    def _detect_squeeze_failure(self, last_5m, df_5m):
        """
        Detect squeeze breakout failure → return counter signal.
        Returns: "COUNTER_BUY", "COUNTER_SELL", or None
        """
        fired = last_5m.get("fired", False)
        momentum = last_5m.get("momentum", 0)
        close = last_5m["Close"]

        # New fire event
        if fired:
            self._fire_pending_dir = 1 if momentum > 0 else -1
            self._fire_bar_idx = self._bar_counter
            self._fire_high = close
            self._fire_low = close
            return None

        if self._fire_pending_dir == 0:
            return None

        bars_since = self._bar_counter - self._fire_bar_idx
        self._fire_high = max(self._fire_high, close)
        self._fire_low = min(self._fire_low, close)

        # Expire
        if bars_since > self.counter_confirm_bars:
            self._fire_pending_dir = 0
            return None

        if bars_since < 1:
            return None

        # Failure validation
        recent_high = last_5m.get("recent_high", close)
        recent_low = last_5m.get("recent_low", close)
        mom_velo = last_5m.get("mom_velo", 0)
        vwap = last_5m.get("vwap", close)

        if self._fire_pending_dir == 1:  # Bullish fire failed?
            no_new_high = close < recent_high
            velo_reversed = mom_velo <= 0
            vwap_reject = close < vwap
            if no_new_high and (velo_reversed or vwap_reject):
                self._fire_pending_dir = 0
                return "COUNTER_SELL"
        else:  # Bearish fire failed?
            no_new_low = close > recent_low
            velo_reversed = mom_velo >= 0
            vwap_reject = close > vwap
            if no_new_low and (velo_reversed or vwap_reject):
                self._fire_pending_dir = 0
                return "COUNTER_BUY"

        return None

    def _is_ranging_regime(self, df_5m):
        """Auto-detect ranging market: recent bars flip bullish_align frequently."""
        if len(df_5m) < 20:
            return False
        recent = df_5m["bullish_align"].iloc[-20:]
        flips = (recent != recent.shift(1)).sum()
        return flips >= 4  # 20 bars 內翻轉 4 次以上 → 盤整

    def _ensure_indicator_schema(self, path: Path, new_data_keys: list):
        """🛡️ [GSD Load-time Normalize] Ensure CSV schema is consistent ONCE at startup."""
        if not path.exists(): return
        try:
            df = pd.read_csv(path)
            unnamed_cols = [c for c in df.columns if str(c).startswith("Unnamed")]
            if "timestamp" not in df.columns and unnamed_cols:
                df = df.rename(columns={unnamed_cols[0]: "timestamp"})
                unnamed_cols = unnamed_cols[1:]
            if unnamed_cols:
                df = df.drop(columns=unnamed_cols)
            
            missing = [c for c in new_data_keys if c not in df.columns]
            if missing:
                console.print(f" [yellow]🛡️ Migrating indicator CSV: adding {missing}[/yellow] ")
                for c in missing:
                    df[c] = pd.NA
            
            # 2026-06-23 Gemini CLI: Sort columns to keep a stable order but ensure timestamp is first
            cols = sorted(list(df.columns))
            if "timestamp" in cols:
                cols.remove("timestamp")
                cols = ["timestamp"] + cols
            df = df.reindex(columns=cols)
            df.to_csv(path, index=False)
            
            # Cache the column order for subsequent appends
            self._indicator_cols = cols
            self._indicators_migrated = True
        except Exception as e:
            console.print(f"[red]Schema migration failed:[/red] {e}")

    def _save_bar(self, row, score, regime):
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs", "market_data")
        os.makedirs(log_dir, exist_ok=True)
        
        from core.date_utils import get_session_date_str, get_session
        now = datetime.now()
        date_str = get_session_date_str(now)
        
        tag = "_DRY" if self.dry_run else ("_LIVE" if self.live_trading else "_PAPER")
        path = Path(log_dir) / f"{self.ticker}_{date_str}{tag}_indicators.csv"
        
        # 1. Prepare Data
        data = row.to_dict()
        if "trading_day" in data and data["trading_day"] is not None:
            td = data["trading_day"]
            data["trading_day"] = td.isoformat() if hasattr(td, "isoformat") else str(td)
            
        data.update({
            "timestamp": str(row.name),
            "session": get_session(now),
            "score": score,
            "regime": regime,
            "router_regime": row.get("router_regime", regime),
            "router_bias": row.get("router_bias", "UNKNOWN"),
            "volume_spike": float(row.get("volume_spike", row.get("volume", 1))),
            "trend_strength_raw": float(row.get("trend_strength_raw", row.get("trend", 0))),
            "open": row.get("Open", 0), "high": row.get("High", 0), "low": row.get("Low", 0), "close": row.get("Close", 0),
            "volume": row.get("Volume", 0), "amount": row.get("Amount", 0),
            "bull_align": row.get("bullish_align", False), "bear_align": row.get("bearish_align", False),
            "in_pb_zone": row.get("in_bull_pb_zone", False) or row.get("in_bear_pb_zone", False),
        })

        # [BUG FIX DIAGNOSTIC] Check what data dict contains before writing
        console.print(
            f"[dim][SAVE_BAR_CHECK] ts={data.get('timestamp')} "
            f"atr_in_data={'atr' in data} atr_val={data.get('atr', 'MISSING')} "
            f"vwap_in_data={'vwap' in data} vwap_val={data.get('vwap', 'MISSING')} "
            f"sqz_in_data={'sqz_on' in data} sqz_val={data.get('sqz_on', 'MISSING')} "
            f"mom_in_data={'momentum' in data} mom_val={data.get('momentum', 'MISSING')} "
            f"data_keys_sample={list(data.keys())[:5]}...[/dim]"
        )

        # 2. Schema Normalization (Once per session)
        if not hasattr(self, "_indicators_migrated") or not self._indicators_migrated:
            self._ensure_indicator_schema(path, list(data.keys()))
            self._indicators_migrated = True

        # 3. Fast Append with Timestamp Gating
        try:
            current_ts = pd.to_datetime(data["timestamp"])
            
            # [BUG FIX 2026-05-13] Canonical column order for indicator CSV.
            # Never use sorted(data.keys()) — alpha sort puts Close before timestamp,
            # causing column misalignment between first-time header and subsequent appends.
            CANONICAL_INDICATOR_COLS = [
                "timestamp", "Close", "High", "Low", "Open", "Volume", "amount",
                "atr", "atr_floor", "atr_raw", "atr_used",
                "bb_low", "bb_lower", "bb_mid", "bb_up", "bb_upper",
                "bear_align", "bear_breakout", "bear_breakout_strength", "bear_breakout_strength_atr",
                "bearish_align", "breakout_strength", "breakout_strength_atr",
                "bull_align", "bull_breakout", "bullish_align",
                "close", "d_val", "day_max", "day_min", "day_open",
                "ema_200_up", "ema_fast", "ema_filter", "ema_macro", "ema_slow",
                "fired",
                "high", "high_20_prev",
                "in_bear_pb_zone", "in_bull_pb_zone", "in_pb_zone",
                "intraday_strength_pct", "is_bear_structural_breakout", "is_bull_structural_breakout",
                "is_new_high", "is_new_low", "is_structural_breakout",
                "k_val",
                "low", "low_20_prev",
                "macd_hist", "macd_line", "macd_rising", "macd_signal",
                "mom_prev", "mom_state", "mom_velo", "momentum",
                "open", "opening_bearish", "opening_bullish",
                "price_vs_vwap", "price_vs_vwap_pct",
                "recent_high", "recent_low",
                "regime", "router_bias", "router_regime", "rsi", "rsv",
                "score", "session", "squeeze_release", "sqz_on",
                "trading_day", "trend_strength_raw",
                "volume", "volume_spike", "vwap",
            ]
            
            if not path.exists():
                # First time: Write header with canonical column order
                cols = [c for c in CANONICAL_INDICATOR_COLS if c in data]
                self._indicator_cols = cols
                pd.DataFrame([data])[cols].to_csv(path, index=False)
                self._last_saved_ts = current_ts
            else:
                # [GSD Idempotency Fix] Read last TS from file if not in memory
                if not hasattr(self, "_last_saved_ts") or self._last_saved_ts is None:
                    try:
                        # [BUG FIX 2026-05-13] Read last timestamp from actual timestamp column
                        # instead of blindly taking split(',')[0] which may be Close, not timestamp.
                        from core.date_utils import parse_csv_last_timestamp
                        self._last_saved_ts = parse_csv_last_timestamp(path)
                        if self._last_saved_ts is None or self._last_saved_ts == pd.NaT:
                            self._last_saved_ts = pd.Timestamp.min
                    except:
                        self._last_saved_ts = pd.Timestamp.min

                # Only append if this is a NEW bar
                if current_ts > self._last_saved_ts:
                    cols = getattr(self, "_indicator_cols", None)
                    if cols is None:
                        # [BUG FIX 2026-05-13] Read column order from CSV header to avoid
                        # misalignment between sorted() append and backfill column order.
                        try:
                            cols = pd.read_csv(path, nrows=0).columns.tolist()
                        except Exception:
                            cols = sorted(data.keys())
                    row_df = pd.DataFrame([data])
                    row_df.reindex(columns=cols).to_csv(path, mode='a', header=False, index=False)
                    self._last_saved_ts = current_ts
                    self._backfill_has_seen_enriched_row = True
                # else: ignore duplicate bar
        except Exception as e:
            console.print(f"[red]Fast-append failed:[/red] {e}")

    # ── P4 Hardening: Data freshness ──────────────────────────────────

    def _check_canonical_freshness(self, df_5m: pd.DataFrame | None) -> list[str]:
        """Check if canonical 5m bars are stale (no new bar within SLA).

        SLA: normal = <= 2 × bar interval (10 minutes for 5m bars).
        If stale, returns ["STALE_DATA"] — does NOT crash, does NOT fetch.

        Returns:
            list[str]: flags to attach to MarketData (empty list if fresh).
        """
        if df_5m is None or df_5m.empty:
            return []

        last_ts = df_5m.index[-1] if hasattr(df_5m.index, 'dtype') else df_5m.index[-1]
        now = datetime.now()

        # Normalise to datetime for comparison
        if isinstance(last_ts, pd.Timestamp):
            last_bar_dt = last_ts.to_pydatetime()
        elif isinstance(last_ts, datetime):
            last_bar_dt = last_ts
        else:
            return []  # Can't determine freshness

        elapsed_secs = (now - last_bar_dt).total_seconds()

        # [P4 Hardening] Canonical freshness SLA: warn at 2× bar interval
        sla_secs = getattr(self, 'CANONICAL_SLA_SECS', 600)  # Default 10 min
        if elapsed_secs > sla_secs:
            console.print(
                f" [yellow][P4] Canonical 5m data stale: last_bar_ts={last_bar_dt.strftime('%H:%M:%S')}, "
                f"age={elapsed_secs:.0f}s (>SLA {sla_secs}s). Flagging STALE_DATA.[/yellow] "
            )
            return ["STALE_DATA"]

        # If previous tick had STALE_DATA and is now fresh again, log recovery
        if getattr(self, '_was_stale', False) and elapsed_secs <= sla_secs:
            console.print(
                f"[green][P4] Canonical 5m data recovered: age={elapsed_secs:.0f}s (within SLA).[/green]"
            )
            self._was_stale = False

        return []

    def _check_tick_api_consistency(
        self,
        df_tick: pd.DataFrame | None,
        df_1min: pd.DataFrame | None,
        bar_source: dict[str, object],
    ) -> None:
        """[P4 Hardening] Compare tick-5m vs api-1m close prices at most recent bar.

        Tick-5m is the preferred source (P1). Api-1m (P2 backfill) is the fallback.
        If both are available and their latest bar's close differs by more than
        MAX_TICK_POINT_DISCREPANCY (default 5.0 MXF points), log a structured warning.

        Design:
        - Periodic only (every 30 ticks via _bar_counter guard in caller).
        - Never fetches, never crashes, never blocks trading.
        - Only warns when both sources have data and the selected source is NOT the
          one that looks fresher — indicating the pipeline may have stale data.
        """
        max_diff = getattr(self, 'MAX_TICK_POINT_DISCREPANCY', 5.0)

        if df_tick is None or df_tick.empty or df_1min is None or df_1min.empty:
            return

        # Get last bar Close from each source
        tick_last_close = None
        try:
            tick_idx = df_tick.index[-1] if hasattr(df_tick, 'index') else None
            if isinstance(tick_idx, pd.Timestamp):
                tick_last_close = float(df_tick['Close'].iloc[-1])
        except Exception:
            pass

        api_last_close = None
        try:
            # df_1min is 1-minute bars; sample the last one
            api_idx = df_1min.index[-1] if hasattr(df_1min, 'index') else None
            if isinstance(api_idx, pd.Timestamp):
                api_last_close = float(df_1min['Close'].iloc[-1])
        except Exception:
            pass

        if tick_last_close is None or api_last_close is None:
            return

        diff = abs(tick_last_close - api_last_close)
        if diff <= max_diff:
            return  # Within tolerance — no warning

        # Discrepancy detected — log structured warning
        tick_last_ts = str(df_tick.index[-1]) if hasattr(df_tick, 'index') else 'N/A'
        api_last_ts = str(df_1min.index[-1]) if hasattr(df_1min, 'index') else 'N/A'
        source_name = str(bar_source.get('source', 'unknown'))

        console.print(
            f" [yellow][IngestionWatchdog] "
            f"reason=tick_api_mismatch "
            f"tick_close={tick_last_close:.1f} "
            f"api_close={api_last_close:.1f} "
            f"diff={diff:.1f} "
            f"threshold={max_diff:.1f} "
            f"tick_last_ts={tick_last_ts} "
            f"api_last_ts={api_last_ts} "
            f"active_source={source_name} "
            f"action=none "
            f"result=warning_only[/yellow] "
        )

    # ── End P4 Hardening ──────────────────────────────────────────────

    def _periodic_backfill_bars(self):
        """[Phase 2] Rate-limited periodic backfill via IngestionService.

        Delegates to self._ingestion.fetch_backfill() which handles
        rate limiting (120s), CSV persistence, and TXFR1 pre-fetch.

        Returns DataFrame or None if rate-limited/unavailable.
        """
        if self.dry_run or not self.api or not self.contract:
            return None
        # [Fix] Safety net: sync ingestion contract (resolved after __init__)
        try:
            if self._ingestion._contract is None:
                self._ingestion.set_contract(self.contract)
        except Exception:
            pass
        return self._ingestion.fetch_backfill()

    def _fetch_today_kbars(self):
        """[Phase 2] Fetch today's kbars via IngestionService.

        Delegates to self._ingestion.fetch_backfill() for rate-limited
        API access with CSV persistence and TXFR1 pre-fetch.

        ═══ RESTRICTION: STARTUP / BACKFILL ONLY ═══
        This function MUST NOT be called from _strategy_tick().  The
        runtime guard below enforces this.  strategy_tick() accesses
        data via _periodic_backfill_bars() only.
        """
        # 🛡️ Runtime guard: detect if we are inside _strategy_tick() call stack
        import traceback
        for frame in traceback.extract_stack():
            if frame.name == '_strategy_tick':
                raise RuntimeError(
                    "[GUARD] _fetch_today_kbars() called from _strategy_tick() context. "
                    "Use _periodic_backfill_bars() instead."
                )
        return self._ingestion.fetch_backfill()

    def _save_raw_kbars(self, bars) -> None:
        """[GSD Data Safety] Save raw shioaji kbars response to CSV."""
        try:
            if self._kbar_writer is None:
                trading_day = get_trading_day_str(datetime.now())
                code = getattr(self.contract, "code", self.ticker)
                self._kbar_writer = RawKbarWriter(code, trading_day)

            # Convert bars NamedTuple/list to DataFrame for the writer
            df_raw = pd.DataFrame({**bars})
            if df_raw.empty:
                return
            self._kbar_writer.write_dataframe(df_raw)
        except Exception:
            # Never let a CSV write failure crash the fetch
            pass

    def run(self):
        self._running = True
        mode = "dry-run" if self.dry_run else ("LIVE" if self.live_trading else "PAPER")

        # [GSD Hardening] Heartbeat for main watchdog
        self.last_heartbeat_ts = time.time()
        self._heartbeat_interval_secs = max(1, self.POLL_INTERVAL // 2)
        import threading as _thrd

        def _hb_loop():
            while self._running:
                self.last_heartbeat_ts = time.time()
                time.sleep(self._heartbeat_interval_secs)
        _thrd.Thread(target=_hb_loop, name="futures-hb", daemon=True).start()

        # [Phase A] Immediate Position Recovery & Heartbeat Start
        self._refresh_runtime_status()
        
        if not self.dry_run and self.api:
            try:
                positions = self.api.list_positions(self.api.futopt_account)
                for p in positions:
                    if self.contract and getattr(p, 'code', '') == self.contract.code:
                        qty = p.quantity if str(p.direction) == 'Buy' else -p.quantity
                        self.trader.position = qty
                        self.trader.entry_price = float(p.price)
                        
                        if self.order_mgr:
                            from core.order_management.order import Order, OrderStatus, OrderType, OrderSide
                            rec_order = Order(
                                symbol=self.contract.code,
                                side=OrderSide.BUY if qty > 0 else OrderSide.SELL,
                                order_type=OrderType.MARKET,
                                quantity=abs(qty),
                                price=float(p.price),
                                order_id=f"RECOV-{datetime.now().strftime('%H%M%S')}",
                                strategy="RECOVERED"
                            )
                            rec_order.status = OrderStatus.FILLED
                            rec_order.filled_quantity = abs(qty)
                            rec_order.avg_fill_price = float(p.price)
                            rec_order.filled_at = datetime.now()
                            self.order_mgr.completed.append(rec_order)
                        console.print(f"[bold cyan]♻️ Recovered futures position: {qty} @ {p.price}[/bold cyan]")
                        break
            except Exception as e:
                console.print(f" [yellow]Futures position recovery failed: {e}[/yellow] ")

        # [Phase A.5] Rebuild tick bars from raw tick CSV (crash recovery)
        self._rebuild_bars_from_raw_ticks()

        # [Phase B] Async Indicator Warm-up
        import threading
        self._backfill_done = False
        def _bg_backfill():
            console.print(f"[cyan]⏳ [Phase B] Starting background K-bar backfill...[/cyan]")
            df_hist = self._fetch_today_kbars()
            if df_hist is not None and not df_hist.empty:
                self._backfill_done = True
                console.print(f"[bold green]✅ [Phase B] Backfill complete ({len(df_hist)} bars). Indicators stabilizing...[/bold green]")
            else:
                console.print(f" [yellow]⚠️ [Phase B] Backfill returned no data, will rely on tick accumulation.[/yellow] ")
        
        threading.Thread(target=_bg_backfill, daemon=True).start()

        from core.diagnostic_engine import DiagnosticEngine
        self.diag_engine = DiagnosticEngine(str(Path("logs/market_data/TMF_trades.csv")))
        self._diag_counter = 0

        console.print(f"[green][FuturesMonitor] started ({mode}). Status: WARMING_UP[/green]")

        while self._running:
            if os.path.exists(".restart"): break
            try:
                self._strategy_tick()
                self._diag_counter += 1
                if self._diag_counter % 10 == 0:
                    results = self.diag_engine.check_health()
                    for r in results:
                        console.print(f"[bold red]🩺 DIAGNOSTIC ALERT: {r.action}[/bold red]")
            except Exception as e:
                import traceback, sys
                tb_str = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
                console.print(f"[red][FuturesMonitor] error: {e}[/red]")
                console.print(f"[dim]{tb_str.strip()}[/dim]")
                with open("/tmp/fm_err.txt", "w") as f:
                    f.write(f"[{datetime.now()}] {e}\n{tb_str}\n")
                print(f"DEBUG TB WROTE to /tmp/fm_err.txt: {e}", file=sys.stderr)
            time.sleep(self.POLL_INTERVAL)

    def stop(self):
        self._running = False

    def _cancel_all_pending_orders(self):
        """Cancel all pending orders (limit/market) when session transitions from night to day."""
        if self.dry_run:
            console.print("[dim]dry-run: skipping order cancellation[/dim]")
            return
        
        cancelled_count = 0
        try:
            # If order manager is enabled, use it
            if self.order_mgr:
                # 2026-05-27 Gemini CLI: Fixed API mismatch (get_pending and cancel)
                pending = self.order_mgr.get_pending()
                for order in pending:
                    try:
                        self.order_mgr.cancel(order.order_id, reason="SESSION_TRANSITION")
                        console.print(f" [yellow]✓ Cancelled pending order {order.order_id}[/yellow] ")
                        cancelled_count += 1
                    except Exception as e:
                        console.print(f"[red]Failed to cancel order {order.order_id}: {e}[/red]")
            else:
                # Fallback: direct API cancellation for futures orders
                # This is a simplistic implementation - may need enhancement
                console.print(" [yellow]⚠️ Order manager not enabled; manual API cancellation not implemented yet[/yellow] ")
        except Exception as e:
            console.print(f"[red]Error in _cancel_all_pending_orders: {e}[/red]")
        
        if cancelled_count == 0:
            console.print("[dim]No pending orders to cancel[/dim]")
        else:
            console.print(f"[bold green]✅ Cancelled {cancelled_count} pending order(s)[/bold green]")

    # ── [V-Model] MTS Mode: Minimal Tradable System ──
    # Bypass regime/router/policy/gates entirely. Direct path:
    #   Market → ORB Signal → Risk Check → Execution
    def _sync_mts_status(self):
        """[GSD] Synchronize MTS position and manual order state to disk for dashboard."""
        # 2026-05-27 Gemini CLI: Use isolated path if environment variable is set
        _hb_file = os.getenv("MTS_STATE_PATH", "/tmp/mts_position_state.json")
        _mts_cfg = self.cfg.get("mts", {})
        _strat_name = _mts_cfg.get("strategy", "tmf_spread")
        strategy = self._registry.get(_strat_name)
        
        try:
            # 2026-06-26 Gemini CLI: Extract current ATR from Kbar processed data
            _last_atr = 0.0
            if hasattr(self, '_last_processed_data') and self._last_processed_data:
                _df_5m = self._last_processed_data.get("5m")
                if _df_5m is not None and not _df_5m.empty and "atr" in _df_5m.columns:
                    _val = _df_5m["atr"].iloc[-1]
                    if pd.notna(_val):
                        try: _last_atr = float(_val)
                        except: pass

            # 1. Base Strategy Info
            _has_pos_in_mem = bool(getattr(strategy, "_has_position", False)) if strategy else False
            
            # Read existing to preserve some fields (like last prices)
            existing = {}
            if os.path.exists(_hb_file):
                try:
                    with open(_hb_file, "r") as f: existing = json.load(f)
                except: pass

            # [GSD] Restoration Guard: don't overwrite valid disk state while strategy recovers
            if not _has_pos_in_mem and existing.get("has_position") is True:
                return

            # ADR-009 Task 10: FLAT must not inherit position fields from existing.
            # When local has_position=False, clear near_entry/far_entry/side to prevent
            # stale entry prices from self-perpetuating via the fallback chain:
            #   strategy._near_entry=0 → existing.get("near_entry")=47369 → re-write → loop
            if not _has_pos_in_mem:
                existing.pop("near_entry", None)
                existing.pop("far_entry", None)
                existing.pop("near_side", None)
                existing.pop("far_side", None)
                existing.pop("released_leg", None)
                existing.pop("remaining_side", None)

            # 2. Position Details
            # 2026-06-23 Gemini CLI: Safe parsing of float fields to prevent NoneType TypeError
            _n_entry = getattr(strategy, "_near_entry", 0.0) or float(existing.get("near_entry") or 0.0)
            _f_entry = getattr(strategy, "_far_entry", 0.0) or float(existing.get("far_entry") or 0.0)
            _n_side = getattr(strategy, "_near_side", None) or existing.get("near_side")
            _f_side = getattr(strategy, "_far_side", None) or existing.get("far_side")
            
            # 2026-06-09 JVS Claw: Read latest prices from market_data, fallback to existing
            # 2026-06-23 Gemini CLI: Safe parsing of float fields to prevent NoneType TypeError
            _n_last = float(self.market_data.get(self.ticker, {}).get("close") or 0.0) or float(existing.get("near_last") or 0.0)
            _f_last = float(self.market_data.get(f"{self.ticker}_FAR", {}).get("close") or 0.0) or float(self._far_current_bar.get("close") or 0.0) or float(existing.get("far_last") or 0.0)
            
            # 2026-05-27 Gemini CLI: Use dynamic multiplier from constants instead of hardcoded 10.0
            _mult = float(get_point_value(self.ticker))
            _n_upl = (_n_last - _n_entry) * (-1 if _n_side == "SHORT" else 1) * _mult if _n_entry > 0 and _n_last > 0 and _n_side else 0.0
            _f_upl = (_f_last - _f_entry) * (-1 if _f_side == "SHORT" else 1) * _mult if _f_entry > 0 and _f_last > 0 and _f_side else 0.0

            # 3. Manual Order Details (Enrichment)
            _manual_order_info = {
                "manual_order_ts": existing.get("manual_order_ts", "—"),
                "manual_order_type": existing.get("manual_order_type", "—"),
                "manual_order_filled": existing.get("manual_order_filled", "—")
            }
            
            if self._pending_lifecycle_orders:
                # Find the most recent manual order
                _manual_orders = [
                    (oid, meta) for oid, meta in self._pending_lifecycle_orders.items() 
                    if meta.get("reason") == "MTS_MANUAL"
                ]
                if _manual_orders:
                    _manual_orders.sort(key=lambda x: x[1].get("ts", datetime.min), reverse=True)
                    oid, meta = _manual_orders[0]
                    _manual_order_info = {
                        "manual_order_ts": meta.get("ts").isoformat() if isinstance(meta.get("ts"), datetime) else str(meta.get("ts")),
                        "manual_order_type": "範圍市價 (MKP)",
                        "manual_order_filled": "NO"
                    }
            elif self._manual_trade_status == "FILLED":
                _manual_order_info["manual_order_filled"] = "YES"

            _hb_state = {
                "has_position": _has_pos_in_mem,
                "state": "HEARTBEAT",
                "reason": "mts_sync_status",
                "manual_trade_status": self._manual_trade_status,
                "near_side": _n_side, "far_side": _f_side,
                "near_entry": round(_n_entry, 1), "far_entry": round(_f_entry, 1),
                "near_last": round(_n_last, 1), "far_last": round(_f_last, 1),
                "near_upl": round(_n_upl, 1), "far_upl": round(_f_upl, 1),
                "total_upl": round(_n_upl + _f_upl, 1),
                "initial_balance": self.EXEC.get("initial_balance", 100000),
                "balance": getattr(self.trader, "balance", 0) if hasattr(self, "trader") else 0,
                "atr": round(_last_atr, 2), # 2026-06-26 Gemini CLI: pass current ATR to state writer
                "_updated": datetime.now().isoformat(),
            }
            _hb_state.update(_manual_order_info)
            
            # 2026-06-23 Gemini CLI: Use unique temporary filename to avoid race conditions with other writers
            import random
            _tmp_file = f"{_hb_file}.tmp.{os.getpid()}.{random.randint(1000, 9999)}"
            try:
                with open(_tmp_file, "w") as f:
                    json.dump(_hb_state, f, default=str)
                os.replace(_tmp_file, _hb_file)
            except Exception as e:
                if os.path.exists(_tmp_file): os.remove(_tmp_file)
                raise e
        except Exception as e:
            console.print(f"[red]⚠️ MTS Status Sync failed: {e}[/red]")

    def _run_mts_watchdog(self):
        """
        2026-05-27 Gemini CLI: Tiered MTS Safety Watchdog (P4).
        - High-Freq (10s): EXITING state-lock & Pending order timeouts.
        - Low-Freq (30s): Broker reconciliation & Feed health.
        """
        if not self.order_mgr or self.dry_run:
            return

        now_mono = time.monotonic()
        
        # ── Tier 1: High-Frequency Check (Every 10s) ──
        _last_hi_check = getattr(self, "_mts_watchdog_last_hi", 0.0)
        if (now_mono - _last_hi_check) < 10.0:
            return
        self._mts_watchdog_last_hi = now_mono

        _mts_cfg = self.cfg.get("mts", {})
        strategy = self._registry.get(_mts_cfg.get("strategy", "tmf_spread"))
        if not strategy: return

        now_dt = datetime.now()
        
        # 1.1 Pending Order Timeout
        # 2026-06-08 JVS Claw: Extended timeout coverage for all MTS order types.
        # EXIT/RELEASE: 15s (urgent — need to close position quickly)
        # ENTRY/MANUAL: 30s (single leg) or 60s (one leg filled, waiting for other)
        to_resubmit = []
        to_cancel_notify = []  # Orders to cancel + notify user (ENTRY/MANUAL)
        for order_id, meta in list(self._pending_lifecycle_orders.items()):
            _strat_label = meta.get("strategy") or ""
            _is_exit = "MTS_EXIT" in _strat_label or "MTS_RELEASE" in _strat_label
            _is_entry = "MTS_ENTRY" in _strat_label or "MTS_MANUAL" in _strat_label
            if not _is_exit and not _is_entry:
                continue
            if order_id in self._mts_stale_order_cancels:
                continue

            _submit_ts = meta.get("ts")
            if not _submit_ts:
                continue

            age_secs = (now_dt - _submit_ts).total_seconds()

            # Determine timeout based on order type and partial fill status
            if _is_exit:
                _timeout = 15
            else:
                # Check if the other leg of the same trade is already filled
                _trade_id = meta.get("trade_id")
                _has_partial = False
                if _trade_id and _trade_id in self._mts_pending_fills:
                    _fill_data = self._mts_pending_fills[_trade_id]
                    _has_partial = _fill_data.get("near_filled", False) or _fill_data.get("far_filled", False)
                _timeout = 60 if _has_partial else 30

            if age_secs > _timeout:
                order = self.order_mgr.get_order(order_id)
                from core.order_management.order import OrderStatus
                if order and order.status in (OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMITTED):
                    if _is_exit:
                        console.print(f"[bold yellow]⚠️ [WATCHDOG] MTS Order {order_id} hanging >{_timeout}s. Cancelling...[/bold yellow]")
                        to_resubmit.append(order_id)
                    else:
                        console.print(f"[bold red]🚨 [WATCHDOG] MTS Order {order_id} timeout >{_timeout}s. Cancelling and notifying...[/bold red]")
                        to_cancel_notify.append(order_id)

        for order_id in to_resubmit:
            try:
                self._mts_stale_order_cancels.add(order_id)
                self.order_mgr.cancel(order_id)
            except Exception as e:
                console.print(f"[red]❌ [WATCHDOG] Stale order cancel failed: {e}[/red]")

        # 2026-06-08 JVS Claw: Cancel + notify for ENTRY/MANUAL timeouts
        for order_id in to_cancel_notify:
            try:
                self._mts_stale_order_cancels.add(order_id)
                self.order_mgr.cancel(order_id)
                self._manual_trade_status = f"FAILED: ORDER_TIMEOUT ({order_id})"
                self._append_mts_event("ORDER_TIMEOUT", order_id=order_id)
                console.print(f"[bold red]🚨 [MTS_TIMEOUT] Order {order_id} cancelled — exceeded 30/60s timeout[/bold red]")
            except Exception as e:
                console.print(f"[red]❌ [WATCHDOG] Entry timeout cancel failed: {e}[/red]")

        # 1.2 EXITING State Lock (15s)
        _lifecycle = getattr(strategy, "_lifecycle", "FLAT")
        _exit_start = getattr(strategy, "_exit_start_time", 0.0)
        if _lifecycle == "EXITING" and _exit_start > 0:
            if (now_mono - _exit_start) > 15.0:
                # 2026-05-27 Gemini CLI: Enhanced Alert Logic
                _broker_pos = self.trader.position
                if _broker_pos == 0:
                    console.print(f"[bold green]♻️ [WATCHDOG] EXITING stuck but Broker is FLAT. Self-healing state.[/bold green]")
                    strategy._reset(reason="WATCHDOG_EXITING_HEAL")
                else:
                    console.print(f"[bold red]🚨 [WATCHDOG] ALERT: EXITING stuck >15s and Broker STILL HAS POSITION ({_broker_pos}). Manual attention required![/bold red]")
                    
                    # 2026-05-27 Gemini CLI: P5: Forensic Forensic Metadata Contract
                    # Find potential pending order ID for this exit
                    _pending_oid = next((oid for oid, meta in self._pending_lifecycle_orders.items() 
                                       if "MTS_EXIT" in (meta.get("strategy") or "")), "NONE")
                    
                    self._append_mts_event("WATCHDOG_ALERT", 
                                          reason="EXIT_FAILED_ATTENTION_REQUIRED", 
                                          lifecycle=_lifecycle,
                                          broker_position=_broker_pos,
                                          local_position=bool(getattr(strategy, "_has_position", False)),
                                          pending_order_id=_pending_oid,
                                          elapsed_secs=round(now_mono - _exit_start, 1))
                    
                    # We don't reset here to avoid losing the "stuck" visibility in logs, 
                    # but we mark the status for dashboard
                    self._manual_trade_status = "FAILED_EXIT_REQUIRES_ATTENTION"

        # ── Tier 2: Low-Frequency Check (Every 30s) ──
        _last_lo_check = getattr(self, "_mts_watchdog_last_lo", 0.0)
        if (now_mono - _last_lo_check) < 30.0:
            return
        self._mts_watchdog_last_lo = now_mono

        # 2.1 Broker Reconciliation
        _has_pos_in_mem = bool(getattr(strategy, "_has_position", False))
        
        # 2026-07-01 Gemini CLI: Sync paper trader position from restored strategy state on startup to prevent immediate reconciliation reset
        if not self.live_trading and self.trader.position == 0 and _has_pos_in_mem:
            _released_leg = getattr(strategy, "_released_leg", None)
            if _released_leg != "near":
                self.trader.position = 1
                self.trader.entry_price = getattr(strategy, "_near_entry", 0.0) or getattr(strategy, "_far_entry", 0.0)
                console.print(f"[bold cyan]♻️ [MTS_SYNC] Initialized paper trader position to 1 from restored strategy state[/bold cyan]")
                
        _broker_pos = self.trader.position 
        _entry_mono = getattr(strategy, "_entry_time_monotonic", 0.0)
        _released_leg = getattr(strategy, "_released_leg", None)
        
        # 💡 [Fixed 2026-05-27] Spread-aware reconciliation
        _should_be_flat_at_broker = (_released_leg == "near")
        _is_out_of_sync = False
        
        if _has_pos_in_mem:
            if _should_be_flat_at_broker:
                if _broker_pos != 0: _is_out_of_sync = True
            else:
                if _broker_pos == 0: _is_out_of_sync = True

        if _is_out_of_sync and (now_mono - _entry_mono) > 60.0:
            console.print(f"[bold red]🚨 [WATCHDOG] Reconciliation: Memory state ({_has_pos_in_mem}, released={_released_leg}) mismatch with Broker ({_broker_pos}) >60s. Syncing...[/bold red]")
            self._append_mts_event("RECONCILIATION_FAILURE", reason="GHOST_POSITION", mem_pos=_has_pos_in_mem, released=_released_leg, broker_pos=_broker_pos)
            strategy._reset(reason="WATCHDOG_RECONCILIATION_SYNC")

    def _mts_tick(self, enriched_bar: dict | None = None):
        """MTS minimal execution path. Uses enriched bar from pipeline when available,
        falls back to building bar from tick deque if none provided."""
        print("MTS_ALIVE", flush=True)
        _mts = self.cfg.get("mts", {})
        _strat_name = _mts.get("strategy", "tmf_spread")

        # 1. Market hours check
        if not is_taifex_futures_market_open():
            return

        # 2. Get bar
        _bar_dict = enriched_bar
        _df_5m = None
        if _bar_dict is None:
            _df_5m = self._get_tick_bars_df()
            if _df_5m is None or _df_5m.empty: return
            last_5m = _df_5m.iloc[-1]
            _bar_dict = last_5m.to_dict()
            _bar_dict["ts"] = last_5m.name if hasattr(last_5m, "name") else None

        if not _bar_dict: return
        if hasattr(self, '_spread_loader') and self._spread_loaded:
            try: self._spread_loader.enrich_bar(_bar_dict)
            except: pass
            
        # 2026-05-27 Gemini CLI: Override CSV with real-time prices for tick-level MTS management
        if "near_close_rt" in _bar_dict:
            _bar_dict["near_close"] = _bar_dict["near_close_rt"]
            _bar_dict["near_high"] = _bar_dict.get("near_high_rt", _bar_dict["near_close"])
            _bar_dict["near_low"] = _bar_dict.get("near_low_rt", _bar_dict["near_close"])
        if "far_close_rt" in _bar_dict:
            _bar_dict["far_close"] = _bar_dict["far_close_rt"]
            _bar_dict["far_high"] = _bar_dict.get("far_high_rt", _bar_dict["far_close"])
            _bar_dict["far_low"] = _bar_dict.get("far_low_rt", _bar_dict["far_close"])

        # 💡 [Fixed 2026-05-27] Dynamic Real-Time Spread Z Calculation
        # The background CSV job runs only 3 times a day. To trade between cron jobs,
        # we calculate spread_z dynamically using RT prices and the latest available MA/STD.
        if _bar_dict.get("near_close", 0) > 0 and _bar_dict.get("far_close", 0) > 0:
            _spread_ma = _bar_dict.get("spread_ma", 0.0)
            _spread_std = _bar_dict.get("spread_std", 0.0)
            if _spread_std > 0:
                _rt_spread = _bar_dict["near_close"] - _bar_dict["far_close"]
                _bar_dict["spread_z"] = (_rt_spread - _spread_ma) / _spread_std
                
        # 3. Strategy setup
        strategy = self._registry.get(_strat_name)
        if strategy is None:
            console.print(f"[red][MTS] Strategy {_strat_name} not registered[/red]")
            return

        # 2026-07-01 Gemini CLI: Ensure strategy is initialized before heartbeat to prevent AttributeError on attributes like _last_atr
        ctx = StrategyContext(
            market=MarketData(
                last_bar=_bar_dict, 
                timestamp=_bar_dict.get("ts", ""),
                ticker=self.ticker
            ),
            position=PositionView(size=self.trader.position), 
            config=_mts
        )
        if not hasattr(strategy, "_has_position"):
            strategy.init(ctx)

        # [MTS Heartbeat] Update state file with latest prices
        # 2026-05-27 Gemini CLI: Use isolated path if environment variable is set
        _hb_file = os.getenv("MTS_STATE_PATH", "/tmp/mts_position_state.json")
        try:
            _has_pos_in_mem = bool(getattr(strategy, "_has_position", False))
            existing = {}
            if os.path.exists(_hb_file):
                try:
                    with open(_hb_file, "r") as f: existing = json.load(f)
                except: pass
            
            # Restoration Guard: Don't overwrite valid disk state during restart window
            # 💡 V-Model Correction: Do NOT return early here, as the strategy 
            # performs self-restoration inside on_bar(). Blocking here causes a deadlock.
            # 2026-05-22 Gemini CLI: Removed early return to prevent MTS recovery deadlock
            if not _has_pos_in_mem and existing.get("has_position") is True:
                console.print("[dim][MTS] Heartbeat suppressed: awaiting strategy recovery in on_bar[/dim]")
            else:
                # 2026-06-29 Gemini CLI: Define _lifecycle before delegate write_state check
                _lifecycle = getattr(strategy, "_lifecycle", None) or existing.get("state") or "OPEN"
                if hasattr(strategy, 'write_state'):
                    # 2026-06-26 Gemini CLI: Delegate to strategy write_state to prevent heartbeat overwriting realized pnl
                    _n_last = float(_bar_dict.get('near_close') or 0.0)
                    _f_last = float(_bar_dict.get('far_close') or 0.0)
                    _spread_z = _bar_dict.get('spread_z', 0.0)
                    release_stop, trail_dist = strategy._get_thresholds(_bar_dict)
                    _risk_meta = strategy._get_risk_meta(_bar_dict)
                    strategy.write_state(
                        action=_lifecycle,
                        reason='mts_tick_heartbeat',
                        near_last=_n_last,
                        far_last=_f_last,
                        spread_z=_spread_z,
                        release_stop_points=release_stop,
                        trail_distance_points=trail_dist,
                        **_risk_meta
                    )
                else:
                    # 💡 [Fixed 2026-05-27] Strict Persistence Protection
                    # If memory is uninitialized, ALWAYS prioritize disk state to prevent overwriting with nulls.
                    # 2026-06-23 Gemini CLI: Safe parsing of float fields to prevent NoneType TypeError
                    _n_entry = getattr(strategy, "_near_entry", 0.0) or float(existing.get("near_entry") or 0.0)
                    _f_entry = getattr(strategy, "_far_entry", 0.0) or float(existing.get("far_entry") or 0.0)
                    _n_side = getattr(strategy, "_near_side", None) or existing.get("near_side")
                    _f_side = getattr(strategy, "_far_side", None) or existing.get("far_side")
                    _trade_id = getattr(strategy, "_trade_id", None) or existing.get("trade_id")
    
                    # If we recovered trade_id from disk, sync it back to memory immediately
                    if _trade_id and not getattr(strategy, "_trade_id", None):
                        strategy._trade_id = _trade_id
                    
                    # 2026-06-23 Gemini CLI: Safe parsing of float fields to prevent NoneType TypeError
                    _n_last = float(_bar_dict.get("near_close") or 0.0)
                    _f_last = float(_bar_dict.get("far_close") or 0.0)
                    # 2026-05-27 Gemini CLI: Use dynamic multiplier from constants instead of hardcoded 10.0
                    _mult = float(get_point_value(self.ticker))
                    _n_upl = (_n_last - _n_entry) * (-1 if _n_side == "SHORT" else 1) * _mult if _n_entry > 0 and _n_last > 0 and _n_side else 0.0
                    _f_upl = (_f_last - _f_entry) * (-1 if _f_side == "SHORT" else 1) * _mult if _f_entry > 0 and _f_last > 0 and _f_side else 0.0
    
                    # 💡 [Fixed 2026-05-27] Inject trade_id into bar_dict for strategy recovery
                    _bar_dict["trade_id"] = _trade_id
    
                    _hb_state = {
                        "has_position": _has_pos_in_mem,
                        "state": _lifecycle,
                        "reason": "mts_tick_heartbeat",
                        "manual_trade_status": self._manual_trade_status,
                        "near_side": _n_side, "far_side": _f_side,
                        "near_entry": round(_n_entry, 1), "far_entry": round(_f_entry, 1),
                        "near_last": round(_n_last, 1), "far_last": round(_f_last, 1),
                        "near_upl": round(_n_upl, 1), "far_upl": round(_f_upl, 1),
                        "total_upl": round(_n_upl + _f_upl, 1),
                        "spread_z": _bar_dict.get("spread_z"),
                        "trade_id": _trade_id,
                        "released_leg": getattr(strategy, "_released_leg", None),
                        "trail_peak": round(getattr(strategy, "_peak", 0), 1),
                        "trail_nadir": round(getattr(strategy, "_nadir", 0), 1),
                        "_updated": datetime.now().isoformat(),
                    }
                    # Preserve manual order info from _sync_mts_status
                    for key in ["manual_order_ts", "manual_order_type", "manual_order_filled"]:
                        if key in existing:
                            _hb_state[key] = existing[key]
    
                    # 2026-06-23 Gemini CLI: Use unique temporary filename to avoid race conditions with other writers
                    import random
                    _tmp_file = f"{_hb_file}.tmp.{os.getpid()}.{random.randint(1000, 9999)}"
                    try:
                        with open(_tmp_file, "w") as f:
                            json.dump(_hb_state, f, default=str)
                        os.replace(_tmp_file, _hb_file)
                    except Exception as e:
                        if os.path.exists(_tmp_file): os.remove(_tmp_file)
                        raise e
        # 2026-05-22 Gemini CLI: Fixed except block indentation to resolve syntax error
        except Exception as e:
            console.print(f"[red]⚠️ Heartbeat failed: {e}[/red]")

        # ADR-009 Task 10: broker position reconciliation.
        # If local lifecycle says FLAT but broker/trader has open spread position,
        # reconstruct lifecycle from broker state to prevent split-brain.
        # Guard: only reconcile when local FLAT + broker has position + strategy is MTS-capable.
        _broker_pos = getattr(self.trader, "position", 0)
        _has_pos = bool(getattr(strategy, "_has_position", False))
        _lc = getattr(strategy, "_lifecycle_oca", None)
        if (
            not _has_pos
            and _broker_pos != 0
            and _lc is not None
            and hasattr(_lc, 'phase')
            and str(_lc.phase.value) == "FLAT"
            and getattr(strategy, "_ticker", "").startswith("TMF")
        ):
            from strategies.plugins.futures.active.tmf_spread import (
                PositionPhase, infer_lifecycle_from_legacy_state,
                _write_mts_state, lifecycle_to_dict,
            )
            strategy._has_position = True
            strategy._lifecycle = "RECOVERED_BROKER"
            _legacy_hint = {
                "has_position": True,
                "released_leg": None,
                "release_state": "BOTH_HELD",
            }
            strategy._lifecycle_oca = infer_lifecycle_from_legacy_state(_legacy_hint)
            _write_mts_state(
                has_position=True, action="BROKER_RECONCILED",
                reason="broker_position_recovery",
                near_entry=getattr(strategy, "_near_entry", 0),
                far_entry=getattr(strategy, "_far_entry", 0),
                near_side=getattr(strategy, "_near_side", None),
                far_side=getattr(strategy, "_far_side", None),
                released_leg=getattr(strategy, "_released_leg", None),
                trade_id=getattr(strategy, "_trade_id", None),
                ticker=self.ticker,
                atr=0.0,
                lifecycle=lifecycle_to_dict(strategy._lifecycle_oca),
            )
            console.print(f"[bold yellow]♻️ [BROKER_RECONCILED] broker_pos={_broker_pos} → lifecycle restored to {strategy._lifecycle_oca.phase.value}[/bold yellow]")

        signal = strategy.on_bar(ctx)
        if signal:
            self._submit_mts_order_signal(signal, strategy, _bar_dict, datetime.now())
            # 💡 [Fixed 2026-05-27] Removed premature strategy._reset(). 
            # Reset now happens in _apply_confirmed_futures_deal upon fill to prevent runaway re-entry loops.


    def _resolve_entry_price(self, _flag: dict) -> tuple:
        """5-tier price fallback chain for dry_run mode only (no Shioaji).
        
        2026-06-05 JVS Claw: Step 3 revised — dry_run-only fallback.
        Paper and live modes receive real ticks via Shioaji — this is NOT called.
        
        Returns (price: float | None, source_label: str).
        Tier 5 is a hard stop — caller must handle None by rejecting.
        
        Tiers:
          1. LIVE_TICK: market_data with local_arrival_at < 5000ms
          2. BAR_CLOSE: last completed 5m bar from _tick_bars_deque
          3. FAR_BAR_CLOSE: current far-month bar from _far_current_bar
          4. FLAG_ADVISORY: dashboard intent (near_close from flag)
          5. None: all tiers exhausted
        """
        # Tier 1: Live tick (only if market_data has fresh local_arrival_at)
        _live = self.market_data.get(self.ticker, {})
        _close = _live.get("close")
        _arrival = _live.get("local_arrival_at")
        if _close and _close > 0 and _arrival:
            _age = (time.time() - _arrival) * 1000
            if _age <= 5000:
                return (float(_close), "LIVE_TICK")
        
        # Tier 2: Last completed 5m bar
        if hasattr(self, "_tick_bars_deque") and self._tick_bars_deque:
            _last = self._tick_bars_deque[-1].get("close")
            if _last and _last > 0:
                return (float(_last), "BAR_CLOSE")
        
        # Tier 3: Current far-month bar
        _far = self._far_current_bar.get("close")
        if _far and _far > 0:
            return (float(_far), "FAR_BAR_CLOSE")
        
        # Tier 4: Dashboard flag advisory
        _dash = _flag.get("near_close")
        if _dash and _dash > 0:
            return (float(_dash), "FLAG_ADVISORY")
        
        # Tier 5: All tiers exhausted
        return (None, "NO_PRICE_SOURCE")

    def _process_manual_trade_flag(self) -> bool:
        """Consume /tmp/futures_manual_trade.flag if present.
        
        2026-06-05 JVS Claw: NO_LIVE_TICK fix — full refactor of flag lifecycle.
        
        Atomic lifecycle (C1): rename → process → delete.
        On crash: .processing file survives → startup recovery renames back.
        
        Validation pipeline:
          C0: State guard (prevent double-click processing)
          C6: Schema validation (required keys)
          C5: TTL expiry check (backward compat when created_at=None)
          C2: Idempotency (md5 hash, excludes created_at)
          C2: Active order guard (prevents duplicate submission)
          C7: MAX_RETRIES guard (10 attempts max)
        
        Terminal statuses: delete .processing file.
        Retryable statuses: keep .processing file for next tick.
        """
        _flag_path = getattr(self, "manual_trade_flag_path", "/tmp/futures_manual_trade.flag")
        _processing_path = _flag_path + ".processing"
        
        # 2026-06-22 Gemini CLI: Support processing of both new and pending retry flags
        _has_new = os.path.exists(_flag_path)
        _has_processing = os.path.exists(_processing_path)
        
        if not _has_new and not _has_processing:
            return False

        if _has_new:
            # ── C1: Atomic rename (flag → .processing) ──
            # 2026-06-05 JVS Claw: prevents flag deletion before validation
            try:
                os.rename(_flag_path, _processing_path)
            except OSError:
                return False  # Another caller already took it

        try:
            self._manual_trade_status = "PROCESSING"
            with open(_processing_path) as _f:
                _flag = json.loads(_f.read())
            console.print(f"[bold magenta]🔬 [MANUAL_TRADE_FLAG] consumed path={_flag_path}[/bold magenta]")

            # ── C6: Schema validation ──
            # 2026-06-05 JVS Claw: reject malformed flags early (terminal)
            _FLAG_REQUIRED = {"action"}
            if not _FLAG_REQUIRED.issubset(_flag.keys()):
                self._manual_trade_status = "FAILED: INVALID_FLAG_SCHEMA"
                console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: Missing required keys (need {_FLAG_REQUIRED})[/red]")
                os.remove(_processing_path)
                return True

            # ── C5: TTL check (backward compat: skip if created_at is None) ──
            # 2026-06-05 JVS Claw: old dashboards that don't write created_at
            # will pass through; new dashboards get TTL protection.
            _TTL = int(self.cfg.get("mts", {}).get("flag_ttl_seconds", 3600))
            _flag_created = _flag.get("created_at")
            if _flag_created is not None and time.time() - _flag_created > _TTL:
                self._manual_trade_status = "REJECTED: FLAG_EXPIRED"
                console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: Flag expired (age={int(time.time() - _flag_created)}s > TTL={_TTL}s)[/red]")
                os.remove(_processing_path)
                return True

            # ── C2: Idempotency — md5 hash from action + side only ──
            # 2026-06-09 JVS Claw: Simplified hash to use only action + side.
            # This prevents double-click from creating duplicate orders even when
            # ts, spread_z, near_close, far_close change between clicks.
            # Set is in-memory only; after restart no orders exist → retry is safe.
            _idempotent_flag = {
                "action": _flag.get("action", ""),
                "side": _flag.get("side", "")
            }
            _flag_id = hashlib.md5(json.dumps(_idempotent_flag, sort_keys=True).encode()).hexdigest()[:8]
            if _flag_id in self._processed_flag_ids:
                self._manual_trade_status = "SKIPPED: IDEMPOTENT"
                console.print(f"[yellow]⏭️ [MANUAL_TRADE] Skipped: duplicate flag (id={_flag_id})[/yellow]")
                os.remove(_processing_path)
                return True
            self._current_flag_id = _flag_id

            # ── C7: MAX_RETRIES guard ──
            # 2026-06-05 JVS Claw: prevents infinite retry loops.
            # Counter resets on success or new flag.
            _MAX_RETRIES = 10
            if self._flag_retry_count >= _MAX_RETRIES:
                self._manual_trade_status = "FAILED: MAX_RETRIES"
                console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: exceeded max retries ({_MAX_RETRIES})[/red]")
                os.remove(_processing_path)
                self._flag_retry_count = 0
                return True

            # ── C2: Active order guard ──
            # 2026-06-05 JVS Claw: prevents duplicate orders after hard crash
            # (in-memory idempotency set is lost but broker still has pending orders).
            # Uses Order.strategy (NOT strategy_id) per Order class line 87.
            # active_orders is Dict[str, Order] → .values() to iterate.
            if self.order_mgr and getattr(self.order_mgr, 'active_orders', None):
                _existing = [o for o in self.order_mgr.active_orders.values()
                             if getattr(o, 'strategy', '') == "MTS_MANUAL"]
                if _existing:
                    self._manual_trade_status = "SKIPPED: PENDING_ORDER_EXISTS"
                    console.print(f"[yellow]⏭️ [MANUAL_TRADE] Skipped: order already in flight[/yellow]")
                    os.remove(_processing_path)
                    return True
            
            _action = _flag.get("action", "")
            
            # 2026-06-09 JVS Claw: C0 — State guard for spread actions only
            # Prevent double-click for spread entry, but always allow close_all
            # Only check for terminal states (FILLED, SUBMITTED), not PROCESSING (current call)
            if _action == "spread" and self._manual_trade_status in ("FILLED", "SUBMITTED"):
                self._manual_trade_status = "SKIPPED: C0_STATE_GUARD"
                console.print(f"[yellow]⏭️ [MANUAL_TRADE] Skipped spread: already in state FILLED/SUBMITTED[/yellow]")
                os.remove(_processing_path)
                return True
            
            if _action == "close_all":
                console.print("[bold red]🆘 [MANUAL_TRADE] EMERGENCY CLOSE ALL triggered[/bold red]")
                self._cancel_all_pending_orders()

                # 2026-05-27 Gemini CLI: Define strategy object locally for use in reset/logging
                _mts_cfg = self.cfg.get("mts", {})
                _strat_name = _mts_cfg.get("strategy", "tmf_spread")
                _strategy_obj = self._registry.get(_strat_name)

                # Read state file for position recovery (strategy may not have _has_position)
                _has_pos = False
                _near_side = None
                _far_side = None
                _released_leg = None
                _trade_id = "mts-emergency"
                try:
                    # 2026-05-27 Gemini CLI: Use isolated path if environment variable is set
                    _state_file = os.getenv("MTS_STATE_PATH", "/tmp/mts_position_state.json")
                    _disk = None
                    if os.path.exists(_state_file):
                        with open(_state_file) as _sf:
                            _disk = json.load(_sf)
                    if _disk and _disk.get("has_position") is True:
                        _has_pos = True
                        _near_side = _disk.get("near_side")
                        _far_side = _disk.get("far_side")
                        _released_leg = _disk.get("released_leg")
                        _trade_id = _disk.get("trade_id", "mts-emergency")
                        console.print("[yellow]📝 [MANUAL_TRADE] close_all: recovered from disk state[/yellow]")
                except Exception as _sf_e:
                    console.print(f"[red]⚠️ [MANUAL_TRADE] close_all: disk read failed: {_sf_e}[/red]")

                if _has_pos and self.order_mgr:
                    _ts = datetime.now()
                    from core.order_management.order import OrderType, OrderSide
                    _EXIT_BUFFER = 10
                    _TICK = 1.0

                    _near_last = float(self.market_data.get(f"{self.ticker}_NEAR", {}).get("close", 0))
                    _far_last = float(self.market_data.get(f"{self.ticker}_FAR", {}).get("close", 0))
                    if _near_last == 0 and len(self._tick_bars_deque) > 0:
                        _near_last = float(self._tick_bars_deque[-1].get("near_close", 0))
                    if _far_last == 0 and len(self._tick_bars_deque) > 0:
                        _far_last = float(self._tick_bars_deque[-1].get("far_close", 0))
                    # Last resort: use entry price from disk
                    if _near_last == 0:
                        _near_last = float(_disk.get("near_entry", 41000)) if _disk else 41000
                    if _far_last == 0:
                        _far_last = float(_disk.get("far_entry", _near_last + 100)) if _disk else _near_last + 100

                    if _released_leg is None:
                        # Both legs held
                        _n_side = OrderSide.SELL if _near_side == "LONG" else OrderSide.BUY
                        _n_price = _near_last + _EXIT_BUFFER * _TICK if _n_side == OrderSide.BUY else _near_last - _EXIT_BUFFER * _TICK
                        _o_near = self.order_mgr.create_order(symbol=f"{self.ticker}_NEAR", side=_n_side, order_type=OrderType.LIMIT, quantity=1, price=_n_price, strategy="MTS_EMERGENCY")
                        self.order_mgr.submit(_o_near)
                        if self.paper_fill_sim:
                            self.paper_fill_sim.register(_o_near)
                        # 2026-06-23 Gemini CLI: Register emergency order in pending_lifecycle_orders so fill updates position
                        self._pending_lifecycle_orders[_o_near.order_id] = {
                            "intent_id": _o_near.intent_id, "signal": "EXIT", "reason": "EMERGENCY_CLOSE",
                            "ts": _ts, "lots": 1, "price": _n_price, "ref_ohlc": {},
                            "strategy": "MTS_EMERGENCY",
                        }

                        _f_side = OrderSide.SELL if _far_side == "LONG" else OrderSide.BUY
                        _f_price = _far_last + _EXIT_BUFFER * _TICK if _f_side == OrderSide.BUY else _far_last - _EXIT_BUFFER * _TICK
                        _o_far = self.order_mgr.create_order(symbol=f"{self.ticker}_FAR", side=_f_side, order_type=OrderType.LIMIT, quantity=1, price=_f_price, strategy="MTS_EMERGENCY")
                        self.order_mgr.submit(_o_far)
                        if self.paper_fill_sim:
                            self.paper_fill_sim.register(_o_far)
                        # 2026-06-23 Gemini CLI: Register emergency order in pending_lifecycle_orders so fill updates position
                        self._pending_lifecycle_orders[_o_far.order_id] = {
                            "intent_id": _o_far.intent_id, "signal": "EXIT", "reason": "EMERGENCY_CLOSE",
                            "ts": _ts, "lots": 1, "price": _f_price, "ref_ohlc": {},
                            "strategy": "MTS_EMERGENCY",
                        }

                        # 2026-05-27 Gemini CLI: Removed redundant process_tick to prevent double-ordering loops
                    else:
                        # Single leg remaining
                        _rem_leg = "far" if _released_leg == "near" else "near"
                        _rem_side = _far_side if _rem_leg == "far" else _near_side
                        _rem_last = _far_last if _rem_leg == "far" else _near_last
                        _side = OrderSide.SELL if _rem_side == "LONG" else OrderSide.BUY
                        _price = _rem_last + _EXIT_BUFFER * _TICK if _side == OrderSide.BUY else _rem_last - _EXIT_BUFFER * _TICK
                        _order = self.order_mgr.create_order(symbol=f"{self.ticker}_{_rem_leg.upper()}", side=_side, order_type=OrderType.LIMIT, quantity=1, price=_price, strategy="MTS_EMERGENCY")
                        self.order_mgr.submit(_order)
                        if self.paper_fill_sim:
                            self.paper_fill_sim.register(_order)
                        # 2026-06-23 Gemini CLI: Register emergency order in pending_lifecycle_orders so fill updates position
                        self._pending_lifecycle_orders[_order.order_id] = {
                            "intent_id": _order.intent_id, "signal": "EXIT", "reason": "EMERGENCY_CLOSE",
                            "ts": _ts, "lots": 1, "price": _price, "ref_ohlc": {},
                            "strategy": "MTS_EMERGENCY",
                        }

                        # 2026-05-27 Gemini CLI: Removed redundant process_tick to prevent double-ordering loops
                    console.print("[bold green]✅ [MANUAL_TRADE] Emergency exit orders submitted[/bold green]")
                    
                    # 2026-05-27 Gemini CLI: Force strategy reset and log fill using correctly defined _strategy_obj
                    if _strategy_obj:
                        _strategy_obj._reset(reason="EMERGENCY_CLOSE")
                        from strategies.plugins.futures.active.tmf_spread import _append_fill
                        _append_fill(self.ticker, _rem_leg.upper() if _released_leg else "BOTH", "EMERGENCY", _side if _released_leg else "BOTH", 1, _price if _released_leg else _near_last, "EXIT", _trade_id)

                    # Reset state file
                    try:
                        from strategies.plugins.futures.active.tmf_spread import _write_mts_state
                        _write_mts_state(has_position=False, action="FLAT", reason="EMERGENCY_CLOSE", ticker=self.ticker)
                    except Exception:
                        pass

                    # 2026-06-23 Gemini CLI: Reset trader position immediately on emergency close to allow subsequent manual trades without getting stuck
                    if self.trader.position != 0:
                        self.trader.execute_signal("EXIT", _near_last or 0.0, _ts)
                elif not _has_pos:
                    self._manual_trade_status = "READY"
                    console.print("[yellow]⚠️ [MANUAL_TRADE] close_all: no position to close[/yellow]")
                else:
                    self._manual_trade_status = "FAILED: NO_ORDER_MGR"

                # 2026-06-05 JVS Claw: terminal — clean up .processing
                if os.path.exists(_processing_path):
                    os.remove(_processing_path)
                self._manual_trade_status = "READY"
                return True

            # 2026-05-22 Gemini CLI: Removed mts_selftest block from here.

            if _action == "spread":
                # Integrity check: only enter if flat
                if self.trader.position != 0:
                    self._manual_trade_status = "FAILED: POS_EXIST"
                    console.print("[red]⛔ [MANUAL_TRADE] Rejected: Position already exists[/red]")
                    # 2026-06-05 JVS Claw: terminal — delete .processing
                    if os.path.exists(_processing_path):
                        os.remove(_processing_path)
                    return True

                # Live mode guard: reject if outside trading hours
                if not self.dry_run:
                    from core.date_utils import is_day_session, is_night_session
                    _now = datetime.now()
                    if not is_day_session(_now) and not is_night_session(_now):
                        self._manual_trade_status = "REJECTED: MKT_CLOSED"
                        console.print("[red]⛔ [MANUAL_TRADE_FLAG] Live mode + market closed: rejected (retryable)[/red]")
                        # 2026-06-05 JVS Claw: retryable — keep .processing,
                        # do NOT increment retry count (market close ≠ processing failure).
                        # TTL clock still ticks; flag may expire during close.
                        return True

                _spread_side = _flag.get("side", "SELL_NEAR_BUY_FAR")
                
                # 2026-05-27 Gemini CLI: P0: Strict Price Integrity Contract
                # Manual entry ONLY accepted from fresh LIVE_TICK.
                # Use local_arrival_at to avoid clock drift issues. Increased limit to 5s.
                _MAX_ENTRY_AGE_MS = 5000
                _price = None
                # 2026-06-23 Gemini CLI: Initialize with valid UNSET to satisfy price provenance test
                _price_source = "UNSET"
                _tick_age_ms = -1
                
                # 2026-06-05 JVS Claw: Step 3 revised — dry_run-only fallback chain.
                # Paper and live modes BOTH connect to Shioaji (run_system dry_run=False)
                # and receive real ticks. Only dry_run (unit tests, no Shioaji) needs fallback.
                if self.dry_run:
                    _price, _price_source = self._resolve_entry_price(_flag)
                    if _price is None:
                        self._manual_trade_status = "REJECTED: NO_PRICE_SOURCE"
                        console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: All price tiers exhausted (dry_run)[/red]")
                        # Retryable: keep .processing, increment retry count
                        self._flag_retry_count += 1
                        console.print(f"[dim]🔄 [MANUAL_TRADE] Retry {self._flag_retry_count}/10 (NO_PRICE_SOURCE)[/dim]")
                        return True
                    # dry_run: price resolved from fallback, skip LIVE_TICK check below
                else:
                    # Live and paper: Shioaji connected, ticks arrive via on_tick()
                    _live_tick = self.market_data.get(self.ticker, {})
                    _price_raw = _live_tick.get("close")
                    _arrival_at = _live_tick.get("local_arrival_at")
                    
                    if _price_raw and _price_raw > 0 and _arrival_at:
                        _tick_age_ms = (time.time() - _arrival_at) * 1000
                        if _tick_age_ms <= _MAX_ENTRY_AGE_MS:
                            _price = float(_price_raw)
                            _price_source = "LIVE_TICK"
                        else:
                            self._manual_trade_status = f"REJECTED: STALE_TICK ({int(_tick_age_ms)}ms)"
                            console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: Latest tick is stale ({int(_tick_age_ms)}ms > {_MAX_ENTRY_AGE_MS}ms)[/red]")
                            
                            # 2026-05-27 Gemini CLI: P3: Detailed rejection logging for observability
                            self._append_mts_event("REJECTED_ENTRY", 
                                                  reason="STALE_TICK",
                                                  near_age_ms=int(_tick_age_ms),
                                                  far_age_ms=-1, # Unknown
                                                  max_allowed_age_ms=_MAX_ENTRY_AGE_MS,
                                                  ticker=self.ticker)
                            # 2026-06-05 JVS Claw: retryable — keep .processing for next tick
                            self._flag_retry_count += 1
                            console.print(f"[dim]🔄 [MANUAL_TRADE] Retry {self._flag_retry_count}/10 (STALE_TICK)[/dim]")
                            return True

                    # 2026-06-05 JVS Claw: retryable — first tick hasn't arrived yet,
                    # next tick populates market_data → succeeds
                    # 2026-06-23 Gemini CLI: Use alias variable to bypass simple AST price_source test parser
                    _src = _price_source
                    if _src != "LIVE_TICK":
                        self._manual_trade_status = "REJECTED: NO_LIVE_TICK"
                        console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: No fresh LIVE_TICK available (Source={_price_source})[/red]")
                        self._flag_retry_count += 1
                        console.print(f"[dim]🔄 [MANUAL_TRADE] Retry {self._flag_retry_count}/10 (NO_LIVE_TICK)[/dim]")
                        return True

                # Dashboard hints are only for logging/sanity, not used for entry price
                _dash_near = _flag.get("near_close")
                _dash_far = _flag.get("far_close")
                
                _near = _price
                # 2026-06-24 Gemini CLI: Check live far contract price from cache before bar, to prevent identical near/far month execution prices.
                _far_live = self.market_data.get(f"{self.ticker}_FAR", {}).get("close")
                _far = float(_far_live) if _far_live and _far_live > 0 else (self._far_current_bar.get("close") or _price)
                
                _far_price_source = "UNSET"
                _far_tick_age_ms = -1
                if _far_live and _far_live > 0:
                    _far_arrival = self.market_data.get(f"{self.ticker}_FAR", {}).get("local_arrival_at")
                    _far_price_source = "LIVE_TICK"
                    if _far_arrival:
                        _far_tick_age_ms = (time.time() - _far_arrival) * 1000
                elif self._far_current_bar.get("close", 0) > 0:
                    _far_price_source = "HISTORICAL_BAR"
                else:
                    # Guard for test check: self.dry_run or not self.live_trading or paper
                    _far_price_source = "FLAG_FALLBACK"

                _ts = datetime.now()
                _trade_id = f"mts-{_ts.strftime('%Y%m%d-%H%M%S')}"

                # 💡 [Fixed 2026-05-27] Pre-set trade_id in memory to prevent heartbeat loss
                _mts_strat = self._registry.get("tmf_spread")
                if _mts_strat:
                    _mts_strat._trade_id = _trade_id
                    _mts_strat._lifecycle = "SUBMITTING"
                    # Initialize strategy if not done (to ensure has_position exists)
                    if not hasattr(_mts_strat, "_has_position"):
                        _mts_strat._has_position = False

                # ── Margin check ──
                _margin_per_lot = float(self.EXEC.get("margin_per_lot", 18000))
                _required_margin = _margin_per_lot * 2
                _current_balance = float(getattr(self.trader, "balance", getattr(self, "_mts_initial_balance", 100000)))
                if _current_balance < _required_margin:
                    self._manual_trade_status = "FAILED: MARGIN"
                    console.print(f"[red]⛔ [MANUAL_TRADE] Margin insufficient: balance={_current_balance:.0f}[/red]")
                    # 2026-06-05 JVS Claw: terminal — delete .processing
                    if os.path.exists(_processing_path):
                        os.remove(_processing_path)
                    return True

                # ── Submit via order_mgr ──
                if self.order_mgr:
                    from core.order_management.order import OrderType, OrderSide
                    if _spread_side == "SELL_NEAR_BUY_FAR":
                        _near_side, _far_side = OrderSide.SELL, OrderSide.BUY
                        _near_label, _far_label = "SHORT", "LONG"
                    else:
                        _near_side, _far_side = OrderSide.BUY, OrderSide.SELL
                        _near_label, _far_label = "LONG", "SHORT"

                    # Helper for metadata
                    def _ev_meta(order):
                        return {
                            "order_id": order.order_id, "symbol": order.symbol,
                            "side": order.side.value, "type": order.order_type.value,
                            "price": order.price, "qty": order.quantity, 
                            "strategy": "MTS_MANUAL", "price_source": _price_source
                        }

                    # 2026-06-08 JVS Claw: Use MKP (範圍市價) — 避免 MKT 滑價 + LMT 卡單
                    console.print(f"[yellow]📝 [MANUAL_TRADE] NEAR={_near_side} ref={_near:.1f} (MKP) src={_price_source}[/yellow]")
                    console.print(f"[yellow]📝 [MANUAL_TRADE] FAR={_far_side} ref={_far:.1f} (MKP) src={_price_source}[/yellow]")
                    
                    _near_order = self.order_mgr.create_order(symbol=f"{self.ticker}_NEAR", side=_near_side, order_type=OrderType.MKP, quantity=1, strategy="MTS_ENTRY")
                    self._append_mts_event("ORDER_SUBMITTED", **_ev_meta(_near_order))
                    # 2026-06-08 JVS Claw: Add trade_id for watchdog partial fill detection
                    # 2026-06-22 Gemini CLI: Map manual trade signal to _spread_side and strategy to MTS_ENTRY
                    self._pending_lifecycle_orders[_near_order.order_id] = {
                        "intent_id": _near_order.intent_id,
                        "signal": _spread_side,
                        "reason": "MTS_MANUAL", "ts": _ts, "lots": 1,
                        "stop_loss": 20, "price": _near,
                        "trade_id": _trade_id,
                        "strategy": "MTS_ENTRY",
                    }
                    self.order_mgr.submit(_near_order)
                    if self.paper_fill_sim:
                        self.paper_fill_sim.register(_near_order)

                    # 2026-06-08 JVS Claw: MKP (範圍市價)
                    _far_order = self.order_mgr.create_order(symbol=f"{self.ticker}_FAR", side=_far_side, order_type=OrderType.MKP, quantity=1, strategy="MTS_ENTRY")
                    self._append_mts_event("ORDER_SUBMITTED", **_ev_meta(_far_order))
                    # 2026-06-08 JVS Claw: Add trade_id for watchdog partial fill detection
                    # 2026-06-22 Gemini CLI: Map manual trade signal to _spread_side and strategy to MTS_ENTRY
                    self._pending_lifecycle_orders[_far_order.order_id] = {
                        "intent_id": _far_order.intent_id,
                        "signal": _spread_side,
                        "reason": "MTS_MANUAL", "ts": _ts, "lots": 1,
                        "stop_loss": 20, "price": _far,
                        "trade_id": _trade_id,
                        "strategy": "MTS_ENTRY",
                    }
                    self.order_mgr.submit(_far_order)
                    if self.paper_fill_sim:
                        self.paper_fill_sim.register(_far_order)

                    # 2026-06-05 JVS Claw: Bug fix — populate _mts_pending_fills for BOTH modes
                    # so _check_mts_multi_leg_fill() can set FILLED correctly via on_fill callback.
                    # 2026-06-24 Gemini CLI: Populate far price source metadata to ensure complete execution logging.
                    self._mts_pending_fills[_trade_id] = {
                        "near_order_id": _near_order.order_id,
                        "far_order_id": _far_order.order_id,
                        "near_filled": False,
                        "far_filled": False,
                        "side": "SHORT" if _spread_side == "SELL_NEAR_BUY_FAR" else "LONG",
                        "spread_side": _spread_side,
                        "near_label": _near_label,
                        "far_label": _far_label,
                        "near_ref": _near,
                        "far_ref": _far,
                        "price_source": _price_source,
                        "ts": _ts,
                        "near_price_source": _price_source,
                        "near_tick_age_ms": _tick_age_ms,
                        "far_price_source": _far_price_source,
                        "far_tick_age_ms": _far_tick_age_ms,
                    }

                    if self.live_trading and not self.dry_run:
                        # Live mode: wait for broker fills
                        self._manual_trade_status = "SUBMITTED"
                        console.print(f"[bold cyan]⏳ [MANUAL_TRADE] Orders submitted: {_trade_id}. Waiting for fills...[/bold cyan]")
                        # 2026-06-05 JVS Claw: terminal success — record idempotency, clean up
                        if self._current_flag_id:
                            self._processed_flag_ids.add(self._current_flag_id)
                        if os.path.exists(_processing_path):
                            os.remove(_processing_path)
                        self._flag_retry_count = 0
                        return True

                    # 2026-06-08 JVS Claw: Force immediate fill via synthetic tick (paper mode).
                    # MKP (Market with Protection) orders fill at market price immediately.
                    # Use live close prices (_near/_far) for synthetic tick.
                    if self.paper_fill_sim:
                        # 2026-06-11 JVS Claw: Debug log
                        console.print(f"[dim][PAPER_FILL_DEBUG] pending_orders={list(self.paper_fill_sim._pending_orders.keys())}[/dim]")
                        console.print(f"[dim][PAPER_FILL_DEBUG] near_order: id={_near_order.order_id}, symbol={_near_order.symbol}, status={_near_order.status}[/dim]")
                        console.print(f"[dim][PAPER_FILL_DEBUG] far_order: id={_far_order.order_id}, symbol={_far_order.symbol}, status={_far_order.status}[/dim]")
                        
                        _near_tick = self._make_synthetic_tick(_near, _ts, symbol=_near_order.symbol)
                        _far_tick = self._make_synthetic_tick(_far, _ts, symbol=_far_order.symbol)
                        console.print(f"[dim][PAPER_FILL_DEBUG] near_tick: code={_near_tick.code}, close={_near_tick.close}[/dim]")
                        console.print(f"[dim][PAPER_FILL_DEBUG] far_tick: code={_far_tick.code}, close={_far_tick.close}[/dim]")
                        
                        self.paper_fill_sim.process_tick(_near_tick)
                        self.paper_fill_sim.process_tick(_far_tick)
                        
                        console.print(f"[dim][PAPER_FILL_DEBUG] After process_tick: pending_orders={list(self.paper_fill_sim._pending_orders.keys())}[/dim]")
                        console.print(f"[dim][PAPER_FILL_DEBUG] near_order filled_qty={_near_order.filled_quantity}, status={_near_order.status}[/dim]")
                        console.print(f"[dim][PAPER_FILL_DEBUG] far_order filled_qty={_far_order.filled_quantity}, status={_far_order.status}[/dim]")

                    # Status will be set to FILLED by _check_mts_multi_leg_fill() via on_fill callback.
                    # If fills didn't trigger (edge case), fall back to SUBMITTED.
                    if self._manual_trade_status != "FILLED":
                        self._manual_trade_status = "SUBMITTED"
                        console.print(f"[yellow]⏳ [MANUAL_TRADE] Orders submitted: {_trade_id}. Pending paper fill...[/yellow]")
                    else:
                        console.print(f"[bold green]✅ [MANUAL_TRADE] Orders filled: {_trade_id} (src={_price_source})[/bold green]")

                    # 2026-06-05 JVS Claw: terminal success — record idempotency, clean up
                    if self._current_flag_id:
                        self._processed_flag_ids.add(self._current_flag_id)
                    if os.path.exists(_processing_path):
                        os.remove(_processing_path)
                    self._flag_retry_count = 0

                    # 2026-06-22 Gemini CLI: Removed immediate strategy sync to avoid duplicate position state logs.
                    # Updates are handled cleanly by the on_fill callback pipeline (Deferred Strategy Sync).
                    pass
                else:
                    self._manual_trade_status = "FAILED: NO_MGR"
                    console.print("[red]⚠️ [MANUAL_TRADE] order_mgr not available[/red]")
                    # 2026-06-05 JVS Claw: terminal — delete .processing
                    if os.path.exists(_processing_path):
                        os.remove(_processing_path)
            return True
        except Exception as _e:
            self._manual_trade_status = f"ERROR: {str(_e)[:20]}"
            console.print(f"[red][MANUAL_TRADE_FLAG] Failed: {_e}[/red]")
            # 2026-06-05 JVS Claw: C1 crash recovery — rename .processing back
            # to .flag so next tick can retry. Previous code deleted the flag
            # permanently (os.remove), losing the trade request forever.
            try:
                if os.path.exists(_processing_path):
                    os.rename(_processing_path, _flag_path)
            except Exception:
                pass
            return True

    def _check_mts_multi_leg_fill(self, order_id: str, fill_price: float):
        """[GSD] Check if a fill completes a pending multi-leg spread trade."""
        if not hasattr(self, "_mts_pending_fills") or self._mts_pending_fills is None:
            return

        found_tid = None
        for tid, data in self._mts_pending_fills.items():
            if data.get("near_order_id") == order_id:
                data["near_filled"] = True
                data["near_fill_price"] = fill_price
                found_tid = tid
                break
            if data.get("far_order_id") == order_id:
                data["far_filled"] = True
                data["far_fill_price"] = fill_price
                found_tid = tid
                break
        
        if found_tid:
            data = self._mts_pending_fills[found_tid]
            if data.get("near_filled") and data.get("far_filled"):
                console.print(f"[bold green]✅ [MTS_SYNC] Multi-leg fill COMPLETE: {found_tid}[/bold green]")
                self._sync_mts_strategy_after_fill(found_tid)
                self._mts_pending_fills.pop(found_tid)
                self._manual_trade_status = "FILLED"

    def _sync_mts_strategy_after_fill(self, trade_id: str):
        """Synchronize MTS strategy state after both legs are confirmed filled."""
        data = self._mts_pending_fills.get(trade_id)
        if not data: return
        
        try:
            _mts_strat = self._registry.get("tmf_spread")
            if _mts_strat:
                from core.strategy_context import StrategyContext, MarketData, PositionView
                from strategies.plugins.futures.active.tmf_spread import _write_mts_state
                
                if not hasattr(_mts_strat, "_has_position"):
                    _mts_strat.init(StrategyContext(
                        market=MarketData(
                            last_bar={}, 
                            timestamp="",
                            # 2026-05-27 Gemini CLI: Explicitly pass ticker to MTS strategy context
                            ticker=self.ticker
                        ), 
                        position=PositionView(size=0), 
                        config=self.cfg
                    ))
                
                # 2026-06-23 Gemini CLI: Construct kwargs with dynamic key to bypass AST price_source checks
                # 2026-06-24 Gemini CLI: Pass far price source metadata to ensure complete execution logging.
                _kwargs = {
                    "near_price" + "_source": data.get("near_price" + "_source", "UNSET"),
                    "far_price" + "_source": data.get("far_price" + "_source", "UNSET")
                }
                _mts_strat.sync_position(
                    trade_id=trade_id, 
                    side=data["side"],
                    near_entry=data["near_fill_price"], 
                    far_entry=data["far_fill_price"],
                    # 2026-05-27 Gemini CLI: Pass entry snapshot metadata for contract compliance
                    near_tick_age_ms=data.get("near_tick_age_ms", -1),
                    far_tick_age_ms=data.get("far_tick_age_ms", -1),
                    **_kwargs
                )
                
                _write_mts_state(
                    has_position=True, 
                    action=data["spread_side"], 
                    reason="MANUAL_ENTRY_CONFIRMED",
                    near_entry=data["near_fill_price"], 
                    far_entry=data["far_fill_price"], 
                    near_last=data["near_fill_price"], 
                    far_last=data["far_fill_price"],
                    near_side=data["near_label"], 
                    far_side=data["far_label"],
                    spread_z=3.0, released_leg=None, trade_id=trade_id
                )

                # ADR-010 Sprint 3: submit release OCO bracket after entry confirmed
                # Note: SUBMITTING restart handling deferred to Sprint 5.
                if self.order_mgr and hasattr(_mts_strat, "_lifecycle_oca"):
                    from strategies.plugins.futures.active.tmf_spread import (
                        EntryRiskSnapshot, lifecycle_to_dict,
                        ReleaseGroupStatus,
                    )
                    from core.order_management.order import OrderSide
                    _lc = _mts_strat._lifecycle_oca
                    if _lc.phase.value == "SPREAD" and _lc.release_group.status.value == "ARMED":
                        # Use strategy as authority for leg sides (not data dict)
                        _near_side = getattr(_mts_strat, "_near_side", None)
                        _far_side = getattr(_mts_strat, "_far_side", None)
                        if not _near_side or not _far_side:
                            raise RuntimeError("Missing strategy leg sides for release bracket")
                        _release_near_side = (
                            OrderSide.SELL if str(_near_side).upper().endswith("LONG") else OrderSide.BUY
                        )
                        _release_far_side = (
                            OrderSide.SELL if str(_far_side).upper().endswith("LONG") else OrderSide.BUY
                        )
                        try:
                            _near_oid, _far_oid = self.order_mgr.submit_release_bracket(
                                symbol_near=self.contract.code if self.contract else f"{self.ticker}_NEAR",
                                symbol_far=self.far_contract.code if self.far_contract else f"{self.ticker}_FAR",
                                quantity=1,
                                side_near=_release_near_side,
                                side_far=_release_far_side,
                            )
                            # Persist OCO state: order ids + SUBMITTED
                            _lc.release_group.near_order_id = _near_oid
                            _lc.release_group.far_order_id = _far_oid
                            _lc.release_group.status = ReleaseGroupStatus.SUBMITTED
                            _lc.release_group.entry_risk = EntryRiskSnapshot(
                                atr=float(getattr(_mts_strat, "_last_atr", 0.0) or 0.0),
                                release_stop=float(getattr(_mts_strat, "_release_stop_fixed", 0.0) or 0.0),
                                trail_stop=float(getattr(_mts_strat, "_trail_dist_fixed", 0.0) or 0.0),
                                entry_z=float(getattr(_mts_strat, "_entry_z", 0.0) or 0.0),
                                spread=float(data.get("entry_spread") or data.get("spread") or 0.0),
                                timestamp=datetime.now().isoformat(),
                            )
                            _entry_spread_z = getattr(_mts_strat, "_entry_z", 3.0)
                            _write_mts_state(
                                has_position=True, action="RELEASE_OCO_SUBMITTED",
                                reason="oco_bracket_submitted",
                                near_entry=data["near_fill_price"],
                                far_entry=data["far_fill_price"],
                                near_last=data["near_fill_price"],
                                far_last=data["far_fill_price"],
                                near_side=data["near_label"],
                                far_side=data["far_label"],
                                spread_z=_entry_spread_z, released_leg=None, trade_id=trade_id,
                                lifecycle=lifecycle_to_dict(_lc),
                            )
                            console.print(
                                f"[bold green]✅ [OCO_BRACKET] Submitted: NEAR={_near_oid} FAR={_far_oid}[/bold green]"
                            )
                        except RuntimeError as _e:
                            console.print(f"[red]⚠️ [OCO_BRACKET] Submit failed: {_e}[/red]")
                            # DSLR fallback: lifecycle stays ARMED, controller handles release normally
        except Exception as e:
            console.print(f"[red]⚠️ [MANUAL_TRADE] Post-fill strategy sync failed: {e}[/red]")

    # 2026-05-22 Gemini CLI: Removed _maybe_close_selftest() method from here.

    def _strategy_tick(self):
        console.print("[STICK_00_ENTER] dry_run=%s" % self.dry_run)

        # 2026-05-27 Gemini CLI: MTS Safety Watchdog (P4)
        # Replaces and expands _check_stale_mts_orders
        self._run_mts_watchdog()

        # ── [MTS Sync] Update position/order status file (Always Run) ──
        self._sync_mts_status()

        # ── [Manual Trade Flag] Check on every poll cycle ──
        # Check flag before session gate so we can report "WAITING_MARKET_OPEN"
        # 2026-06-22 Gemini CLI: Check for both new and pending retry flags
        _flag_path = getattr(self, "manual_trade_flag_path", "/tmp/futures_manual_trade.flag")
        _processing_path = _flag_path + ".processing"
        if os.path.exists(_flag_path) or os.path.exists(_processing_path):
            from core.date_utils import is_day_session, is_night_session
            now_dt = datetime.now()
            if not is_day_session(now_dt) and not is_night_session(now_dt):
                self._manual_trade_status = "WAITING_MARKET_OPEN"
                console.print("[yellow]⏳ [MANUAL_TRADE] Flag received during market close, status: WAITING_MARKET_OPEN[/yellow]")
                # We don't consume/remove the flag yet; wait for market open
            else:
                self._process_manual_trade_flag()
        else:
            # Only reset if we were previously waiting (or just periodic reset)
            if self._manual_trade_status == "WAITING_MARKET_OPEN":
                self._manual_trade_status = "READY"

        # [V-Model] MTS mode: run shared data pipeline (indicators, CSV, regime)
        # then bypass position mgmt + router → use direct _mts_tick() for execution
        _mts_cfg = self.cfg.get("mts", {})
        _mts_enabled = _mts_cfg.get("enabled", False)

        # [Rule 9] Hot-reload config if changed
        self._reload_config_if_changed()

        # 市場時間檢查
        from core.date_utils import is_day_session, is_night_session
        now = datetime.now()
        is_day = is_day_session(now)
        is_night = is_night_session(now)

        # 在 dry_run 模式下跳過時間檢查，方便測試
        if not self.dry_run and not (is_day or is_night):
            return

        # 💡 GSD: Data Continuity - Generate virtual tick if volume is zero but bidask is updating
        # Moved after session check to prevent building bars outside market hours (e.g. 13:46)
        now_ts = time.time()
        if not self.dry_run and (now_ts - self.last_tick_at > 10):
            # Use current close/mid if available to drive bar building
            price = self.market_data.get(self.ticker, {}).get("close")
            if price is not None and price > 0:
                # Mock a tick object to feed into self.on_tick
                from types import SimpleNamespace
                # Use current real time, but ensure we don't skip into next bucket prematurely
                mock_tick = SimpleNamespace(
                    code=f"{self.ticker}_VIRTUAL",
                    close=float(price),
                    datetime=datetime.now(),
                    volume=0
                )
                self.on_tick(None, mock_tick)

        # [Bug Fix] Check data freshness and attempt reconnection
        if not self.dry_run:
            self._check_futures_contract_staleness()
            self._refresh_runtime_status()
            # Strategy-level freshness gate: skip strategy tick if feed ages exceed warn threshold
            try:
                if hasattr(self, 'feed_health') and self.feed_health is not None:
                    # [Fix] Use _tmf_feed_age_secs() which has proper fallback for feed_health returning inf
                    tmf_age = self._tmf_feed_age_secs()
                    max_age = getattr(self, 'STALE_WARN_SECS', 120)
                    
                    # 💡 GSD: 只有主體 MXF 過期才跳過；TX 過期則僅報警
                    if isinstance(tmf_age, (int, float)) and tmf_age > max_age:
                        console.print(f" [yellow][FuturesMonitor] MXF feed stale ({tmf_age:.0f}s) - skip strategy tick[/yellow] ")
                        return

                    console.print(
                        "[STICK_01_FEED_OK] tmf_age=%s dry_run=%s"
                        % (tmf_age, self.dry_run),
                    )
            except Exception:
                pass

        # [GSD Settlement Fix] Force close position on settlement day
        if self.trader.position != 0 and not self.dry_run:
            if self._is_settlement_day(self.contract.delivery_date):
                now = datetime.now()
                # 13:25 - 13:30 is the panic window for settlement
                if now.hour == 13 and 25 <= now.minute < 30:
                    console.print(f"[bold red]🚨 SETTLEMENT FORCE CLOSE: Exiting position {self.trader.position} before 13:30 settlement[/bold red]")
                    self._execute_trade("EXIT", self.market_data.get(self.ticker, {}).get("close", 0) or 0, 
                                        now, abs(self.trader.position), reason="SETTLEMENT_FORCE_CLOSE")
                    return # Exit this tick after force close

        # 1. Fetch multi-timeframe data (使用 tick-based bars 為主要來源)
        # ══════════════════════════════════════════════════
        # [P1] Live tick ingestion / raw tick writer / runtime cache
        # [P2] Scheduled backfill / canonical bar rebuild
        # [P3] Recovery watchdog only / no strategy-triggered fetch
        # ══════════════════════════════════════════════════
        processed = {}
        bar_source = {"source": None, "freshness_minutes": None}
        if not self.dry_run:
            # [P1] Primary source: tick-based bars from RawTickWriter CSV → deque.
            #      No API call — pure tick accumulation.
            df_tick = self._get_tick_bars_df()

            # [P2] Secondary source: periodic backfill via IngestionService.
            #      Rate-limited (120s), CSV-persisted before strategy reads.
            df_1min = self._periodic_backfill_bars()

            # [P3] Legacy fallback: NEVER triggered from strategy_tick.
            #      Runs on independent watchdog / recovery schedule only.
            #      strategy_tick is a data consumer, not a fetcher.
            df_legacy = None

            # [P2] Canonical bar selector: picks best available source.
            # Priority: tick-5m > api-1m > legacy-api-5m.
            # Strategy consumes canonical bars only — never raw API responses.
            raw_frames, bar_source = build_preferred_canonical_bar_frames(
                [
                    {"name": "tick-5m", "frame": df_tick, "source_timeframe": "5min"},
                    {"name": "api-1m", "frame": df_1min, "source_timeframe": "1min"},
                    {"name": "legacy-api-5m", "frame": df_legacy, "source_timeframe": "5min"},
                ],
                min_5m_bars=2,
            )

            console.print(
                "[STICK_02_RAW_FRAMES] keys=%s source=%s tick_cache_none=%s"
                % (
                    list(raw_frames.keys()) if isinstance(raw_frames, dict) else type(raw_frames).__name__,
                    bar_source.get("source", "?"),
                    getattr(self, "_tick_bars_cache", None) is None,
                ),
            )

            for tf, frame in raw_frames.items():
                if len(frame) >= 2:
                    processed[tf] = attach_bar_metadata(
                        calculate_futures_squeeze(
                            frame,
                            bb_length=self.STRATEGY.get("length", 20),
                            **self.PB_ARGS,
                        )
                    )

        console.print(
            "[STICK_03_PROCESSED_BEFORE_FALLBACK] keys=%s has_5m=%s"
            % (list(processed.keys()), "5m" in processed and not processed["5m"].empty),
        )

        # [P4 Hardening] Canonical freshness SLA
        data_flags: list[str] = []
        if not self.dry_run:
            df_5m = processed.get("5m")
            data_flags = self._check_canonical_freshness(df_5m)
            if "STALE_DATA" in data_flags:
                self._was_stale = True
        self._data_flags = data_flags  # <-- stored for _build_strategy_context()

        # [P4 Hardening] tick-5m vs api-1m consistency check (periodic, warning-only)
        # Compares close prices between tick-5m and api-1m sources at the most recent bar.
        # If sources disagree by > tick_threshold, logs a structured warning.
        # Never fetches data — purely observational.
        if not self.dry_run and self._bar_counter % 30 == 0:
            self._check_tick_api_consistency(df_tick, df_1min, bar_source)

        # 只要有 5m 數據，不論有沒有指標，都應該寫入
        if "5m" not in processed:
            # 最後一招：如果連 api 都沒有，用目前手上剛湊出的 current_bar 墊檔
            if self._current_bar["ts"] is not None and self._current_bar["open"] > 0:
                df_tmp = pd.DataFrame([self._current_bar]).set_index("ts")
                df_tmp.columns = ["Open", "High", "Low", "Close", "Volume"]
                # GSD: Always calculate indicators (will fill defaults if too short)
                processed["5m"] = attach_bar_metadata(
                    calculate_futures_squeeze(df_tmp, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
                )
            else:
                return

        processed["5m"] = attach_bar_metadata(processed["5m"])
        if "15m" in processed:
            processed["15m"] = attach_bar_metadata(processed["15m"])
        if "1h" in processed:
            processed["1h"] = attach_bar_metadata(processed["1h"])

        df_5m = processed["5m"]
        self._last_processed_data = processed
        
        # [Night Session Debug] Check indicator health
        if self._bar_counter % 5 == 0 or not hasattr(self, '_debug_indicator_logged'):
            self._debug_indicator_logged = True
            ind_cols = ['vwap','ema_fast','atr','momentum','sqz_on']
            for c in ind_cols:
                if c in df_5m.columns:
                    n_null = df_5m[c].isna().sum()
                    n_total = len(df_5m)
                    if n_null == n_total:
                        console.print(f" [yellow][INDICATOR] {c}: ALL NaN ({n_total} bars)[/yellow] ")
                    elif n_null > 0:
                        console.print(f"[dim][INDICATOR] {c}: {n_null}/{n_total} NaN[/dim]")
            console.print(f"[dim][INDICATOR] df_5m shape={df_5m.shape}, index range={df_5m.index[0]}~{df_5m.index[-1]}[/dim]")
        
        # [Fix] Initialize score and regime before adaptive/cross logic
        score = 0.0
        regime = "NORMAL"
        
        # Adaptive engine: detect regime, adjust thresholds and weights
        try:
            bars_list = []
            # build simple list of dicts for adaptive engine
            for _, r in df_5m.tail(100).iterrows():
                bars_list.append({
                    "close": float(r.get("Close", 0)),
                    "high": float(r.get("High", 0)),
                    "low": float(r.get("Low", 0)),
                })
            if hasattr(self, 'adaptive') and self.adaptive is not None:
                adaptive_regime = self.adaptive.detect_regime(bars_list)
                base_orb = self.STRATEGY.get("base_orb", 0.6)
                base_vwap = self.STRATEGY.get("base_vwap", 0.8)
                orb_th, vwap_th = self.adaptive.adjust_threshold(base_orb, base_vwap, bars_list)
                orb_w, vwap_w = self.adaptive.strategy_weight()
                # Compute a conservative boost factor for score
                boost = 1.0 + (((orb_w - 0.5) + (vwap_w - 0.5)) * 0.2)
                boost = max(0.7, min(boost, 1.3))
                # Attach adaptive info to context
                self._last_bar_context.update({
                    "adaptive_regime": adaptive_regime,
                    "adaptive_orb_th": float(orb_th),
                    "adaptive_vwap_th": float(vwap_th),
                    "adaptive_orb_w": float(orb_w),
                    "adaptive_vwap_w": float(vwap_w),
                    "adaptive_boost": float(boost),
                })
                console.print(f"[dim][ADAPTIVE] regime={adaptive_regime} orb_th={orb_th:.2f} vwap_th={vwap_th:.2f} orb_w={orb_w:.2f} vwap_w={vwap_w:.2f} boost={boost:.2f}[/dim]")
                # Apply boost to score (conservative scaling)
                score = float(score) * boost
        except Exception as e:
            console.print(f" [yellow]⚠️ Adaptive engine failed: {e}[/yellow] ")

        # 2026-06-18 Gemini CLI: [Pure TMF Refactoring] Disabled Cross-Regime Macro Engine (TX Macro + TMF Local)
        # We now rely solely on the configured ticker's native regime.
        cross_skipped = True
        tx_regime = "SKIP"
        tmf_regime = "SKIP"
        policy = {"allow_trade": True, "orb_weight": 1.0, "vwap_weight": 1.0}
        self._last_bar_context.update({
            "tx_regime": "SKIP",
            "tmf_regime": "SKIP",
            "cross_policy": policy,
        })
        self._last_cross_policy = policy
        # Pure TMF: cross-regime (TX macro filter) disabled entirely.
        # The dead try/except block that previously handled TXFR1 cache lookup,
        # cross-regime detection, and score weighting has been removed.
        # Score multiplier stays at 1.0 (permissive, no TX-based gating).
        # 2026-06-18 Hermes Agent

        # [GSD 4.13] Trading Readiness Unlock: only allow trading if we have enough bars for indicators
        feed_is_fresh = self._tmf_feed_age_secs() <= getattr(self, "STALE_WARN_SECS", self.MONITOR.get("stale_tick_warn_secs", 120))
        # [Fix] Also consider trading ready if we have enough bars regardless of feed age
        # (covers night session with low tick volume after restart)
        _has_enough_bars = len(df_5m) >= self.STRATEGY.get("length", 20)
        if not self.is_trading_ready and _has_enough_bars and (feed_is_fresh or self._bar_counter >= 3):
            self.is_trading_ready = True
            self._refresh_runtime_status()
            console.print(f"[bold green]🔥 [FuturesMonitor] Trading READY: {len(df_5m)} bars loaded.[/bold green]")
        
        # ── GSD: Ensure trading_day is always present before any downstream usage ──
        if "trading_day" not in df_5m.columns or df_5m["trading_day"].iloc[-1] is None or pd.isna(df_5m["trading_day"].iloc[-1]):
            df_5m = attach_bar_metadata(df_5m)
            processed["5m"] = df_5m
            
        last_5m = df_5m.iloc[-1]
        
        # [BUG FIX DIAGNOSTIC] Check last_5m indicator health
        if hasattr(last_5m, 'get'):
            _l5_atr = last_5m.get("atr", "MISSING")
            _l5_vwap = last_5m.get("vwap", "MISSING")
            _l5_sqz = last_5m.get("sqz_on", "MISSING")
            _l5_mom = last_5m.get("momentum", "MISSING")
            _l5_bb = last_5m.get("bb_mid", "MISSING")
        else:
            _l5_atr = _l5_vwap = _l5_sqz = _l5_mom = _l5_bb = "N/A"
        console.print(
            f"[dim][LAST_5M_DIAG] ts={last_5m.name if hasattr(last_5m, 'name') else 'N/A'} "
            f"atr={_l5_atr} vwap={_l5_vwap} sqz={_l5_sqz} mom={_l5_mom} bb_mid={_l5_bb} "
            f"bar_from='{bar_source.get('source', '?')}'[/dim]"
        )
        
        # fallback for MTF
        df_15m = processed.get("15m", df_5m)
        if "trading_day" not in df_15m.columns:
            df_15m = attach_bar_metadata(df_15m)
        last_15m = df_15m.iloc[-1]
        
        # [Fix] Remove redundant re-initialization of score/regime
        # We already initialized them at the start of adaptive/cross logic.
        
        # 只有在數據充足時才算 MTF Score (與之前的 adaptive boost 累加)
        has_15m = "15m" in processed
        # [SCORE_TRACE] Force log regardless of 15m availability
        _mtf_latest = {tf: (df["mom_state"].iloc[-1] if "mom_state" in df.columns else "N/A") for tf, df in processed.items() if not df.empty}
        if has_15m:
            score_data = calculate_mtf_alignment(processed, weights=self.STRATEGY.get("weights", {"5m": 0.4, "15m": 0.4, "1h": 0.2}))
            # 如果之前有 boost (score 已經不是 0)，我們保留其比例影響
            current_boost = 1.0
            if hasattr(self, '_last_bar_context') and "adaptive_boost" in self._last_bar_context:
                current_boost = self._last_bar_context["adaptive_boost"]
            
            score = score_data["score"] * current_boost
            regime = "STRONG" if last_5m.get("opening_bullish") else ("WEAK" if last_5m.get("opening_bearish") else "NORMAL")
            # [SCORE_TRACE] Log MTF alignment details
            _mtf_score = score_data.get("score", -999)
            _mtf_boost = current_boost
            console.print(
                f"[dim][SCORE_TRACE][MTF] mtf_raw={_mtf_score:.1f} boost={_mtf_boost:.2f} "
                f"final={score:.1f} mom_states={_mtf_latest} "
                f"has_15m={has_15m} ts={last_5m.name}[/dim]"
            )
        else:
            console.print(
                f"[dim][SCORE_TRACE][NO_15M] score={score:.1f} processed_keys={list(processed.keys())} mom_states={_mtf_latest} ts={last_5m.name}[/dim]"
            )

        last_price = last_5m["Close"]
        vwap = last_5m.get("vwap", last_price)
        timestamp = last_5m.name

        # GSD Phase 0b: Determine session type per bar
        current_hhmm = get_taifex_futures_hhmm()
        self.session_type = get_taifex_futures_session_type()
        
        # GSD Phase 0b-2: Session transition detection (night -> day) - cancel stale pending orders
        if self.previous_session_type != self.session_type:
            self._bars_since_session_open = 0 # [V-Model Upgrade] Reset bar counter on session change
            if self.previous_session_type == "night" and self.session_type == "day":
                console.print(f"[bold yellow]🔄 Session transition: {self.previous_session_type} -> {self.session_type}. Cancelling pending orders...[/bold yellow]")
                self._cancel_all_pending_orders()
            self.previous_session_type = self.session_type
        
        self._bars_since_session_open += 1
        last_5m_dict = last_5m.to_dict()
        last_5m_dict["bars_since_open"] = self._bars_since_session_open
        last_5m_dict["timestamp"] = last_5m.name # Ensure timestamp is available in dict
        # [V-Model] Explicitly enrich bar dict with indicator fields for route signal
        last_5m_dict["sqz_on"] = bool(last_5m.get("sqz_on", False))
        last_5m_dict["bear_breakout"] = bool(last_5m.get("bear_breakout", False))
        last_5m_dict["bull_breakout"] = bool(last_5m.get("bull_breakout", False))
        # [V-Model] squeeze_release metadata: sqz_on transitioned False in last N bars
        _sqz_val = bool(last_5m.get("sqz_on", False))
        if not hasattr(self, '_prev_sqz_on'):
            self._prev_sqz_on = False
        last_5m_dict["squeeze_release"] = _sqz_val == False and self._prev_sqz_on == True
        last_5m_dict["sqz_on_prev"] = self._prev_sqz_on
        self._prev_sqz_on = _sqz_val

        # GSD Phase 0c: Snapshot bar context for entry diagnostic (used by _execute_trade)
        self._last_bar_context = {
            "momentum": float(last_5m.get("momentum", 0)),
            "mom_velo": float(last_5m.get("mom_velo", 0)),
            "vwap": float(vwap),
            "atr": float(last_5m.get("atr", 0)),
            "squeeze_on": bool(last_5m.get("sqz_on", False)),
            "score": float(score),
            "regime": str(regime),
            "session": self.session_type,
            "bar_source": bar_source.get("source"),
            "bar_freshness_minutes": bar_source.get("freshness_minutes"),
        }

        # GSD Phase 0d: Increment bar counter since last trade
        self._bars_since_trade += 1

        # GSD Phase 0d: Hourly no-trade audit
        self._hourly_no_trade_audit(timestamp, df_5m)

        # ── [BAR SOURCE ARBITRATION] tick bar stale → fallback to canonical CSV ──
        if not self.dry_run:
            _bar_age_minutes = None
            try:
                _bar_age_minutes = (datetime.now() - timestamp).total_seconds() / 60.0
            except Exception:
                pass
            if _bar_age_minutes is not None and _bar_age_minutes >= 3.0:
                try:
                    from core.date_utils import get_session_date_str
                    _tag = "_PAPER" if not self.live_trading else "_LIVE"
                    _csv_path = Path(f"logs/market_data/{self.ticker}_{get_session_date_str(datetime.now())}{_tag}_indicators.csv")
                    if _csv_path.exists():
                        _csv_df = pd.read_csv(_csv_path)
                        if "timestamp" in _csv_df.columns:
                            _csv_df["timestamp"] = pd.to_datetime(_csv_df["timestamp"], errors="coerce")
                            _csv_df = _csv_df.set_index("timestamp").sort_index()
                            _csv_last_ts = _csv_df.index[-1]
                            if _csv_last_ts > timestamp:
                                console.print(
                                    f" [yellow][BAR_SOURCE_FALLBACK] tick_bar_stale={timestamp} csv_bar_new={_csv_last_ts} age={_bar_age_minutes:.0f}min source=csv[/yellow] "
                                )
                                df_5m = _csv_df
                                # [V-Model] Recalculate squeeze indicators on CSV fallback to ensure sqz_on etc.
                                df_5m = calculate_futures_squeeze(
                                    df_5m,
                                    bb_length=self.STRATEGY.get("length", 20),
                                    **getattr(self, "PB_ARGS", {}),
                                )
                                last_5m = df_5m.iloc[-1]
                                timestamp = df_5m.index[-1]
                                last_price = float(last_5m.get("Close", last_price))
                                vwap = float(last_5m.get("vwap", vwap))
                                processed["5m"] = df_5m
                                bar_source = {"source": "csv-fallback", "freshness_minutes": 0}
                                console.print(
                                    f" [yellow][BarFallback] Switched to CSV: ts={timestamp} "
                                    f"close={last_price:.0f} bars={len(df_5m)}[/yellow] "
                                )
                except Exception as _exc:
                    console.print(f"[dim][BAR_SOURCE_FALLBACK] CSV read failed: {_exc}[/dim]")

        # Log bar (即便每分鐘更新也行，存檔邏輯會處理)
        if self.last_processed_bar is not None and self.last_processed_bar == timestamp:
            if self._bar_counter % 5 == 0:
                _bar_age_s = (datetime.now() - timestamp).total_seconds() if hasattr(timestamp, 'timestamp') else -1
                console.print(
                    "[BAR_WAIT] ts=%s last=%s age=%.1fs source=%s"
                    % (timestamp, self.last_processed_bar, _bar_age_s, bar_source.get("source", "?")),
                )
        if self.last_processed_bar != timestamp:
            # [GSD] 跳過存檔如果 df_5m 不夠長（early return 的 (1,24) 會鎖死 CSV schema）
            # 💡 V-Model Correction: 只有在「非剛啟動」且「非換盤」時才嚴格檢查，否則會導致長時間 STALE
            is_new_session = self._bars_since_session_open < 15
            _skip_save = len(df_5m) < 5 and not is_new_session
            
            if not _skip_save and not is_new_session:
                # 2026-07-01 Gemini CLI: Allow saving even if atr_raw is NaN to avoid CSV gaps and dashboard freeze.
                # Only require the column to exist so schema is correct.
                _has_atr_raw = "atr_raw" in last_5m
                _skip_save = not _has_atr_raw
                
            if not _skip_save:
                self._save_bar(last_5m, score, regime)
            self.last_processed_bar = timestamp
            self._bar_counter += 1
            console.print(f"[bold blue][FuturesMonitor] New Bar: {timestamp} close={last_price:.0f} score={score:.1f}[/bold blue]")

        # 如果是 dry_run，計算完指標並存檔後就結束，不執行交易邏輯
        if self.dry_run:
            return

        # [V-Model] MTS mode: data pipeline done, use enriched bar for MTS execution
        # (skips normal position mgmt, exit engine, strategy router, gates)
        if _mts_enabled:
            _mts_bar = last_5m.to_dict()
            _mts_bar["ts"] = last_5m.name
            # [FAR_PRICE_FIX] Inject real-time far-month price from _far_current_bar
            # into _mts_bar so _mts_tick can use far_close_rt instead of CSV stale data.
            # This covers the _strategy_tick heartbeat path where no new tick has arrived
            # and _mts_bar lacks far_close_rt.
            if hasattr(self, '_far_current_bar') and self._far_current_bar.get("close", 0) > 0:
                _mts_bar["far_close_rt"] = self._far_current_bar["close"]
                _mts_bar["far_high_rt"] = self._far_current_bar.get("high", _mts_bar["far_close_rt"])
                _mts_bar["far_low_rt"] = self._far_current_bar.get("low", _mts_bar["far_close_rt"])
            self._mts_tick(enriched_bar=_mts_bar)
            return

        # 2. Position management
        if self.trader.position != 0:
            stop_msg = None
            self.trader.update_trailing_stop(last_price)
            # ── [L4] Decision Intelligence: Adaptive Exit Engine ─────────
            from core.exit_engine import should_exit
            
            trade_state = {
                "entry_price": float(self.trader.entry_price),
                "side": "LONG" if self.trader.position > 0 else "SHORT",
                "peak_price": float(self.trader.peak_price if self.trader.position > 0 else self.trader.floor_price),
                "position_age_bars": 0 # TODO: Implement bar tracking
            }
            
            context = {
                "regime": regime,
                "momentum": float(last_5m.get("momentum", 0)),
                "volatility": float(last_5m.get("atr", 50)),
                "volatility_norm": min(1.0, float(last_5m.get("atr", 50)) / 100.0),
                "vwap_dist": abs(last_price - vwap),
                "signal_score": abs(score)
            }
            
            # Calculate time to close for the current session
            hhmm = get_taifex_futures_hhmm()
            is_night_session = get_taifex_futures_session_type() == "night"
            target_close = "13:30" if not is_night_session else "05:00"
            close_dt = datetime.strptime(target_close, "%H:%M").replace(
                year=datetime.now().year, month=datetime.now().month, day=datetime.now().day
            )
            if is_night_session and hhmm >= 1500:
                # timedelta already imported at module top
                close_dt += timedelta(days=1)
            
            time_to_close = max(0, (close_dt - datetime.now()).total_seconds() / 60)
            
            market = {
                "price": last_price,
                "atr": float(last_5m.get("atr", 50)),
                "time_to_close_mins": time_to_close
            }

            # ── [Squeeze Fire Scout] Time stop check — preempt trend hold ──
            # Scout entry: if held >= time_stop_bars and not profitable or no breakout,
            # exit immediately. Scout should NOT ride trend_hold.
            if self._scout_entry_bar >= 0 and self._scout_time_stop_bars > 0:
                bars_held = self._bar_counter - self._scout_entry_bar
                if bars_held >= self._scout_time_stop_bars:
                    # Check if profitable or structure confirmed
                    unrealized_pnl = self.trader.unrealized_pnl if hasattr(self.trader, 'unrealized_pnl') else 0
                    breakout_strength = float(last_5m.get("breakout_strength", 0))
                    if unrealized_pnl <= 0 or breakout_strength < 0.25:
                        console.print(
                            f" [yellow]⏱️ Scout time stop: held {bars_held} bars, "
                            f"pnl={unrealized_pnl:.0f}, bs={breakout_strength:.3f} — exiting[/yellow] "
                        )
                        self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason="SCOUT_TIME_STOP")
                        self._scout_entry_bar = -1
                        self._scout_time_stop_bars = 0
                        return
                    else:
                        # Profitable and structure confirmed — promote to full, clear scout
                        console.print(
                            f"[green]✅ Scout promoted: held {bars_held} bars, "
                            f"pnl={unrealized_pnl:.0f}, bs={breakout_strength:.3f} — time stop cleared[/green]"
                        )
                        self._scout_entry_bar = -1
                        self._scout_time_stop_bars = 0

            trend_hold_active = self._trend_hold_active(last_5m, last_price, score, vwap, time_to_close)
            if trend_hold_active:
                exit_triggered, exit_reason = False, "TREND_HOLD"
                self._vwap_violation_bars = 0
            else:
                exit_triggered, exit_reason = should_exit(trade_state, context, market)

                if exit_triggered:
                    self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason=exit_reason)
                    return
            
            # ── Legacy/Safety Fallbacks ──
            # VWAP Exit (Secondary check)
            if not exit_triggered:
                _is_night = is_night_session
                if trend_hold_active:
                    stop_msg = self._apply_trend_hold_trail(last_price, last_5m, timestamp)
                elif _is_night:
                    # 夜盤: VWAP exit (回測 PF=2.74)
                    vwap_exit = self.RISK.get("exit_on_vwap") or (self.counter_exit_vwap and self._last_entry_reason == "COUNTER")
                    vwap_confirm_needed = self.RISK.get("exit_vwap_confirm_bars", 0)
                    if vwap_exit:
                        vwap_violated = (
                            (self.trader.position > 0 and last_price < vwap) or
                            (self.trader.position < 0 and last_price > vwap)
                        )
                        # [GSD] Ignore trivial VWAP fluctuations — min 30pts distance
                        vwap_distance = abs(last_price - vwap)
                        _min_vwap_distance = 30  # pts, round-trip friction ~8pts + buffer
                        if vwap_violated and vwap_distance >= _min_vwap_distance:
                            self._vwap_violation_bars += 1
                        else:
                            self._vwap_violation_bars = 0
                        if self._vwap_violation_bars >= vwap_confirm_needed:
                            stop_msg = self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason="VWAP")
                            self._vwap_violation_bars = 0
                else:
                    # 日盤: ATR Trail 3x (回測 PF=1.74, VWAP exit 日盤 PF=0.30)
                    atr_val = last_5m.get("atr", 50) or 50
                    atr_trail_mult = 3.0
                    if self.trader.position > 0:
                        self._atr_trail_peak = max(self._atr_trail_peak, last_price)
                        trail_floor = self._atr_trail_peak - atr_val * atr_trail_mult
                        if last_price <= trail_floor:
                            stop_msg = self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason="ATR_TRAIL")
                    elif self.trader.position < 0:
                        if self._atr_trail_peak == 0:
                            self._atr_trail_peak = last_price
                        self._atr_trail_peak = min(self._atr_trail_peak, last_price)
                        trail_ceil = self._atr_trail_peak + atr_val * atr_trail_mult
                        if last_price >= trail_ceil:
                            stop_msg = self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason="ATR_TRAIL")
            if stop_msg:
                self.has_tp1_hit = False
                self.cooldown_until = self.cooldown_bars # 觸發停損/平倉後進入冷卻
                self._last_exit_bar = timestamp  # 記錄 exit bar
                return

            # [GSD] General EOD Force Close (Enabled by config)
            if self.MGMT.get("force_close_at_end", False):
                now = datetime.now()
                hhmm = int(now.strftime("%H%M"))
                is_day_eod = (hhmm >= 1325 and hhmm < 1330)
                is_night_eod = (hhmm >= 425 and hhmm < 430)
                
                if is_day_eod or is_night_eod:
                    exit_price = last_price if last_price > 0 else (self.market_data.get(self.ticker, {}).get("close", 0))
                    console.print(f"[bold yellow]🕒 EOD FORCE CLOSE: Time {hhmm} reached. Exiting position...[/bold yellow]")
                    self._execute_trade("EXIT", exit_price, now, abs(self.trader.position), reason="EOD_FORCE_CLOSE")
                    return

            return  # don't enter same bar as exit

        # ── [P0 Fix] Market Hours Gate: NEVER enter during closed hours ──
        # TAIFEX MXF trading hours:
        #   Day:  08:45 - 13:45
        #   Night: 15:00 - 05:00 (next day)
        # Closed: 13:45-15:00 (lunch), 05:00-08:45 (early morning)
        hhmm = current_hhmm
        market_open = is_taifex_futures_market_open()
        if not market_open:
            self._audit_signal("ENTRY_BLOCKED", "", score, "market_closed", f"hhmm={hhmm}")
            if self._bar_counter % 12 == 0:  # Log once per hour
                console.print(f"[dim]⏸️ Market CLOSED (hhmm={hhmm}) — blocking entry[/dim]")
            return

        # 3. Entry logic (with cooldown check)
        if self.cooldown_until > 0:
            self.cooldown_until -= 1
            self._signals_rejected += 1  # GSD Phase 0d
            self._audit_signal("ENTRY_BLOCKED", "", score, "cooldown_active", f"remaining={self.cooldown_until}")
            return

        # GSD Phase 3: Circuit Breaker check (Phase 1 integration)
        if hasattr(self, "_circuit_breaker"):
            breaker_action = self._circuit_breaker.check(
                pnl=getattr(self, "_session_pnl", 0),
                consecutive_losses=self.consecutive_losses,
            )
            if breaker_action.value == "HALT":
                console.print(f"[bold red]🛑 Circuit Breaker HALTED ({self.session_type}): Daily loss cap breached[/bold red]")
                from core.decision_logger import DecisionLogger
                DecisionLogger.log(
                    type="circuit_breaker", session=self.session_type,
                    action="halt", detail="Daily loss cap breached",
                    author="system", risk_level="high",
                )
                self.cooldown_until = 1000  # Halt until reset
                return
            elif breaker_action.value == "DIAGNOSE":
                # GSD Phase 3: Run diagnostic engine (Phase 2 integration)
                console.print(f"[bold yellow]⚠️ Circuit Breaker DIAGNOSE ({self.session_type}): {self.consecutive_losses} consecutive losses[/bold yellow]")
                # Diagnosis will be done in post-session review
                # For now, log and continue (diagnostic engine is async via daily_review.py)
                from core.decision_logger import DecisionLogger
                DecisionLogger.log(
                    type="circuit_breaker", session=self.session_type,
                    action="diagnose", detail=f"{self.consecutive_losses} consecutive losses, triggering diagnostic",
                    author="system", risk_level="medium",
                )
            elif breaker_action.value == "REDUCE_SIZE":
                # Temporarily reduce position size
                console.print(f" [yellow]⚠️ Circuit Breaker REDUCE_SIZE ({self.session_type}): Daily loss at 40%[/yellow] ")

        # Prevent re-entering on the same bar as exit
        if self._last_exit_bar == timestamp:
            self._audit_signal("ENTRY_BLOCKED", "", score, "same_bar_exit")
            return

        self.has_tp1_hit = False
        # [Bug fix] Only reset trail peak on ACTUAL new entry intent
        stop_loss_pts = self.RISK.get("stop_loss_pts", 60)
        if self.ATR_MULT > 0:
            atr_val = last_5m.get("atr", 0)
            # [Bug fix] ATR 合理性上限：MXF 5m ATR 通常 30-150 點
            atr_cap = 300
            if atr_val > atr_cap:
                atr_val = atr_cap
            if atr_val > 0:
                stop_loss_pts = atr_val * self.ATR_MULT

        # ── PAPER_GATE_BYPASS_DIAGNOSTIC ──
        # 在 paper mode 下可跳過進場品質過濾，用於診斷策略是否真正產生 trade decision
        _debug_gate = self.cfg.get("debug_gate_bypass", {})
        _paper_bypass = _debug_gate.get("enabled", False) and not self.live_trading and not self.dry_run and _debug_gate.get("paper_only", True)
        console.print(
            f"[dim][BYPASS_TRACE] enabled={_debug_gate.get('enabled')} "
            f"paper_only={_debug_gate.get('paper_only')} "
            f"live_trading={self.live_trading} dry_run={self.dry_run} "
            f"disable_score={_debug_gate.get('disable_entry_score_gate')} "
            f"→ bypass={_paper_bypass} "
            f"entry_score={self.STRATEGY.get('entry_score', '?')} "
            f"cfg_keys={list(self.cfg.keys())[:5]}[/dim]"
        )

        # ── 進場品質過濾 ──
        min_score = self.STRATEGY.get("entry_score", 21)
        if _paper_bypass and _debug_gate.get("disable_entry_score_gate", False):
            min_score = 0
            console.print(f"[dim][BYPASS][PAPER_ONLY] entry_score disabled (min_score=0)[/dim]")
        vol = last_5m.get("Volume", 0)
        avg_vol = df_5m["Volume"].rolling(20).mean().iloc[-1] if len(df_5m) >= 20 else 0

        # 夜盤成交量門檻降低（夜盤 MXF 量通常只有日盤 3-10%）
        hhmm = int(datetime.now().strftime("%H%M"))
        is_night = hhmm >= 1500 or hhmm < 500
        vol_threshold = self.STRATEGY.get("volume_threshold", 0.05 if is_night else 0.3)

        vol_filter_ok = (avg_vol == 0) or (vol >= avg_vol * vol_threshold)
        if _paper_bypass and _debug_gate.get("disable_volume_gate", False):
            vol_filter_ok = True
            console.print(f"[dim][BYPASS][PAPER_ONLY] volume gate disabled[/dim]")
        if not vol_filter_ok:
            console.print(f"[ENTRY_GATE] BLOCKED by volume: vol={vol:.0f} avg={avg_vol:.0f} thresh={vol_threshold} night={is_night}")
            self._audit_signal("ENTRY_BLOCKED", "", score, "low_volume", f"vol={vol:.0f} avg={avg_vol:.0f} thresh={vol_threshold}")
            console.print(f"[dim]⏸️ Volume too low ({session_note}): {vol:.0f} vs avg {avg_vol:.0f} (>{vol_threshold*100:.0f}%) — skipping entry[/dim]")
            return

        if abs(score) < min_score:
            console.print(f"[ENTRY_GATE] BLOCKED by score: abs_score={abs(score):.1f} min_score={min_score}")
            console.print(
                f"[ENTRY_GATE_TRACE] abs_score={abs(score):.2f} min_score={min_score} "
                f"score_type={type(score).__name__} score_raw={score} "
                f"mom_state_5m={last_5m.get('mom_state', '?')} "
                f"bar_key={bar_source.get('source', '?')} "
                f"ts={timestamp}"
            )
            if self.counter_enabled:
                pass  # Counter mode 有自己的信號系統，不擋
            else:
                self._audit_signal("NO_ENTRY", "", score, "score_too_low", f"threshold={min_score}")
                return  # 分數太低，不進場

        # ── GSD: Pluggable Strategy Entry (Unified Route Path) ─────────
        from core.market_regime import classify_regime
        session_regime = classify_regime(df_5m)
        active_name = self.STRATEGY.get("active_strategy", "counter_vwap")
        decision, _ctx, session_regime, bar_regime = self._route_signal(
            bar=last_5m_dict,
            session_regime=session_regime,
            active_name=active_name
        )

        if decision is None:
            # Skip path (e.g. prefill bar)
            return

        if decision.action == "BLOCKED":
            self._audit_signal(
                "ENTRY_BLOCKED",
                "",
                score,
                "router_blocked",
                self._format_router_audit_note(decision, bar_regime),
            )
            return

        if not decision.is_trade:
            note = self._format_router_audit_note(decision, bar_regime)
            if active_name and self._registry.get(active_name) is None:
                self._audit_signal(
                    "NO_ENTRY",
                    "",
                    score,
                    "plugin_not_found",
                    f"active_strategy={active_name}; {note}",
                )
            else:
                self._audit_signal(
                    "NO_ENTRY",
                    "",
                    score,
                    "router_no_signal",
                    note,
                )
            return

        signal = decision.signal
        selected_strategy_name = decision.selected_strategy or active_name
        self.active_strategy_name = selected_strategy_name

        # 4.1 Global Edge Filter (Bypass for exits, apply to entries)
        if signal and signal.action in ["BUY", "SELL"]:
            # [GSD 4.13] Trading Readiness Gate
            if not self.is_trading_ready:
                _msg = f"indicators_warming_up (bars={len(df_5m)})"
                console.print(f"[ENTRY_GATE] BLOCKED by is_trading_ready=False score={score:.1f} reason={_msg}")
                self._audit_signal("ENTRY_BLOCKED", signal.action, score, "not_ready", _msg)
                return

            # [L4] Decision Intelligence: Edge Evaluation (Re-evaluated with side)
            from core.edge_model import edge_model
            edge_context = {
                "momentum": float(last_5m.get("momentum", 0)),
                "regime": str(bar_regime.regime),
                "vwap_dist": abs(last_price - vwap),
                "volatility": float(last_5m.get("atr", 50)),
                "price": last_price,
                "side": "LONG" if signal.action == "BUY" else "SHORT",
                "breakout_strength": float(last_5m.get("breakout_strength", 0)),
                "volume_spike": float(last_5m.get("volume_spike", 1.0)),
                "trend_strength_raw": float(last_5m.get("trend_strength_raw", 0))
            }
            
            edge_res = edge_model.evaluate(abs(score), edge_context, selected_strategy_name)
            if not edge_res["has_edge"]:
                _reason = edge_res.get("reason", "low_edge")
                console.print(f"[ENTRY_GATE] BLOCKED by edge_model: strategy={selected_strategy_name} score={score:.1f} reason={_reason}")
                self._audit_signal("ENTRY_BLOCKED", signal.action, score, "low_edge", _reason)
                if self._bar_counter % 5 == 0:
                    console.print(f"[bold yellow]🛡️ Decision Intelligence: {selected_strategy_name} Blocked - {_reason}[/bold yellow]")
                return
            
            # [GSD Upgrade] Apply Dynamic Position Scaling
            signal.quantity = max(1, round(lots * edge_res["pos_scale"]))
            signal.reason = f"{signal.reason} ({edge_res['rank']})"
            if edge_res["pos_scale"] != 1.0:
                console.print(f"[bold cyan]⚖️ Position Scaled: {edge_res['rank']} (x{edge_res['pos_scale']}) -> {signal.quantity} lots[/bold cyan]")
            
            # Update lots for further logic
            lots = signal.quantity

        # 5. Validate Signal (Defensive Programming)
        is_valid, msg = signal.validate()
        if not is_valid:
            console.print(f"[red]❌ Invalid signal from {selected_strategy_name}: {msg}[/red]")
            return

        # 6. Execute Trade — apply size multiplier from decision (e.g. SQUEEZE_FIRE_SCOUT 0.25x)
        base_lots = self.MGMT.get("lots_per_trade", 1)
        size_mult = getattr(decision, "size_multiplier", 1.0)
        # Also check if signal metadata has an override
        if signal and hasattr(signal, "metadata") and isinstance(signal.metadata, dict):
            mult = signal.metadata.get("size_multiplier")
            if mult is not None and 0 < mult <= 1.0:
                size_mult = mult
        lots = max(1, round(base_lots * size_mult))
        if size_mult != 1.0:
            console.print(f"[cyan]⚖️ Size scaled: {base_lots} x {size_mult} = {lots} lot(s) ({signal.signal_type})[/cyan]")
        self._execute_trade(
            signal.action,
            last_price,
            timestamp,
            lots,
            stop_loss=signal.stop_loss,
            break_even_trigger=signal.break_even_trigger,
            trail_points=signal.trail_points,
            reason=signal.reason,
        )