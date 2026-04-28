"""
Futures monitor — full strategy from daily_simulation.
Accepts an injected Shioaji API instance (no internal login).
"""
import sys
import os
import time
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
from core.bar_utils import attach_bar_metadata, build_preferred_canonical_bar_frames, resample_ohlcv
from core.date_utils import get_taifex_futures_hhmm, is_taifex_futures_market_open, get_taifex_futures_session_type
from core.spread_loader import get_spread_loader
from squeeze_futures.data.shioaji_client import ShioajiClient
from squeeze_futures.data.data_storage import save_trade
from squeeze_futures.data.tick_writer import RawTickWriter, get_trading_day_str
from squeeze_futures.data.kbar_writer import RawKbarWriter

try:
    from squeeze_futures.report.notifier import send_email_notification
except ImportError:
    send_email_notification = None

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
        self.ticker = "MXF"
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
        self._signals_generated = 0      # valid signals this hour
        self._signals_rejected = 0       # rejected signals this hour (reason, count)
        self._last_audit_hour = -1       # last hour we ran the audit
        self._data_stale_bars = 0        # consecutive bars with no new data
        self.options_monitor = None      # shared options monitor for hourly audit / repair
        
        # 💡 GSD: Market data cache for virtual ticks
        self.market_data = {"MTX": {"close": 0.0}}
        self.last_tick_at = time.time()  # [gstack] 數據新鮮度追蹤 — must init before _strategy_tick()
        self._last_real_tmf_tick_at = self.last_tick_at
        self._runtime_status = None

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
        # 💡 GSD: Initialize with current time bucket to prevent immediate flip
        self._last_bar_ts = int(time.time() / 300) * 300

        # ── [V-Model] SpreadLoader for calendar spread data (near-far spread_z) ──
        self._spread_loader = get_spread_loader()
        self._spread_loaded = self._spread_loader.load_latest_csv()
        if self._spread_loaded:
            print(f"[V-Model] SpreadLoader initiated: {self._spread_loader.status()}", flush=True)
        else:
            print("[V-Model] SpreadLoader: no calendar spread data found", flush=True)

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
                margin_per_lot=self.EXEC.get("margin_per_lot", 40000),
            )
        else:
            # We don't change initial_balance after start, but we can update fees and margin
            self.trader.fee_per_side = self.EXEC.get("broker_fee_per_side", 20)
            self.trader.exchange_fee_per_side = self.EXEC.get("exchange_fee_per_side", 0)
            self.trader.tax_rate = self.EXEC.get("tax_rate", 0)
            self.trader.margin_per_lot = self.EXEC.get("margin_per_lot", 40000)

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
        """[Wave 2 optimization] Lazy DF conversion: rebuild cache only on new bar."""
        if self._tick_bars_cache is None and len(self._tick_bars_deque) > 0:
            # Build DataFrame from deque
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
        self._registry = StrategyRegistry()
        self._registry.discover()
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
            console.print(f"[yellow]⚠️ Circuit Breaker init failed: {e}[/yellow]")
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
                market=MarketData(last_bar={}),
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
            console.print("[yellow][FuturesMonitor] dry-run: skipping contract fetch[/yellow]")
            return True

        # [GSD Fix] Warm-up from Parquet SSOT (Wave 5 Integration)
        try:
            # Try MXF first, then TXFR1 as fallback
            from core.data_manager import data_manager
            ticker_warm = self.ticker  # e.g. "MXF"
            df_hist = data_manager.load_historical(ticker_warm)
            if df_hist.empty or len(df_hist) < 20:
                # Fallback: try TXFR1 which has broader coverage
                df_hist = data_manager.load_historical("TXFR1")
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
                console.print(f"[yellow][FuturesMonitor] Parquet warm-up empty, trying CSV fallback...[/yellow]")
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
            tmf_list = list(self.api.Contracts.Futures.MXF)
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
                            console.print(f"[yellow][FuturesMonitor] Settlement day detected ({now_str}), skipping expired contract {c.code} after 13:30[/yellow]")
                
                # Sort by delivery date (ascending)
                tmf_sorted = sorted(valid_contracts, key=lambda c: c.delivery_date)
                
                if tmf_sorted:
                    # Pick the first one (nearest delivery)
                    self.contract = tmf_sorted[0]
                    console.print(f"[green][FuturesMonitor] ✓ MXF front-month: {self.contract.code} (delivers {self.contract.delivery_date})[/green]")
                else:
                    # Fallback to absolute nearest if no valid ones found (shouldn't happen in live)
                    self.contract = sorted(tmf_list, key=lambda c: c.delivery_date)[0]
                    console.print(f"[yellow][FuturesMonitor] No future delivery found, using absolute nearest: {self.contract.code}[/yellow]")
                
                # Log all available codes for verification
                all_codes = [f"{c.code}({c.delivery_date})" for c in tmf_sorted]
                console.print(f"[dim][FuturesMonitor] Valid MXF queue: {', '.join(all_codes)}[/dim]")

                # [Far Month] Select first contract with DIFFERENT delivery date for dual chart
                front_delivery = self.contract.delivery_date if self.contract else None
                self.far_contract = None
                for c in tmf_sorted[1:]:
                    if c.delivery_date != front_delivery:
                        self.far_contract = c
                        break
                if self.far_contract is not None:
                    console.print(f"[green][FuturesMonitor] ✓ MXF far-month: {self.far_contract.code} (delivers {self.far_contract.delivery_date})[/green]")
                else:
                    self.far_contract = None
                    console.print(f"[yellow][FuturesMonitor] No far-month contract available[/yellow]")
            else:
                console.print("[red][FuturesMonitor] No MXF contracts found![/red]")
        except Exception as e:
            console.print(f"[red][FuturesMonitor] Error selecting MXF contract: {e}[/red]")

        # [Bug Fix] Add contract rollover check
        self._last_contract_code = self.contract.code if self.contract else None

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
        # [BUG FIX] Use session date so backfill writes to the same file as _save_bar.
        # Previously used today.strftime('%Y%m%d') which causes dual-file contamination
        # during night session (bar times belong to next trading day).
        date_str = get_session_date_str(today)
        tag = "_DRY" if self.dry_run else ("_LIVE" if self.live_trading else "_PAPER")
        csv_path = Path(f"logs/market_data/{self.ticker}_{date_str}{tag}_indicators.csv")
        
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
                    console.print("[yellow][FuturesMonitor] Repaired corrupt startup CSV timestamp header[/yellow]")
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
                has_indicator_data = (
                    not existing.empty
                    and "momentum" in existing.columns
                    and existing["momentum"].notna().any()
                )
                if has_indicator_data:
                    console.print(f"[dim][FuturesMonitor] Skipping raw backfill — session CSV already has indicator data (last_ts={last_ts})[/dim]")
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
        """Prefer real MXF feed freshness over synthetic continuity timestamps."""
        try:
            if hasattr(self, "feed_health") and self.feed_health is not None:
                age = self.feed_health.age("MXF")
                if age is not None and math.isfinite(float(age)):
                    return max(0.0, float(age))
        except Exception:
            pass
        # feed_health can report inf before the first real MXF tick arrives after startup.
        # Fall back to the local MXF timer so watchdog logic keeps its intended grace window.
        last_real_tick = getattr(self, "_last_real_tmf_tick_at", self.last_tick_at)
        return max(0.0, time.time() - last_real_tick)

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
            self._set_runtime_status(SystemReadiness.MONITORING)

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
        console.print(
            f"[yellow][IngestionWatchdog] "
            f"reason=feed_stale symbol={symbol} "
            f"tick_age_secs={secs_since_tick:.0f} "
            f"last_bar_ts={last_bar_ts} "
            f"canonical_age_secs={canonical_age_secs} "
            f"action=check_contract "
            f"result=degraded[/yellow]"
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

        # If we exceed critical threshold, stop the monitor so external supervisor restarts the process
        if secs_since_tick >= critical:
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
            raise RuntimeError(f"MXF tick stale for {secs_since_tick} seconds (>{critical}), exiting monitor.")

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
                f"[yellow][IngestionWatchdog] "
                f"reason=contract_expired symbol={symbol} "
                f"tick_age_secs={secs_since_tick:.0f} "
                f"last_bar_ts={last_bar_ts} "
                f"canonical_age_secs={canonical_age_secs} "
                f"action=rollover "
                f"result=triggered[/yellow]"
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
                f"[yellow][IngestionWatchdog] "
                f"reason=rollover_failed symbol={symbol} "
                f"tick_age_secs={secs_since_tick:.0f} "
                f"last_bar_ts={last_bar_ts} "
                f"canonical_age_secs={canonical_age_secs} "
                f"action=rollover "
                f"result=exception:{e}[/yellow]"
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
                    f"[yellow][IngestionWatchdog] "
                    f"reason=feed_stale symbol={symbol} "
                    f"tick_age_secs={secs_since_tick:.0f} "
                    f"last_bar_ts={last_bar_ts} "
                    f"canonical_age_secs={canonical_age_secs} "
                    f"action=fetch_recovery_kline "
                    f"result=empty_response[/yellow]"
                )
        except Exception as e:
            console.print(
                f"[yellow][IngestionWatchdog] "
                f"reason=feed_stale symbol={symbol} "
                f"tick_age_secs={secs_since_tick:.0f} "
                f"last_bar_ts={last_bar_ts} "
                f"canonical_age_secs={canonical_age_secs} "
                f"action=fetch_recovery_kline "
                f"result=exception:{e}[/yellow]"
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
            console.print(f"[yellow]⚠️ Error checking contract expiration: {e}[/yellow]")
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
            console.print(f"[yellow]⚠️ Error checking settlement day: {e}[/yellow]")
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
            console.print(f"[yellow]⚠️ Error calculating settlement time: {e}[/yellow]")
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
            
            # Get all available contracts
            tmf_list = list(self.api.Contracts.Futures.MXF)
            if not tmf_list:
                console.print("[yellow]⚠️ No MXF contracts available[/yellow]")
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
                    console.print(f"[yellow]⚠️ Re-subscription failed: {e}[/yellow]")
        except Exception as e:
            console.print(f"[yellow]⚠️ Contract rollover check error: {e}[/yellow]")

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
                        # Also prime the current bar with the last tick's price
                        self._current_bar["ts"] = None  # Will be set on next incoming tick
                        self._current_bar["open"] = last_bar["close"]
                        self._current_bar["high"] = last_bar["close"]
                        self._current_bar["low"] = last_bar["close"]
                        self._current_bar["close"] = last_bar["close"]
                        self._current_bar["volume"] = 0

        except Exception as e:
            console.print(f"[yellow][FuturesMonitor] Tick CSV rebuild failed (non-fatal): {e}[/yellow]")

    def on_tick(self, exchange, tick):
        self.last_tick_at = time.time()  # [gstack] 更新數據更新時間

        # [Far Month] Handle far-month tick accumulation (independent from near-month)
        if self.far_contract and tick.code == self.far_contract.code:
            self._accumulate_far_tick(tick)
            return

        # 💡 GSD: Data Continuity Fix
        # Use strict matching for the primary MXF contract
        is_tmf = self.contract and tick.code == self.contract.code
        # For MTX, we still allow startswith for the heartbeat, but we MUST NOT use its price for MXF bars
        is_mtx = tick.code.startswith("MXF") or tick.code.startswith("MTX")

        if not is_tmf and not is_mtx:
            return
            
        # [GSD Data Safety] Write raw tick to CSV FIRST — before any in-memory use
        # Only write real TMF ticks (not MTX heartbeat ticks which use stale price)
        if is_tmf:
            self._write_raw_tick(tick)
            self._last_real_tmf_tick_at = self.last_tick_at
            price = float(tick.close)
            self._last_tmf_price = price  # Cache for heartbeat
            self._refresh_runtime_status()
        else:
            # It's an MTX heartbeat tick
            if not hasattr(self, '_last_tmf_price') or self._last_tmf_price <= 0:
                # No MXF price yet, can't build bar
                return
            price = self._last_tmf_price
            
        # Only count volume for MXF to keep indicators accurate
        vol = int(getattr(tick, "volume", 0)) if is_tmf else 0
        
        # [Wave 1 optimization] Use integer time bucketing to avoid expensive pd.Timestamp().floor()
        # Only compute Timestamp when bar changes (every 5 minutes)
        tick_ts = pd.Timestamp(tick.datetime)
        ts_int = int(tick_ts.timestamp() / 300) * 300
        
        bar = self._current_bar
        if bar["ts"] is None or ts_int > self._last_bar_ts:
            # 💡 GSD: Only flip the bar if we have a NEW time bucket
            if bar["ts"] is not None and bar["open"] > 0:
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
            bar["ts"] = ts
            self._last_bar_ts = ts_int
            bar["open"] = bar["high"] = bar["low"] = bar["close"] = price
            bar["volume"] = vol
        elif ts_int == self._last_bar_ts:
            # Accumulate into current bar
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["volume"] += vol
            
            # [gstack Safety Guard] Real-time stop loss check on every tick
            if not self.dry_run and self.trader.position != 0:
                # 1. Update trailing stop peak/floor
                self.trader.update_trailing_stop(price)
                # 2. Check for SL breach
                self._check_stop_loss(tick.datetime, price)
        else:
            # Old data packet, ignore
            return
        cb = self.client._tick_callbacks.get(tick.code)
        if cb:
            cb(exchange, tick)

    # [Far Month] Accumulate far-month ticks into independent 5-min bars
    def _accumulate_far_tick(self, tick):
        """Accumulate far-month MXF ticks into _far_tick_bars_deque (5-min bars).
        Does NOT affect strategy signals, stop loss, or orders."""
        price = float(tick.close)
        vol = int(getattr(tick, "volume", 0))
        tick_ts = pd.Timestamp(tick.datetime)
        ts_int = int(tick_ts.timestamp() / 300) * 300

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
            console.print(f"[yellow]Safety stop error: {e}[/yellow]")

    def _cancel_safety_stop(self):
        """Cancel the exchange-side safety stop after normal exit."""
        if not self._safety_stop_trade or not self.api:
            return
        try:
            self.api.cancel_order(self._safety_stop_trade)
            console.print("[dim]🛡️ Safety stop cancelled[/dim]")
        except Exception as e:
            console.print(f"[yellow]Safety stop cancel error: {e}[/yellow]")
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
            console.print(f"[yellow]⚠️  {verdict}: {note}[/yellow]")
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
            console.print(f"[yellow]⚠️ Trade backup failed: {e}[/yellow]")

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
                cur_price = float(self.market_data.get("MTX", {}).get("close", 0))
            except Exception:
                cur_price = 0.0
            
            if cur_price <= 0:
                try:
                    cur_price = float(self.market_data.get("MXF", {}).get("close", 0))
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
            console.print(f"[yellow]⚠️ Failed to append lifecycle order: {e}[/yellow]")
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
                    console.print(f"[yellow]⚠️ Failed to recover order from row: {e}[/yellow]")
                    continue
            
            if recovered_count > 0:
                console.print(f"[bold cyan]♻️ Recovered {recovered_count} futures orders from trades CSV[/bold cyan]")
                # Save immediately to orders JSON
                self._save_orders_file_wrapper()
            
        except Exception as e:
            console.print(f"[yellow]Futures order recovery from trades CSV failed: {e}[/yellow]")

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

    def _apply_confirmed_futures_deal(self, event):
        from core.order_management.order import OrderStatus
        from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit

        pending = self._pending_lifecycle_orders.get(event.order_id)
        if pending is None or event.fill_qty <= 0:
            return None

        deal_key = event.deal_id or f"{event.order_id}:{event.fill_qty}:{event.fill_price}"
        if deal_key in self._applied_lifecycle_deals:
            return None

        signal = pending.get("signal")
        if signal not in ("BUY", "SELL", "EXIT", "PARTIAL_EXIT"):
            return None

        ts = datetime.now()
        lots = int(event.fill_qty)
        price = float(event.fill_price or 0)
        reason = pending.get("reason")
        stop_loss = pending.get("stop_loss")
        break_even_trigger = pending.get("break_even_trigger")
        trail_points = pending.get("trail_points")
        cross_policy = pending.get("cross_policy")

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
                console.print(f"[green]📦 Confirmed deal: {action} {event.fill_qty} @ {event.fill_price:.0f} deal={event.deal_id} → {msg}[/green]")

        def _on_cancel_callback(event):
            console.print(f"[yellow]🚫 Order CANCELLED: {event.order_id} ({event.reason})[/yellow]")
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
        self.paper_fill_sim.process_tick(self._make_synthetic_tick(price, ts))
        return order.order_id

    def _make_synthetic_tick(self, price, ts):
        """Create a synthetic tick object from price/timestamp for PaperFillSimulator."""
        tick = type("Tick", (), {})()
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
            console.print(f"[yellow]Margin check failed: {e} — allowing order[/yellow]")
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
        }, ticker="MXF")

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
            parts.append(f"notes={' | '.join(decision.notes[:3])}")
        return "; ".join(parts)

    def _route_entry_signal(self, last_5m, df_5m, df_15m, timestamp, active_name, attribution_recorder=None):
        from core.market_regime import classify_regime

        session_regime = classify_regime(df_5m)
        bar = last_5m.to_dict()
        
        # Use the new _route_signal method with attribution support
        return self._route_signal(
            bar=bar,
            session_regime=session_regime,
            active_name=active_name,
            attribution_recorder=attribution_recorder
        )

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
            except Exception:
                skew_signal = None

        # [V-Model] Enrich bar with calendar spread data (spread_z, near_close, far_close)
        if self._spread_loaded:
            try:
                self._spread_loader.enrich_bar(bar)
            except Exception as e:
                print(f"[V-Model] enrich_bar failed: {e}", flush=True)

        ctx = StrategyContext(
            market=MarketData(
                last_bar=bar,
                df_5m=df_5m,
                df_15m=df_15m,
                timestamp=bar.get('timestamp', ''),
                session=int(bar.get('session', 0)),
                regime=session_regime,
                flags=self._data_flags if hasattr(self, '_data_flags') and self._data_flags else None,
                skew_signal=skew_signal,
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
        # Build context
        ctx = self._build_strategy_context(bar, session_regime)

        print(f"[DEBUG_MARK] route_signal entered bar_ts={bar.get('timestamp')}", flush=True)

        # [Phase 2 Fix] Skip routing on prefill/warmup bars (old data from Parquet/CSV)
        # Check if bar timestamp is from current trading day
        bar_ts = bar.get("timestamp")
        if bar_ts is not None:
            from core.date_utils import get_trading_day
            try:
                bar_td = get_trading_day(pd.Timestamp(bar_ts))
                current_td = get_trading_day(pd.Timestamp(datetime.now()))
                print(f"[DEBUG_MARK] prefill_check bar_td={bar_td} current_td={current_td}", flush=True)
                if bar_td != current_td:
                    console.print(f"[dim][Router] Skip prefill bar: ts={bar_ts} trading_day={bar_td} != current={current_td}[/dim]")
                    return None, ctx, session_regime, None
            except Exception:
                pass

        # [Phase 2: Skew Filter] Gate pre-check — block entry when skew signal
        # indicates extreme downside risk with sufficient confidence.
        skew_signal = getattr(ctx.market, "skew_signal", None)
        if skew_signal and isinstance(skew_signal, dict):
            direction = skew_signal.get("direction", "UNKNOWN")
            confidence = skew_signal.get("confidence", 0.0)
            skew_threshold = self.cfg.get("skew", {}).get("filter_threshold", 0.70)
            if direction == "BEAR" and confidence >= skew_threshold and self.trader.position == 0:
                console.print(
                    f"[yellow][SkewGate] BLOCK entry — skew BEAR "
                    f"confidence={confidence:.2f} >= {skew_threshold:.2f}[/yellow]"
                )
                bar_regime = classify_futures_bar_regime(bar, session_regime=session_regime)
                decision = FuturesRouterDecision(
                    action="skip",
                    reason=f"SKEW_GATE_BEAR_conf_{confidence:.2f}",
                    regime=bar_regime.regime,
                    bias=bar_regime.bias,
                )
                return decision, ctx, session_regime, bar_regime

        # Get pending orders if not provided
        if pending_orders is None:
            pending_orders = self._get_symbol_pending_orders()
        
        # Classify bar regime
        bar_regime = classify_futures_bar_regime(bar, session_regime=session_regime)
        
        # [Phase 2 Debug] Router input features — use raw print to avoid rich truncation
        print(
            f"[DEBUG_MARK] classified regime={getattr(bar_regime, 'regime', None)} "
            f"bias={getattr(bar_regime, 'bias', None)}",
            flush=True,
        )
        print(
            f"[RouterInput] bull_breakout={bar.get('breakout_strength', 0):.4f} "
            f"bear_breakout={bar.get('bear_breakout_strength', 0):.4f} "
            f"trend={bar.get('trend_strength_raw', 0):.6f} "
            f"adx={bar.get('adx', 0):.2f} "
            f"regime={getattr(bar_regime, 'regime', 'NONE')} "
            f"bias={getattr(bar_regime, 'bias', 'NONE')}",
            flush=True,
        )

        # Route signal with optional attribution
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
                console.print(f"[yellow][FuturesMonitor] Block entry: invalid price {price}[/yellow]")
                return None
            # 2) Feed freshness (use monitor thresholds)
            try:
                if hasattr(self, 'feed_health') and self.feed_health is not None:
                    tx_age = self.feed_health.age('TX')
                    tmf_age = self.feed_health.age('MXF')
                    max_age = getattr(self, 'STALE_WARN_SECS', 120)
                    if tx_age > max_age or tmf_age > max_age:
                        self._audit_signal("ENTRY_BLOCKED", "", 0, "feed_stale", f"TX={tx_age:.0f}s MXF={tmf_age:.0f}s")
                        console.print(f"[yellow][FuturesMonitor] Block entry: feed stale TX={tx_age:.0f}s MXF={tmf_age:.0f}s[/yellow]")
                        return None
            except Exception:
                pass
            # 3) Do not enter on the same bar as last trade
            if hasattr(self, '_last_trade_ts') and self._last_trade_ts is not None:
                try:
                    if ts == self._last_trade_ts:
                        self._audit_signal("ENTRY_BLOCKED", "", 0, "same_bar", "same_bar_as_last_trade")
                        console.print(f"[yellow][FuturesMonitor] Block entry: same bar as last trade ({ts})[/yellow]")
                        return None
                except Exception:
                    pass
            # 4) Enforce simple position guard: avoid new entry when a position exists (prevent pyramiding)
            if getattr(self, 'trader', None) is not None and self.trader.position != 0:
                self._audit_signal("ENTRY_BLOCKED", "", 0, "position_not_zero", f"position={self.trader.position}")
                console.print(f"[yellow][FuturesMonitor] Block entry: position not zero ({self.trader.position})[/yellow]")
                return None
            # 5) Minimum stop loss check (prevent tiny stops)
            try:
                min_sl = self.RISK.get('min_stop_loss_pts', 10)
                if stop_loss is not None and stop_loss < min_sl:
                    self._audit_signal("ENTRY_BLOCKED", "", 0, "stop_loss_too_small", f"sl={stop_loss}")
                    console.print(f"[yellow][FuturesMonitor] Block entry: stop_loss {stop_loss} < min {min_sl}[/yellow]")
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
            console.print(f"[yellow]⚠️  Loss #{self.consecutive_losses}: {pnl_pts:.1f} pts ({reason or 'unknown'}) [{sess}][/yellow]")
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
            if send_email_notification:
                    send_email_notification(
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
                console.print(f"[yellow]🛡️ Migrating indicator CSV: adding {missing}[/yellow]")
                for c in missing:
                    df[c] = pd.NA
                # Sort columns to keep a stable order
                df = df.reindex(columns=sorted(df.columns))
                df.to_csv(path, index=False)
            
            # Cache the column order for subsequent appends
            self._indicator_cols = sorted(df.columns)
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
            "breakout_strength": float(getattr(self, "_last_bar_context", {}).get("breakout_strength", 0.0)),
            "volume_spike": float(getattr(self, "_last_bar_context", {}).get("volume_spike", 1.0)),
            "trend_strength_raw": float(getattr(self, "_last_bar_context", {}).get("trend_strength_raw", 0.0)),
            "open": row.get("Open", 0), "high": row.get("High", 0), "low": row.get("Low", 0), "close": row.get("Close", 0),
            "volume": row.get("Volume", 0), "amount": row.get("Amount", 0),
            "bull_align": row.get("bullish_align", False), "bear_align": row.get("bearish_align", False),
            "in_pb_zone": row.get("in_bull_pb_zone", False) or row.get("in_bear_pb_zone", False),
        })

        # 2. Schema Normalization (Once per session)
        if not hasattr(self, "_indicators_migrated") or not self._indicators_migrated:
            self._ensure_indicator_schema(path, list(data.keys()))
            self._indicators_migrated = True

        # 3. Fast Append with Timestamp Gating
        try:
            current_ts = pd.to_datetime(data["timestamp"])
            if not path.exists():
                # First time: Write header
                cols = sorted(data.keys())
                self._indicator_cols = cols
                pd.DataFrame([data])[cols].to_csv(path, index=False)
                self._last_saved_ts = current_ts
            else:
                # [GSD Idempotency Fix] Read last TS from file if not in memory
                if not hasattr(self, "_last_saved_ts") or self._last_saved_ts is None:
                    try:
                        # Optimization: read only last line to get last timestamp
                        last_line = subprocess.check_output(['tail', '-1', str(path)]).decode().split(',')[0]
                        self._last_saved_ts = pd.to_datetime(last_line)
                    except:
                        self._last_saved_ts = pd.Timestamp.min

                # Only append if this is a NEW bar
                if current_ts > self._last_saved_ts:
                    cols = getattr(self, "_indicator_cols", sorted(data.keys()))
                    row_df = pd.DataFrame([data])
                    row_df.reindex(columns=cols).to_csv(path, mode='a', header=False, index=False)
                    self._last_saved_ts = current_ts
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
                f"[yellow][P4] Canonical 5m data stale: last_bar_ts={last_bar_dt.strftime('%H:%M:%S')}, "
                f"age={elapsed_secs:.0f}s (>SLA {sla_secs}s). Flagging STALE_DATA.[/yellow]"
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
            f"[yellow][IngestionWatchdog] "
            f"reason=tick_api_mismatch "
            f"tick_close={tick_last_close:.1f} "
            f"api_close={api_last_close:.1f} "
            f"diff={diff:.1f} "
            f"threshold={max_diff:.1f} "
            f"tick_last_ts={tick_last_ts} "
            f"api_last_ts={api_last_ts} "
            f"active_source={source_name} "
            f"action=none "
            f"result=warning_only[/yellow]"
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
                console.print(f"[yellow]Futures position recovery failed: {e}[/yellow]")

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
                console.print(f"[yellow]⚠️ [Phase B] Backfill returned no data, will rely on tick accumulation.[/yellow]")
        
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
                pending = self.order_mgr.get_pending_orders()
                for order in pending:
                    try:
                        self.order_mgr.cancel_order(order.id)
                        console.print(f"[yellow]✓ Cancelled pending order {order.id}[/yellow]")
                        cancelled_count += 1
                    except Exception as e:
                        console.print(f"[red]Failed to cancel order {order.id}: {e}[/red]")
            else:
                # Fallback: direct API cancellation for futures orders
                # This is a simplistic implementation - may need enhancement
                console.print("[yellow]⚠️ Order manager not enabled; manual API cancellation not implemented yet[/yellow]")
        except Exception as e:
            console.print(f"[red]Error in _cancel_all_pending_orders: {e}[/red]")
        
        if cancelled_count == 0:
            console.print("[dim]No pending orders to cancel[/dim]")
        else:
            console.print(f"[bold green]✅ Cancelled {cancelled_count} pending order(s)[/bold green]")

    def _strategy_tick(self):
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
            # Use current MTX close/mid if available to drive bar building
            price = self.market_data.get("MTX", {}).get("close", 0)
            if price > 0:
                # Mock a tick object to feed into self.on_tick
                from types import SimpleNamespace
                # Use current real time, but ensure we don't skip into next bucket prematurely
                mock_tick = SimpleNamespace(
                    code="TMF_VIRTUAL",
                    close=price,
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
                    tx_age = self.feed_health.age('TX')
                    tmf_age = self.feed_health.age('MXF')
                    max_age = getattr(self, 'STALE_WARN_SECS', 120)
                    
                    # 💡 GSD: 只有主體 MXF 過期才跳過；TX 過期則僅報警
                    if tmf_age > max_age:
                        console.print(f"[yellow][FuturesMonitor] MXF feed stale ({tmf_age:.0f}s) - skip strategy tick[/yellow]")
                        return
                    
                    if tx_age > max_age:
                        if self._bar_counter % 5 == 0: # 減少日誌噪音
                            console.print(f"[yellow][FuturesMonitor] TX feed quiet ({tx_age:.0f}s) - continuing in degraded mode[/yellow]")
            except Exception:
                pass

        # [GSD Settlement Fix] Force close position on settlement day
        if self.trader.position != 0 and not self.dry_run:
            if self._is_settlement_day(self.contract.delivery_date):
                now = datetime.now()
                # 13:25 - 13:30 is the panic window for settlement
                if now.hour == 13 and 25 <= now.minute < 30:
                    console.print(f"[bold red]🚨 SETTLEMENT FORCE CLOSE: Exiting position {self.trader.position} before 13:30 settlement[/bold red]")
                    self._execute_trade("EXIT", self.market_data.get("MTX", {}).get("close", 0) or 0, 
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

            for tf, frame in raw_frames.items():
                if len(frame) >= 2:
                    processed[tf] = attach_bar_metadata(
                        calculate_futures_squeeze(
                            frame,
                            bb_length=self.STRATEGY.get("length", 20),
                            **self.PB_ARGS,
                        )
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
                        console.print(f"[yellow][INDICATOR] {c}: ALL NaN ({n_total} bars)[/yellow]")
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
            console.print(f"[yellow]⚠️ Adaptive engine failed: {e}[/yellow]")

        # Cross-regime decision (TX macro + MXF local)
        try:
            tx_regime = "UNKNOWN"
            tmf_regime = "UNKNOWN"
            policy = None
            tx_bars_list = None

            # Prefer in-memory TX bars built from live ticks when available
            try:
                if hasattr(self, 'tx_bar_builder') and self.tx_bar_builder is not None:
                    tx_bars = self.tx_bar_builder.bars()
                    if tx_bars and len(tx_bars) >= 20:
                        tx_bars_list = [{
                            "close": float(b.get("close", 0)),
                            "high": float(b.get("high", 0)),
                            "low": float(b.get("low", 0)),
                        } for b in tx_bars[-100:]]
            except Exception:
                tx_bars_list = None

            # ═══ TX CACHE ONLY (no on-demand API calls) ═══
            # TX bars for cross-regime engine are populated exclusively
            # during backfill/startup via IngestionService.fetch_backfill()
            # (which calls _prefetch_tx_bars() as a side effect).
            # Access is via self._ingestion.get_tx_cache() or fallback
            # to _tx_cached_kbars (populated by the old path).
            # If neither live ticks nor cached TX bars are available,
            # we skip cross-regime entirely (no fallback API call).
            if tx_bars_list is None:
                try:
                    tx_cached = self._ingestion.get_tx_cache()
                    if tx_cached is None:
                        tx_cached = getattr(self, '_tx_cached_kbars', None)
                    if tx_cached is not None and len(tx_cached) >= 20:
                        tx_bars_list = list(tx_cached[-100:])
                        console.print(f"[dim][TX] Using {len(tx_bars_list)} cached TX bars for cross-regime[/dim]")
                except Exception:
                    tx_bars_list = None

            tmf_bars_list = bars_list if 'bars_list' in locals() else []

            # If we have TX bars built from ticks, ensure time alignment with MXF 5m bars
            cross_skipped = False
            try:
                if tx_bars_list and df_5m is not None:
                    aligned = self._bars_time_aligned(tx_bars_list, df_5m)
                    if not aligned:
                        console.print(f"[yellow][CROSS] tx/tmf bars not time-aligned (skip cross-regime) [/yellow]")
                        # Skip cross-regime gating entirely: set permissive policy and jump ahead
                        policy = {"allow_trade": True, "orb_weight": 1.0, "vwap_weight": 1.0}
                        self._last_bar_context.update({
                            "tx_regime": "SKIP",
                            "tmf_regime": "SKIP",
                            "cross_policy": policy,
                        })
                        self._last_cross_policy = policy
                        console.print(f"[dim][CROSS] Skipped cross-regime due to misalignment; permissive fallback[/dim]")
                        cross_skipped = True
                        
            except Exception:
                pass

            if cross_skipped:
                # Jump ahead — skip regime detection and cross_engine, use permissive policy
                tx_regime = "SKIP"
                tmf_regime = "SKIP"
            else:
                if getattr(self, 'tx_detector', None) is not None and tx_bars_list:
                    tx_regime = self.tx_detector.detect(tx_bars_list)
                if getattr(self, 'tmf_detector', None) is not None:
                    tmf_regime = self.tmf_detector.detect(tmf_bars_list)
                if getattr(self, 'cross_engine', None) is not None:
                    # Cross engine supports freshness flags; use feed_health if present
                    tx_fresh = True
                    tmf_fresh = True
                    try:
                        if hasattr(self, 'feed_health') and self.feed_health is not None:
                            tx_fresh = self.feed_health.age('TX') <= FEED_STALE_SECS
                            tmf_fresh = self.feed_health.age('MXF') <= FEED_STALE_SECS
                    except Exception:
                        tx_fresh = tmf_fresh = True
                    policy = self.cross_engine.decide(tx_regime, tmf_regime, tx_fresh=tx_fresh, tmf_fresh=tmf_fresh)
                else:
                    policy = {"allow_trade": True, "orb_weight": 1.0, "vwap_weight": 1.0}

                self._last_bar_context.update({
                    "tx_regime": tx_regime,
                    "tmf_regime": tmf_regime,
                    "cross_policy": policy,
                })
                # Persist last cross policy for later use in order callbacks / audit
                self._last_cross_policy = policy
            console.print(f"[dim][CROSS] tx={tx_regime} tmf={tmf_regime} allow={policy.get('allow_trade', False)} orb_w={policy.get('orb_weight', 0):.2f} vwap_w={policy.get('vwap_weight', 0):.2f} reason={policy.get('reason','')}[/dim]")

            if not policy.get('allow_trade', False):
                console.print(f"[yellow]🔒 CrossPolicy: trading disabled by tx={tx_regime} tmf={tmf_regime} reason={policy.get('reason','')}[/yellow]")
                score = 0.0
            else:
                mult = max(0.5, min(1.3, 0.6 * policy.get('orb_weight', 1.0) + 0.4 * policy.get('vwap_weight', 1.0)))
                score = float(score) * mult
        except Exception as e:
            console.print(f"[yellow]⚠️ Cross-regime integration failed: {e}[/yellow]")

        # [GSD 4.13] Trading Readiness Unlock: only allow trading if we have enough bars for indicators
        feed_is_fresh = self._tmf_feed_age_secs() <= getattr(self, "STALE_WARN_SECS", self.MONITOR.get("stale_tick_warn_secs", 120))
        if not self.is_trading_ready and len(df_5m) >= self.STRATEGY.get("length", 20) and feed_is_fresh:
            self.is_trading_ready = True
            self._refresh_runtime_status()
            console.print(f"[bold green]🔥 [FuturesMonitor] Trading READY: {len(df_5m)} bars loaded.[/bold green]")
        
        # ── GSD: Ensure trading_day is always present before any downstream usage ──
        if "trading_day" not in df_5m.columns or df_5m["trading_day"].iloc[-1] is None or pd.isna(df_5m["trading_day"].iloc[-1]):
            df_5m = attach_bar_metadata(df_5m)
            processed["5m"] = df_5m
            
        last_5m = df_5m.iloc[-1]
        
        # fallback for MTF
        df_15m = processed.get("15m", df_5m)
        if "trading_day" not in df_15m.columns:
            df_15m = attach_bar_metadata(df_15m)
        last_15m = df_15m.iloc[-1]
        
        # [Fix] Remove redundant re-initialization of score/regime
        # We already initialized them at the start of adaptive/cross logic.
        
        # 只有在數據充足時才算 MTF Score (與之前的 adaptive boost 累加)
        if "15m" in processed:
            score_data = calculate_mtf_alignment(processed, weights=self.STRATEGY.get("weights", {"5m": 0.4, "15m": 0.4, "1h": 0.2}))
            # 如果之前有 boost (score 已經不是 0)，我們保留其比例影響
            current_boost = 1.0
            if hasattr(self, '_last_bar_context') and "adaptive_boost" in self._last_bar_context:
                current_boost = self._last_bar_context["adaptive_boost"]
            
            score = score_data["score"] * current_boost
            regime = "STRONG" if last_5m.get("opening_bullish") else ("WEAK" if last_5m.get("opening_bearish") else "NORMAL")

        last_price = last_5m["Close"]
        vwap = last_5m.get("vwap", last_price)
        timestamp = last_5m.name

        # GSD Phase 0b: Determine session type per bar
        current_hhmm = get_taifex_futures_hhmm()
        self.session_type = get_taifex_futures_session_type()
        
        # GSD Phase 0b-2: Session transition detection (night -> day) - cancel stale pending orders
        if self.previous_session_type != self.session_type:
            if self.previous_session_type == "night" and self.session_type == "day":
                console.print(f"[bold yellow]🔄 Session transition: {self.previous_session_type} -> {self.session_type}. Cancelling pending orders...[/bold yellow]")
                self._cancel_all_pending_orders()
            self.previous_session_type = self.session_type

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

        # Log bar (即便每分鐘更新也行，存檔邏輯會處理)
        if self.last_processed_bar != timestamp:
            self._save_bar(last_5m, score, regime)
            self.last_processed_bar = timestamp
            self._bar_counter += 1
            console.print(f"[bold blue][FuturesMonitor] New Bar: {timestamp} close={last_price:.0f} score={score:.1f}[/bold blue]")

        # 如果是 dry_run，計算完指標並存檔後就結束，不執行交易邏輯
        if self.dry_run:
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
                    exit_price = last_price if last_price > 0 else (self.market_data.get("MTX", {}).get("close", 0))
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
                console.print(f"[yellow]⚠️ Circuit Breaker REDUCE_SIZE ({self.session_type}): Daily loss at 40%[/yellow]")

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

        # ── 進場品質過濾 ──
        min_score = self.STRATEGY.get("entry_score", 21)
        vol = last_5m.get("Volume", 0)
        avg_vol = df_5m["Volume"].rolling(20).mean().iloc[-1] if len(df_5m) >= 20 else 0

        # 夜盤成交量門檻降低（夜盤 MXF 量通常只有日盤 3-10%）
        hhmm = int(datetime.now().strftime("%H%M"))
        is_night = hhmm >= 1500 or hhmm < 500
        vol_threshold = self.STRATEGY.get("volume_threshold", 0.05 if is_night else 0.3)

        vol_filter_ok = (avg_vol == 0) or (vol >= avg_vol * vol_threshold)
        if not vol_filter_ok:
            session_note = "夜盤" if is_night else "日盤"
            self._audit_signal("ENTRY_BLOCKED", "", score, "low_volume", f"vol={vol:.0f} avg={avg_vol:.0f} thresh={vol_threshold}")
            console.print(f"[dim]⏸️ Volume too low ({session_note}): {vol:.0f} vs avg {avg_vol:.0f} (>{vol_threshold*100:.0f}%) — skipping entry[/dim]")
            return

        if abs(score) < min_score:
            if self.counter_enabled:
                pass  # Counter mode 有自己的信號系統，不擋
            else:
                self._audit_signal("NO_ENTRY", "", score, "score_too_low", f"threshold={min_score}")
                return  # 分數太低，不進場

        # ── GSD: Pluggable Strategy Entry ────────────────────────────
        active_name = self.STRATEGY.get("active_strategy", "counter_vwap")
        decision, _ctx, session_regime, bar_regime = self._route_entry_signal(
            last_5m, df_5m, df_15m, timestamp, active_name
        )

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
                self._audit_signal("ENTRY_BLOCKED", signal.action, score, "not_ready", "Indicators warming up")
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
                self._audit_signal("ENTRY_BLOCKED", signal.action, score, "low_edge", edge_res["reason"])
                if self._bar_counter % 5 == 0:
                    console.print(f"[bold yellow]🛡️ Decision Intelligence: {selected_strategy_name} Blocked - {edge_res['reason']}[/bold yellow]")
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

        # 6. Execute Trade
        lots = self.MGMT.get("lots_per_trade", 1)
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
