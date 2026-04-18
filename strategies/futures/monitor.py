"""
Futures monitor — full strategy from daily_simulation.
Accepts an injected Shioaji API instance (no internal login).
"""
import sys
import os
import time
import yaml
import traceback
from pathlib import Path
from collections import deque
from datetime import datetime, timedelta
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

# Old imports for backward compatibility (fallback)
from squeeze_futures.data.shioaji_client import ShioajiClient
from squeeze_futures.data.data_storage import save_trade
from squeeze_futures.data.data_storage import save_trade

# GSD: 策略外掛系統
from core.strategy_registry import StrategyRegistry
from core.strategy_context import StrategyContext, PositionView, MarketData
from core.signal import Signal
from squeeze_futures.data.shioaji_client import ShioajiClient
from squeeze_futures.data.data_storage import save_trade

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
        self.dry_run = dry_run
        self.cfg = self._load_config(config_path)
        self.ticker = "TMF"
        self.contract = None
        self._running = False

        # Wrap injected api into ShioajiClient without re-login
        self.client = ShioajiClient.__new__(ShioajiClient)
        self.client.api = api
        self.client.is_logged_in = not dry_run
        self.client._tick_callbacks = {}
        self.client._kbar_callbacks = {}
        self.client._latest_kbars = {}

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
        self.cooldown_until = 0

        # GSD Phase 0b: Consecutive losses tracker (separate for day/night)
        self.consecutive_losses = 0
        self.session_losses = []  # [(timestamp, pnl_pts, exit_reason, session)]
        self.session_type = None  # "day" or "night", set per bar
        self.previous_session_type = None  # Track previous session for transition detection
        self._last_bar_context = {}  # Phase 0c: snapshot for entry diagnostic

        # GSD Phase 3: Circuit Breaker integration (Phase 1)
        self._circuit_breaker = None
        self._session_pnl = 0.0  # Session PnL for circuit breaker

        # Squeeze Failure Counter mode
        self.COUNTER = self.STRATEGY.get("counter_mode", {})
        self.counter_enabled = self.COUNTER.get("enabled", False)
        self.counter_auto_regime = self.COUNTER.get("auto_regime", True)
        self.counter_confirm_bars = self.COUNTER.get("confirm_bars", 5)
        self.counter_atr_sl_mult = self.COUNTER.get("atr_sl_mult", 1.0)
        self.counter_exit_vwap = self.COUNTER.get("exit_on_vwap", True)
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

        # ── [L3] Order Lifecycle Manager (independent of live_trading) ──
        self._use_order_manager = self.MONITOR.get("use_order_manager", False)
        self.order_mgr = None
        self.paper_fill_sim = None
        if self._use_order_manager:
            from core.order_management.order_manager import OrderManager
            from core.order_management.paper_fill import PaperFillSimulator
            _om_mode = "live" if self.live_trading else "paper"
            broker = self.client if self.live_trading else None
            self.order_mgr = OrderManager(mode=_om_mode, broker_adapter=broker)
            if _om_mode == "paper":
                self.paper_fill_sim = PaperFillSimulator(self.order_mgr)
                self.order_mgr.set_simulator(self.paper_fill_sim)
            self._wire_order_callbacks()
            console.print(f"[green]📋 Order Lifecycle Manager enabled ({_om_mode} mode)[/green]")

        self.last_tick_at = time.time()  # [gstack] 數據新鮮度追蹤

        # Adaptive engine (lightweight regime/threshold adapter)
        try:
            from strategies.adaptive_engine import AdaptiveEngine
            self.adaptive = AdaptiveEngine()
        except Exception:
            self.adaptive = None

        # Cross-regime: TX / TMF detectors and policy engine
        try:
            from strategies.cross_regime import RegimeDetector, TMFLocalDetector, CrossRegimeEngine
            self.tx_detector = RegimeDetector()
            self.tmf_detector = TMFLocalDetector()
            self.cross_engine = CrossRegimeEngine()
        except Exception:
            self.tx_detector = None
            self.tmf_detector = None
            self.cross_engine = None

        # GSD Phase 0d: Hourly no-trade audit tracking
        self._last_trade_ts = None       # timestamp of last trade
        self._bars_since_trade = 0       # bars since last trade
        self._signals_generated = 0      # valid signals this hour
        self._signals_rejected = 0       # rejected signals this hour (reason, count)
        self._last_audit_hour = -1       # last hour we ran the audit
        self._data_stale_bars = 0        # consecutive bars with no new data
        
        # 💡 GSD: Market data cache for virtual ticks
        self.market_data = {"MTX": {"close": 0.0}}

        # Trader
        self.trader = PaperTrader(
            ticker=self.ticker,
            initial_balance=self.EXEC.get("initial_balance", 100000),
            point_value=get_point_value(self.ticker),
            fee_per_side=self.EXEC.get("broker_fee_per_side", 20),
            exchange_fee_per_side=self.EXEC.get("exchange_fee_per_side", 0),
            tax_rate=self.EXEC.get("tax_rate", 0),
        )
        self.has_tp1_hit = False
        self.last_processed_bar = None
        self._last_exit_bar = None  # 防止同根 K bar exit 後再進場
        self._last_entry_reason = None
        self._safety_stop_trade = None  # Exchange-side safety stop order
        # 💡 GSD: Initialize with current time bucket to prevent immediate flip
        self._last_bar_ts = int(time.time() / 300) * 300

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
            strategy.init(dummy_ctx)
            self._active_strategy_name = active_name
            console.print(f"[green]🔧 Pre-initialized strategy: {active_name}[/green]")

        # Tick-based bar builder (Initialize always to avoid AttributeError in dry_run)
        # [Wave 2 optimization] Use deque for O(1) append/trim instead of DataFrame.loc + slicing
        self._tick_bars_deque = deque(maxlen=300)
        self._tick_bars_cache = None  # Cached DF for indicator calculations
        self._current_bar = {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ts": None}

        if self.dry_run:
            console.print("[yellow][FuturesMonitor] dry-run: skipping contract fetch[/yellow]")
            return True

        # [GSD Fix] Warm-up from Parquet SSOT (Wave 5 Integration)
        try:
            from core.data_manager import data_manager
            df_hist = data_manager.load_historical(self.ticker)
            if not df_hist.empty:
                df_warm = df_hist.tail(100)
                for ts, row in df_warm.iterrows():
                    self._tick_bars_deque.append({
                        "open": row["Open"], "high": row["High"], "low": row["Low"], 
                        "close": row["Close"], "volume": row["Volume"], "ts": ts
                    })
                # Initialize cache to prevent immediate indicator re-calc
                self._tick_bars_cache = df_warm[["Open", "High", "Low", "Close", "Volume"]].copy()
                console.print(f"[green][FuturesMonitor] ✓ Warmed up with {len(df_warm)} bars from Parquet DB[/green]")
        except Exception as e:
            console.print(f"[dim][FuturesMonitor] Parquet warm-up failed: {e}[/dim]")

        # 獲取TMF合約
        try:
            tmf_list = list(self.api.Contracts.Futures.TMF)
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
                    console.print(f"[green][FuturesMonitor] ✓ TMF front-month: {self.contract.code} (delivers {self.contract.delivery_date})[/green]")
                else:
                    # Fallback to absolute nearest if no valid ones found (shouldn't happen in live)
                    self.contract = sorted(tmf_list, key=lambda c: c.delivery_date)[0]
                    console.print(f"[yellow][FuturesMonitor] No future delivery found, using absolute nearest: {self.contract.code}[/yellow]")
                
                # Log all available codes for verification
                all_codes = [f"{c.code}({c.delivery_date})" for c in tmf_sorted]
                console.print(f"[dim][FuturesMonitor] Valid TMF queue: {', '.join(all_codes)}[/dim]")
            else:
                console.print("[red][FuturesMonitor] No TMF contracts found![/red]")
        except Exception as e:
            console.print(f"[red][FuturesMonitor] Error selecting TMF contract: {e}[/red]")

        # [Bug Fix] Add contract rollover check
        self._last_contract_code = self.contract.code if self.contract else None

        # Pre-fill from kbars if available (使用新的方法)
        try:
            # 首先嘗試使用新的方法獲取當天1分鐘K棒
            df_1min = self._fetch_today_kbars()
            if df_1min is not None and len(df_1min) >= 1:
                # 重採樣為5分鐘K棒
                df = df_1min.resample("5min").agg({
                    "Open": "first",
                    "High": "max",
                    "Low": "min",
                    "Close": "last",
                    "Volume": "sum"
                }).dropna()
                
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
                # 如果新方法失敗，嘗試舊的get_kline方法
                df = self.client.get_kline(self.ticker, interval="5m")
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
                    console.print(f"[green][FuturesMonitor] pre-filled {len(self._tick_bars_deque)} bars from get_kline[/green]")
                    
                    # [GSD Fix] Backfill night session gaps on startup
                    self._backfill_night_gaps(df)
        except Exception:
            pass
        return True

    def _backfill_night_gaps(self, api_df):
        """[GSD Fix] On startup, check if today's CSV has night session data.
        If missing or incomplete, merge API bars with existing CSV."""
        if self.dry_run or not self.api:
            return
        
        from pathlib import Path
        today = datetime.now()
        date_str = today.strftime('%Y%m%d')
        tag = "_DRY" if self.dry_run else ("_LIVE" if self.live_trading else "_PAPER")
        csv_path = Path(f"logs/market_data/{self.ticker}_{date_str}{tag}_indicators.csv")
        
        # Read existing CSV
        if csv_path.exists():
            try:
                existing = pd.read_csv(csv_path, parse_dates=['timestamp'])
                existing.set_index('timestamp', inplace=True)
                last_ts = existing.index.max()
                console.print(f"[dim][FuturesMonitor] Existing CSV: {len(existing)} bars, latest={last_ts}[/dim]")
            except Exception:
                existing = pd.DataFrame()
                last_ts = None
        else:
            existing = pd.DataFrame()
            last_ts = None
        
        # Find bars from API that are newer than CSV
        if not api_df.empty and last_ts is not None:
            new_bars = api_df[api_df.index > last_ts]
            if not new_bars.empty:
                console.print(f"[bold cyan]🔧 Backfilling {len(new_bars)} missing bars from API ({new_bars.index[0]} → {new_bars.index[-1]})[/bold cyan]")
                
                # Append new bars to existing CSV
                if existing.empty:
                    combined = new_bars.copy()
                else:
                    combined = pd.concat([existing, new_bars])
                
                # Add missing columns if needed
                for col in ['score', 'regime', 'session', 'bull_align', 'bear_align', 'in_pb_zone']:
                    if col not in combined.columns:
                        combined[col] = 0 if col in ['score'] else ('NORMAL' if col == 'regime' else (2 if col == 'session' else False))
                
                combined.to_csv(csv_path)
                console.print(f"[green][FuturesMonitor] ✅ Backfill complete: {len(combined)} total bars in CSV[/green]")

    def _check_futures_contract_staleness(self):
        """[Wave 1 Fix] Check if TMF ticks are stale and attempt recovery.

        Behavior:
        - If no new tick for < warn_secs: no-op.
        - If >= warn_secs but < critical_secs: attempt light recovery (rollover/resubscribe) and try fetching kline.
        - If >= critical_secs: mark monitor not running and raise to trigger supervisor restart.
        """
        if self.dry_run or not self.api:
            return
        
        secs_since_tick = time.time() - self.last_tick_at
        warn = getattr(self, 'STALE_WARN_SECS', self.MONITOR.get('stale_tick_warn_secs', 120))
        critical = getattr(self, 'STALE_CRITICAL_SECS', self.MONITOR.get('stale_tick_critical_secs', 600))
        if secs_since_tick < warn:
            return

        console.print(f"[yellow]⚠️ TMF data stale for {secs_since_tick/60:.1f} min, checking contract...[/yellow]")

        # If we exceed critical threshold, stop the monitor so external supervisor restarts the process
        if secs_since_tick >= critical:
            console.print(f"[red]🚨 TMF data stale CRITICAL: {secs_since_tick/60:.1f} min. Shutting down to trigger supervisor restart.[/red]")
            try:
                if self.contract:
                    self.api.quote.unsubscribe(self.contract, quote_type='tick')
            except Exception:
                pass
            # Mark monitor as not running and raise to break out of run loop
            self._running = False
            raise RuntimeError(f"TMF tick stale for {secs_since_tick} seconds (>{critical}), exiting monitor.")

        # Between warn and critical: attempt light recovery
        console.print(f"[dim]Attempting light recovery (re-subscribe / rollover / fetch) after {secs_since_tick/60:.1f} min stale[/dim]")

        # Check for expiry/rollover
        today_str = datetime.now().strftime("%Y/%m/%d")
        if self.contract and self.contract.delivery_date < today_str:
            console.print(f"[yellow]⚠️ TMF contract {self.contract.code} expired (delivery: {self.contract.delivery_date})[/yellow]")
            self._check_contract_rollover()
            # Update last tick time so we don't spam retry immediately
            self.last_tick_at = time.time()
            return

        # If contract valid but no ticks, could be session transition or connection drop
        # Try contract rollover/resubscribe first
        try:
            self._check_contract_rollover()
        except Exception as e:
            console.print(f"[yellow]⚠️ Contract rollover attempt failed: {e}[/yellow]")

        # Try a light kline fetch to recover bars if possible
        try:
            df = self.client.get_kline(self.ticker, interval="5m")
            if df is not None and not df.empty:
                # Replace deque with recent tail to refresh state
                tail = df.tail(200)
                self._tick_bars_deque.clear()
                for _, row in tail.iterrows():
                    self._tick_bars_deque.append({
                        "open": row.get('Open', row.get('open', 0)),
                        "high": row.get('High', row.get('high', 0)),
                        "low": row.get('Low', row.get('low', 0)),
                        "close": row.get('Close', row.get('close', 0)),
                        "volume": row.get('Volume', row.get('volume', 0)),
                        "ts": row.name,
                    })
                self._tick_bars_cache = None
                console.print(f"[green]✅ Light kline refresh succeeded: loaded {len(self._tick_bars_deque)} bars[/green]")
                # Update last_tick_at to avoid immediate retry
                self.last_tick_at = time.time()
                return
        except Exception as e:
            console.print(f"[yellow]⚠️ Light kline refresh failed: {e}[/yellow]")

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
        """[GSD Fix] Check if TMF contract has rolled over and re-subscribe if needed."""
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
            tmf_list = list(self.api.Contracts.Futures.TMF)
            if not tmf_list:
                console.print("[yellow]⚠️ No TMF contracts available[/yellow]")
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

    def on_tick(self, exchange, tick):
        self.last_tick_at = time.time()  # [gstack] 更新數據更新時間
        
        # 💡 GSD: Data Continuity Fix
        # Use strict matching for the primary TMF contract
        is_tmf = self.contract and tick.code == self.contract.code
        # For MTX, we still allow startswith for the heartbeat, but we MUST NOT use its price for TMF bars
        is_mtx = tick.code.startswith("MXF") or tick.code.startswith("MTX")
        
        if not is_tmf and not is_mtx:
            return
            
        # [GSD Settlement Fix] If it's an MTX tick, it's just a heartbeat to drive timing.
        # We use the last known TMF price to avoid price contamination (TMF != MTX).
        if is_tmf:
            price = float(tick.close)
            self._last_tmf_price = price # Cache for heartbeat
        else:
            # It's an MTX heartbeat tick
            if not hasattr(self, '_last_tmf_price') or self._last_tmf_price <= 0:
                # No TMF price yet, can't build bar
                return
            price = self._last_tmf_price
            
        # Only count volume for TMF to keep indicators accurate
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
        
        secs_since_tick = time.time() - self.last_tick_at
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

    def _monitor_trade_records(self, timestamp):
        """
        Monitor trade records integrity and perform hourly checks.
        
        Returns:
            str: Summary of trade records status
        """
        try:
            from pathlib import Path
            import pandas as pd
            from datetime import datetime, timedelta
            
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
            from datetime import datetime
            
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
                    cur_price = float(self.market_data.get("TMF", {}).get("close", 0))
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

    # ── Order Lifecycle (L3 Integration) ──
    def _wire_order_callbacks(self):
        """Wire OrderManager callbacks to PaperTrader and audit system."""
        from core.order_management.order import OrderStatus, OrderSide

        def _on_fill_callback(event):
            if event.status == OrderStatus.FILLED:
                ts = datetime.now()
                stop_loss = getattr(self, "_pending_stop_loss", 60)
                max_lots = self.MGMT.get("max_positions", 1)
                if event.side == OrderSide.BUY:
                    msg = self.trader.execute_signal("BUY", event.fill_price, ts,
                                                      lots=event.fill_qty, max_lots=max_lots, stop_loss=stop_loss)
                    console.print(f"[green]📦 Order FILLED: BUY {event.fill_qty} @ {event.fill_price:.0f} → {msg}[/green]")
                else:
                    msg = self.trader.execute_signal("SELL", event.fill_price, ts,
                                                      lots=event.fill_qty, max_lots=max_lots, stop_loss=stop_loss)
                    console.print(f"[red]📦 Order FILLED: SELL {event.fill_qty} @ {event.fill_price:.0f} → {msg}[/red]")
                # Save to exports
                try:
                    from squeeze_futures.data.data_storage import save_trade
                    save_trade({
                        "type": "BUY" if event.side == OrderSide.BUY else "SELL",
                        "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                        "price": event.fill_price,
                        "lots": event.fill_qty,
                        "direction": "LONG" if event.side == OrderSide.BUY else "SHORT",
                        "pnl_pts": 0, "pnl_cash": 0, "friction_cost": 0,
                        "reason": getattr(self, "_last_entry_reason", "ORDER_MANAGER"),
                    })
                except Exception:
                    pass
                self._save_orders_file_wrapper()

        def _on_cancel_callback(event):
            console.print(f"[yellow]🚫 Order CANCELLED: {event.order_id} ({event.reason})[/yellow]")
            self._save_orders_file_wrapper()

        def _on_reject_callback(event):
            console.print(f"[red]❌ Order REJECTED: {event.order_id} ({event.reason})[/red]")
            self._save_orders_file_wrapper()

        self.order_mgr.register_callback("on_fill", _on_fill_callback)
        self.order_mgr.register_callback("on_cancel", _on_cancel_callback)
        self.order_mgr.register_callback("on_reject", _on_reject_callback)
        self._save_orders_file_wrapper()

    def _submit_order_via_manager(self, signal, price, ts, lots, stop_loss=None, reason=None):
        """Submit order through OrderManager instead of direct PaperTrader call."""
        from core.order_management.order import OrderType, OrderSide

        side = OrderSide.BUY if signal == "BUY" else OrderSide.SELL
        order_type = OrderType.MARKET  # Default to market; can be configured

        order = self.order_mgr.create_order(
            symbol=self.ticker, side=side, order_type=order_type,
            quantity=lots, strategy=reason or "UNKNOWN",
        )
        self.order_mgr.submit(order, exchange_ordno=f"PAPER-{order.order_id}")
        self.paper_fill_sim.register(order)

        # Store stop_loss for callback to use
        self._pending_stop_loss = stop_loss or self.RISK.get("stop_loss_pts", 60)

        console.print(f"[cyan]📤 Order SUBMITTED: {signal} {lots} @ {price:.0f} ({reason}) "
                      f"[order_id={order.order_id}][/cyan]")

        # Process the current tick immediately for market orders
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
            required = margin.initial_margin if margin.initial_margin > 0 else 17000  # TMF 一口約 17,000
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
        """Record signal audit trail to CSV (thread-safe, TMF file)."""
        from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
        save_signal_audit({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "signal": signal_type,
            "side": side,
            "score": score,
            "rejection": rejection_reason,
            "note": note,
        }, ticker="TMF")

    def _execute_trade(self, signal, price, ts, lots, *, stop_loss=None, break_even_trigger=None, trail_points=None, reason=None):
        action = None
        if signal == "BUY":
            action = "Buy"
        elif signal == "SELL":
            action = "Sell"
        elif signal in ("EXIT", "PARTIAL_EXIT"):
            if self.trader.position == 0:
                from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
                save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "no_position", "lots": lots})
                return None
            action = "Sell" if self.trader.position > 0 else "Buy"

        live_ready = self.live_trading and not self.dry_run and self.contract is not None
        if live_ready and action is not None:
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
            self._last_entry_reason = reason
            # [Bug Fix] Initialize trail peak to entry price
            self._atr_trail_peak = price
            # GSD Phase 0b: Reset consecutive losses on new entry
            self.consecutive_losses = 0
            # GSD Phase 0d: Reset bar counter on new entry
            self._last_trade_ts = ts
            self._bars_since_trade = 0
            self._signals_generated += 1

        # ── [L3] Route through OrderManager if enabled ──
        if self._use_order_manager and self.order_mgr and signal in ("BUY", "SELL"):
            # Paper mode: submit order → wait for tick fill → callback → PaperTrader
            return self._submit_order_via_manager(signal, price, ts, lots,
                                                   stop_loss=stop_loss, reason=reason)
        elif self._use_order_manager and self.order_mgr and signal in ("EXIT", "PARTIAL_EXIT"):
            # Exits need immediate execution (no queue in paper mode)
            self.paper_fill_sim.process_tick(self._make_synthetic_tick(price, ts))
            # Fall through to direct PaperTrader call for exits

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
                    "reason": reason or ""})

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
                        "entry_diag": self._entry_features_futures})

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
                        f"[TMF] {signal} {lots} lots @ {price:.0f}",
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
            df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
            
            missing = [c for c in new_data_keys if c not in df.columns]
            if missing:
                self.logger.info(f"🛡️ Migrating indicator CSV: adding {missing}")
                for c in missing: df[c] = np.nan
                # Sort columns to keep a stable order
                df = df.reindex(columns=sorted(df.columns))
                df.to_csv(path, index=False)
            
            # Cache the column order for subsequent appends
            self._indicator_cols = sorted(df.columns)
            self._indicators_migrated = True
        except Exception as e:
            self.logger.error(f"Schema migration failed: {e}")

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
            self.logger.error(f"Fast-append failed: {e}")

    # ── Main strategy loop ──

    def _fetch_today_kbars(self):
        """從API獲取當天的1分鐘K棒資料（類似選擇權系統的方法）"""
        if self.dry_run or not self.api or not self.contract:
            console.print(f"[yellow][FuturesMonitor] Cannot fetch kbars: dry_run={self.dry_run}, api={self.api is not None}, contract={self.contract}[/yellow]")
            return None
        
        # 頻率限制：每2分鐘才調用一次API（更頻繁更新）
        now_ts = time.time()
        if hasattr(self, '_last_kbars_fetch_at') and now_ts - self._last_kbars_fetch_at < 120:
            return None
        
        try:
            # 獲取起始日期 (回溯 3 天以確保有足夠的歷史資料算指標)
            today = datetime.now()
            start_date = (today - timedelta(days=3)).strftime("%Y-%m-%d")
            if today.hour < 5:  # 凌晨5點前算前一天
                today = today - timedelta(days=1)
            date_str = today.strftime("%Y-%m-%d")
            
            # 使用api.kbars獲取1分鐘K棒
            console.print(f"[cyan][FuturesMonitor] Fetching kbars for contract={self.contract.code}, from {start_date} to {date_str}[/cyan]")
            bars = self.api.kbars(self.contract, start=start_date, end=date_str)
            self._last_kbars_fetch_at = now_ts
            
            # 轉換為DataFrame
            frame = pd.DataFrame({**bars})
            console.print(f"[green][FuturesMonitor] Successfully fetched {len(frame) if not frame.empty else 0} kbars[/green]")
            if frame.empty or "ts" not in frame.columns:
                return None
            
            # 處理時間戳
            frame["ts"] = pd.to_datetime(frame["ts"])
            frame = frame.set_index("ts")
            
            # 確保欄位名稱正確
            column_map = {
                "Open": "Open", "open": "Open",
                "High": "High", "high": "High",
                "Low": "Low", "low": "Low",
                "Close": "Close", "close": "Close",
                "Volume": "Volume", "volume": "Volume"
            }
            frame = frame.rename(columns=column_map)
            
            # 只保留需要的欄位
            required_cols = ["Open", "High", "Low", "Close", "Volume"]
            available_cols = [col for col in required_cols if col in frame.columns]
            
            if len(available_cols) < 4:  # 至少需要OHLC
                return None
            
            return frame[available_cols].sort_index()
            
        except Exception as e:
            console.print(f"[yellow][FuturesMonitor] Error fetching today kbars: {e}[/yellow]")
            return None
    def run(self):
        self._running = True
        mode = "dry-run" if self.dry_run else ("LIVE" if self.live_trading else "PAPER")
        
        # [Phase A] Immediate Position Recovery & Heartbeat Start
        from core.shioaji_session import set_system_status, SystemReadiness
        set_system_status(SystemReadiness.MONITORING)
        
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
                console.print(f"[red][FuturesMonitor] error: {e}[/red]")
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
        # 💡 GSD: Data Continuity - Generate virtual tick if volume is zero but bidask is updating
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

        # 市場時間檢查
        from core.date_utils import is_day_session, is_night_session
        now = datetime.now()
        is_day = is_day_session(now)
        is_night = is_night_session(now)

        # 在 dry_run 模式下跳過時間檢查，方便測試
        if not self.dry_run and not (is_day or is_night):
            return

        # [Bug Fix] Check data freshness and attempt reconnection
        if not self.dry_run:
            self._check_futures_contract_staleness()

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

        # 1. Fetch multi-timeframe data (使用選擇權系統的方法)
        processed = {}
        if not self.dry_run:
            # 首先嘗試從API獲取當天的1分鐘K棒資料
            df_1min = self._fetch_today_kbars()
            
            if df_1min is not None and len(df_1min) >= 1:
                # 從1分鐘K棒重採樣為5分鐘K棒
                df_5m = df_1min.resample("5min").agg({
                    "Open": "first",
                    "High": "max", 
                    "Low": "min",
                    "Close": "last",
                    "Volume": "sum"
                }).dropna()
                
                if len(df_5m) >= 2:
                    processed["5m"] = calculate_futures_squeeze(df_5m, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
                    
                    # 從1分鐘K棒重採樣為其他時間框架
                    for tf, rule in [("15m", "15min"), ("1h", "1h")]:
                        res = df_1min.resample(rule).agg({
                            "Open": "first",
                            "High": "max",
                            "Low": "min", 
                            "Close": "last",
                            "Volume": "sum"
                        }).dropna()
                        if len(res) >= 2:
                            processed[tf] = calculate_futures_squeeze(res, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
            
            # 如果API資料不足，嘗試使用tick累積的資料
            if "5m" not in processed:
                df_base = self._get_tick_bars_df()
                if len(df_base) >= 30:
                    processed["5m"] = calculate_futures_squeeze(df_base, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
                    
                    # Resample for higher timeframes
                    for tf, rule in [("15m", "15min"), ("1h", "1h")]:
                        res = df_base.resample(rule).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
                        if len(res) >= 20:
                            processed[tf] = calculate_futures_squeeze(res, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
            
            # 如果還是沒有資料，嘗試舊的get_kline方法作為fallback
            if "5m" not in processed:
                try:
                    df = self.client.get_kline(self.ticker, interval="5m")
                    if not df.empty:
                        processed["5m"] = calculate_futures_squeeze(df, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
                except Exception as e:
                    console.print(f"[yellow][FuturesMonitor] api.kbars failed: {e}[/yellow]")

        # 只要有 5m 數據，不論有沒有指標，都應該寫入
        if "5m" not in processed:
            # 最後一招：如果連 api 都沒有，用目前手上剛湊出的 current_bar 墊檔
            if self._current_bar["ts"] is not None and self._current_bar["open"] > 0:
                df_tmp = pd.DataFrame([self._current_bar]).set_index("ts")
                df_tmp.columns = ["Open", "High", "Low", "Close", "Volume"]
                # GSD: Always calculate indicators (will fill defaults if too short)
                processed["5m"] = calculate_futures_squeeze(df_tmp, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
            else:
                return

        df_5m = processed["5m"]
        
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

        # Cross-regime decision (TX macro + TMF local)
        try:
            tx_regime = "UNKNOWN"
            tmf_regime = "UNKNOWN"
            policy = None
            tx_bars_list = None
            # Try to fetch TX 5m kline if client supports it
            try:
                tx_df = None
                if hasattr(self.client, 'get_kline'):
                    tx_df = self.client.get_kline("TX", interval="5m")
                if tx_df is not None and not tx_df.empty:
                    tx_bars_list = [{
                        "close": float(r.get("Close", 0)),
                        "high": float(r.get("High", 0)),
                        "low": float(r.get("Low", 0)),
                    } for _, r in tx_df.tail(100).iterrows()]
            except Exception:
                tx_bars_list = None

            tmf_bars_list = bars_list if 'bars_list' in locals() else []

            if hasattr(self, 'tx_detector') and self.tx_detector is not None and tx_bars_list:
                tx_regime = self.tx_detector.detect(tx_bars_list)
            if hasattr(self, 'tmf_detector') and self.tmf_detector is not None:
                tmf_regime = self.tmf_detector.detect(tmf_bars_list)
            if hasattr(self, 'cross_engine') and self.cross_engine is not None:
                policy = self.cross_engine.decide(tx_regime, tmf_regime)
            else:
                policy = {"allow_trade": True, "orb_weight": 1.0, "vwap_weight": 1.0}

            self._last_bar_context.update({
                "tx_regime": tx_regime,
                "tmf_regime": tmf_regime,
                "cross_policy": policy,
            })
            console.print(f"[dim][CROSS] tx={tx_regime} tmf={tmf_regime} allow={policy.get('allow_trade', False)} orb_w={policy.get('orb_weight', 0):.2f} vwap_w={policy.get('vwap_weight', 0):.2f}[/dim]")

            if not policy.get('allow_trade', False):
                console.print(f"[yellow]🔒 CrossPolicy: trading disabled by tx={tx_regime} tmf={tmf_regime}[/yellow]")
                score = 0.0
            else:
                mult = max(0.5, min(1.3, 0.6 * policy.get('orb_weight', 1.0) + 0.4 * policy.get('vwap_weight', 1.0)))
                score = float(score) * mult
        except Exception as e:
            console.print(f"[yellow]⚠️ Cross-regime integration failed: {e}[/yellow]")

        # [GSD 4.13] Trading Readiness Unlock: only allow trading if we have enough bars for indicators
        if not self.is_trading_ready and len(df_5m) >= self.STRATEGY.get("length", 20):
            self.is_trading_ready = True
            from core.shioaji_session import set_system_status, SystemReadiness
            set_system_status(SystemReadiness.TRADING)
            console.print(f"[bold green]🔥 [FuturesMonitor] Trading READY: {len(df_5m)} bars loaded.[/bold green]")
        
        # ── GSD: Ensure trading_day is always present before any downstream usage ──
        if "trading_day" not in df_5m.columns or df_5m["trading_day"].iloc[-1] is None or pd.isna(df_5m["trading_day"].iloc[-1]):
            from core.date_utils import get_trading_day
            df_5m["trading_day"] = get_trading_day(df_5m.index)
            
        last_5m = df_5m.iloc[-1]
        
        # fallback for MTF
        df_15m = processed.get("15m", df_5m)
        if "trading_day" not in df_15m.columns:
            df_15m["trading_day"] = df_5m["trading_day"].reindex(df_15m.index, method='ffill')
        last_15m = df_15m.iloc[-1]
        
        # 指標預設值
        score = 0.0
        regime = "NORMAL"
        
        # 只有在數據充足時才算 MTF Score
        if "15m" in processed:
            score_data = calculate_mtf_alignment(processed, weights=self.STRATEGY.get("weights", {"5m": 0.4, "15m": 0.4, "1h": 0.2}))
            score = score_data["score"]
            regime = "STRONG" if last_5m.get("opening_bullish") else ("WEAK" if last_5m.get("opening_bearish") else "NORMAL")

        last_price = last_5m["Close"]
        vwap = last_5m.get("vwap", last_price)
        timestamp = last_5m.name

        # GSD Phase 0b: Determine session type per bar
        hhmm = int(timestamp.strftime("%H%M")) if hasattr(timestamp, "strftime") else int(datetime.now().strftime("%H%M"))
        self.session_type = "night" if (hhmm >= 1500 or hhmm < 500) else "day"
        
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
            hhmm = int(datetime.now().strftime("%H%M"))
            is_night_session = hhmm >= 1500 or hhmm < 500
            target_close = "13:30" if not is_night_session else "05:00"
            close_dt = datetime.strptime(target_close, "%H:%M").replace(
                year=datetime.now().year, month=datetime.now().month, day=datetime.now().day
            )
            if is_night_session and hhmm >= 1500:
                from datetime import timedelta
                close_dt += timedelta(days=1)
            
            time_to_close = max(0, (close_dt - datetime.now()).total_seconds() / 60)
            
            market = {
                "price": last_price,
                "atr": float(last_5m.get("atr", 50)),
                "time_to_close_mins": time_to_close
            }
            
            exit_triggered, exit_reason = should_exit(trade_state, context, market)
            
            if exit_triggered:
                self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason=exit_reason)
                return
            
            # ── Legacy/Safety Fallbacks ──
            # VWAP Exit (Secondary check)
            if not exit_triggered:
                _is_night = is_night_session
                if _is_night:
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
        # TAIFEX TMF trading hours:
        #   Day:  08:45 - 13:45
        #   Night: 15:00 - 05:00 (next day)
        # Closed: 13:45-15:00 (lunch), 05:00-08:45 (early morning)
        hhmm = int(timestamp.strftime("%H%M")) if hasattr(timestamp, "strftime") else int(datetime.now().strftime("%H%M"))
        market_open = (845 <= hhmm <= 1345) or (hhmm >= 1500) or (hhmm < 500)
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
            # [Bug fix] ATR 合理性上限：TMF 5m ATR 通常 30-150 點
            atr_cap = 300
            if atr_val > atr_cap:
                atr_val = atr_cap
            if atr_val > 0:
                stop_loss_pts = atr_val * self.ATR_MULT

        # ── 進場品質過濾 ──
        min_score = self.STRATEGY.get("entry_score", 21)
        vol = last_5m.get("Volume", 0)
        avg_vol = df_5m["Volume"].rolling(20).mean().iloc[-1] if len(df_5m) >= 20 else 0

        # 夜盤成交量門檻降低（夜盤 TMF 量通常只有日盤 3-10%）
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
        # 1. Get active strategy from registry
        active_name = self.STRATEGY.get("active_strategy", "counter_vwap")
        strategy = self._registry.get(active_name)

        # 2. Fallback to old hardcoded logic if plugin not found (Migration safety)
        if strategy is None:
            console.print(f"[yellow]⚠️ Strategy plugin '{active_name}' not found in registry.[/yellow]")
            self._audit_signal("NO_ENTRY", "", score, "plugin_not_found", f"active_strategy={active_name}")
            return  # Don't crash — just skip this bar

        # ── GSD: Market Regime Detection (Wave 19 Integration) ───────
        from core.market_regime import classify_regime
        # We use the current window of bars to classify the regime
        current_regime = classify_regime(df_5m)
        
        # 3. Build immutable StrategyContext (SDD Rule 1: Read-only view)
        ctx = StrategyContext(
            market=MarketData(
                last_bar=last_5m.to_dict(),
                df_5m=df_5m,
                df_15m=df_15m,
                timestamp=str(timestamp),
                session=int(last_5m.get("session", 0)),
                regime=current_regime, # GSD: Pass live regime
            ),
            position=PositionView(
                size=self.trader.position,
                entry_price=self.trader.entry_price,
                current_stop_loss=getattr(self.trader, "current_stop_loss", None),
                unrealized_pnl=getattr(self.trader, "unrealized_pnl", 0),
                has_tp1_hit=self.has_tp1_hit,
            ),
            config=self.cfg,  # Pass full config; strategy picks what it needs
            bar_counter=self._bar_counter,
        )

        # 3.5. Initialize strategy once (when first loaded or strategy changes)
        if not hasattr(self, '_active_strategy_name') or self._active_strategy_name != active_name:
            strategy.init(ctx)
            self._active_strategy_name = active_name

        # 4. Execute strategy
        signal = strategy.on_bar(ctx)

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
                "regime": str(regime),
                "vwap_dist": abs(last_price - vwap),
                "volatility": float(last_5m.get("atr", 50)),
                "price": last_price,
                "side": "LONG" if signal.action == "BUY" else "SHORT",
                "breakout_strength": float(last_5m.get("breakout_strength", 0)),
                "volume_spike": float(last_5m.get("volume_spike", 1.0)),
                "trend_strength_raw": float(last_5m.get("trend_strength_raw", 0))
            }
            
            edge_res = edge_model.evaluate(abs(score), edge_context, active_name)
            if not edge_res["has_edge"]:
                self._audit_signal("ENTRY_BLOCKED", signal.action, score, "low_edge", edge_res["reason"])
                if self._bar_counter % 5 == 0:
                    console.print(f"[bold yellow]🛡️ Decision Intelligence: {active_name} Blocked - {edge_res['reason']}[/bold yellow]")
                return
            
            # [GSD Upgrade] Apply Dynamic Position Scaling
            signal.quantity = max(1, round(lots * edge_res["pos_scale"]))
            signal.reason = f"{signal.reason} ({edge_res['rank']})"
            if edge_res["pos_scale"] != 1.0:
                console.print(f"[bold cyan]⚖️ Position Scaled: {edge_res['rank']} (x{edge_res['pos_scale']}) -> {signal.quantity} lots[/bold cyan]")
            
            # Update lots for further logic
            lots = signal.quantity

        # 4.5 Fallback: try Spring/Upthrust if registry strategy has no signal
        if signal is None:
            from strategies.futures.elite_strategies import strategy_spring_upthrust
            spring_signal = strategy_spring_upthrust({
                "last_5m": last_5m, "df_5m": df_5m,
                "score": score, "stop_loss_pts": stop_loss_pts,
            }, self.cfg)
            if spring_signal:
                # GSD P4.4: Validate Spring signal (same as registry strategy)
                from core.signal import Signal
                spring_obj = Signal(
                    action=spring_signal["action"],
                    reason=spring_signal.get("reason", "SPRING"),
                    stop_loss=spring_signal.get("stop_loss", 0),
                )
                is_valid, msg = spring_obj.validate()
                if not is_valid:
                    console.print(f"[red]❌ Invalid Spring signal: {msg}[/red]")
                    return

                atr_val = last_5m.get("atr", 0)
                if atr_val > 300: atr_val = 300
                spring_sl = spring_signal.get("stop_loss", atr_val * 2.0 if atr_val > 0 else 60)
                lots = self.MGMT.get("lots_per_trade", 1)
                action = spring_signal["action"]
                if self.MGMT.get(f"allow_{'long' if action == 'BUY' else 'short'}", True):
                    console.print(f"[bold cyan]🌊 SPRING/UPTHRUST {action} SL={spring_sl:.1f}[/bold cyan]")
                    self._execute_trade(action, last_price, timestamp, lots,
                                        stop_loss=spring_sl, reason=spring_signal["reason"])
            return

        # 5. Validate Signal (Defensive Programming)
        is_valid, msg = signal.validate()
        if not is_valid:
            console.print(f"[red]❌ Invalid signal from {active_name}: {msg}[/red]")
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
