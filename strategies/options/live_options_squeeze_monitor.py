import time
import datetime
import argparse
import pandas as pd
import yaml
import shioaji as sj
from shioaji import TickFOPv1, Exchange
from collections import deque
import sys
import os
import threading
import logging
import io
import uuid
from pathlib import Path
from types import SimpleNamespace
from rich.console import Console

# 依序匯入策略所需組件
from options_engine.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment
from core.date_utils import get_session_date_str
from options_engine.engine.broker_adapter import ShioajiBrokerAdapter
from options_engine.engine.backtest_engine import (
    classify_exit_reason,
    should_exit_position,
    should_take_partial_profit,
    should_exit_by_time_constraints,
)
from options_engine.engine.backtest_engine import resolve_option_strike
from options_engine.engine.options_strategy import get_mode_profile, get_score_floor, get_stop_loss_pct, get_hard_stop_pct, get_strategy_weights, infer_mid_trend, resolve_entry_side
from core.bar_utils import (
    attach_bar_metadata,
    build_canonical_bar_frames,
    build_preferred_canonical_bar_frames,
    canonicalize_ohlcv,
    fill_small_ohlcv_gaps,
    resample_ohlcv,
    validate_ohlcv_bars,
)
from core.options_snapshot import OPTION_SNAPSHOT_COLUMNS, build_options_snapshot_row
from core.order_lifecycle_audit import (
    count_option_ledger_order_events,
    read_orders_file,
    rebuild_options_orders_from_ledger,
    write_orders_file,
)

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")

import login.shioaji_login as shioaji_login
from options_engine.engine.broker_adapter import ShioajiBrokerAdapter
from options_engine.engine.broker_adapter import MockBrokerAdapter # Support mock if needed

try:
    from strategies.futures.squeeze_futures.report.notifier import send_email_notification as _notify
except ImportError:
    _notify = None

# Structured notification system (core/notification/)
try:
    from core.notification.notifier import notify_trade_event as _notify_trade_event
    from core.notification.formatters.options_formatter import (
        OptionsPositionState,
        compute_unrealized_pnl_from_monitor,
    )
    _has_notification_system = True
except ImportError:
    _has_notification_system = False

# Fallback: legacy email formatter (strategies/options/email_formatter.py)
try:
    from strategies.options.email_formatter import (
        TradeEvent,
        compute_unrealized_pnl,
    )
    _has_legacy_formatter = True
except ImportError:
    _has_legacy_formatter = False

console = Console()
logger = logging.getLogger(__name__)

# 🚀 確保優先使用當前專案的 src 目錄，避免讀取到其他專案的舊版本
ROOT = Path(__file__).resolve().parent
CURRENT_SRC = str(ROOT / "src")
if CURRENT_SRC not in sys.path:
    sys.path.insert(0, CURRENT_SRC)

try:
    from options_engine.engine.greeks import black_scholes, calculate_dte, find_implied_volatility
except ImportError:
    # 備援路徑處理
    sys.path.append(os.path.join(os.path.dirname(__file__), "src"))
    from options_engine.engine.greeks import black_scholes, calculate_dte, find_implied_volatility

# QuantLib pricing engine (if configured)
_use_quantlib = False
try:
    from options_engine.engine.greeks_ql import (
        black_scholes as ql_black_scholes,
        calculate_dte as ql_calculate_dte,
        find_implied_volatility as ql_find_implied_volatility,
    )
    _use_quantlib = True
    console.print("[bold green][pricing] QuantLib engine available[/bold green]")
except ImportError:
    console.print("[dim][pricing] QuantLib not available, using py_vollib[/dim]")

console.print(f"[bold green][debug][/bold green] calculate_futures_squeeze imported from: [cyan]{calculate_futures_squeeze.__globals__.get('__file__')}[/cyan]")


class MockTrade:
    def __init__(self, contract, action, price, quantity, status="Filled"):
        self.contract = contract
        self.order = SimpleNamespace(action=action, price=price, quantity=quantity)
        self.status = SimpleNamespace(status=status)


class MockBrokerAdapter:
    def __init__(self, execution_cfg=None):
        self.execution_cfg = execution_cfg or {}
        self.aggressive_ticks = self.execution_cfg.get("aggressive_ticks", 0)
        self.tick_size = float(self.execution_cfg.get("tick_size", 1.0))

    def aggressive_entry_price(self, ask_price):
        return max(0.0, float(ask_price) + (self.aggressive_ticks * self.tick_size))

    def aggressive_exit_price(self, bid_price):
        return max(self.tick_size, float(bid_price) - (self.aggressive_ticks * self.tick_size))

    def place_entry_order(self, contract, quantity):
        return MockTrade(contract, "Buy", self.aggressive_entry_price(getattr(contract, "ask_price", 0.0) or 0.0), quantity)

    def place_exit_order(self, contract, quantity, bid_price=None):
        price = bid_price if bid_price is not None else (getattr(contract, "bid_price", 0.0) or 0.0)
        return MockTrade(contract, "Sell", self.aggressive_exit_price(price), quantity)

    def describe_trade(self, trade):
        return {
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": getattr(trade.status, "status", None),
            "order": f"{trade.order.action}@{trade.order.price}x{trade.order.quantity}",
            "contract": getattr(getattr(trade, "contract", None), "code", None),
        }

    def refresh_status(self, account=None):
        return None

    def cancel_order(self, trade):
        return None


class ShioajiOptionsSmartMonitor:
    def __init__(self, dry_run=False, run_once=False, replay_path=None, dry_run_live_orders=False):
        self.full_cfg = self.load_config()
        self.dry_run = dry_run
        self.run_once = run_once
        self.dry_run_live_orders = dry_run_live_orders
        self.replay_path = Path(replay_path).expanduser().resolve() if replay_path else None
        self.mode = self.full_cfg['active_mode']
        self.live_trading = bool(self.full_cfg.get("live_trading", False))
        if self.dry_run and not self.dry_run_live_orders:
            self.live_trading = False
        if self.dry_run_live_orders:
            self.live_trading = True
        self.m_cfg = get_mode_profile(self.full_cfg, self.mode)
        self.exit_opt = self.full_cfg['exit_optimization']
        self.strategy_cfg = self.full_cfg['strategy']
        self.weights = get_strategy_weights(self.full_cfg)
        self.entry_score = self.strategy_cfg['entry_score']
        self.score_floor = get_score_floor(self.full_cfg)
        self.stop_loss_pct = get_stop_loss_pct(self.full_cfg)
        self.hard_stop_pct = get_hard_stop_pct(self.full_cfg)
        self.max_holding_days = self.full_cfg.get("risk_mgmt", {}).get("max_holding_days")
        self.min_dte_to_exit = self.full_cfg.get("risk_mgmt", {}).get("min_dte_to_exit")
        self.execution_cfg = self.full_cfg.get("execution", {})
        self.pricing_cfg = self.full_cfg.get("pricing", {})
        self.max_spread_pct = self.execution_cfg.get("max_spread_pct", 0.05)
        self.base_lots = int(self.full_cfg.get("risk_mgmt", {}).get("lots_per_trade", 2))
        self.paper_lots = self.base_lots
        self.max_positions = self.full_cfg.get("risk_mgmt", {}).get("max_positions", self.base_lots)
        self.fallback_underlying_price = float(self.strategy_cfg.get("fallback_underlying_price", 23000))
        self.monthly_delivery_min_days = int(self.strategy_cfg.get("monthly_delivery_min_days", 7))
        self.strike_rounding = int(self.pricing_cfg.get("strike_rounding", 100))
        self.risk_free_rate = float(self.pricing_cfg.get("risk_free_rate", 0.02))
        # Select pricing engine
        self._pricing_model = self.pricing_cfg.get("pricing_model", "black_scholes")
        if self._pricing_model == "quantlib" and _use_quantlib:
            self._bs = ql_black_scholes
            self._iv = ql_find_implied_volatility
            self._dte = ql_calculate_dte
            console.print("[bold green][pricing] Using QuantLib engine[/bold green]")
        else:
            self._bs = black_scholes
            self._iv = find_implied_volatility
            self._dte = calculate_dte
            console.print(f"[dim][pricing] Using py_vollib ({self._pricing_model})[/dim]")
        self.eod_panic_time = self._parse_hhmm(self.exit_opt.get("eod_panic_time", "13:30"))
        self.eod_passive_window_mins = int(self.exit_opt.get("eod_passive_window_mins", 20))
        
        # GSD: New risk management parameters
        self.risk_mgmt = self.full_cfg.get("risk_mgmt", {})
        self.entry_premium_limit = float(self.risk_mgmt.get("entry_premium_limit", 250))
        self.opening_grace_mins = int(self.risk_mgmt.get("opening_grace_mins", 5))
        
        # [Fix] Parameterized fees (GSD enhancement)
        self.broker_fee_per_side = float(self.execution_cfg.get("broker_fee_per_side", 20.0))
        self.exchange_fee_per_side = float(self.execution_cfg.get("exchange_fee_per_side", 5.0))
        self.tax_rate = float(self.execution_cfg.get("tax_rate", 0.00002))
        self.shutdown_grace_mins = int(self.exit_opt.get("shutdown_grace_mins", 1))
        
        # [GSD 4.12] Passive Initialization: Defer API and heavy objects
        self.api = None 
        self.broker = None
        self.order_mgr = None
        self._use_order_manager = False 
        
        # [Vertical Spread v1] Feature flag: convert single-leg CALL/PUT to debit vertical spreads
        self._enable_vertical_spread = bool(self.full_cfg.get("vertical_spread", {}).get("enabled", False))
        self._spread_width = int(self.full_cfg.get("vertical_spread", {}).get("width", 100))
        self._current_spread = None  # populated by enter_spread_paper_position
        self._spread_holding_bars = 0

        self.market_data = {"MTX": {"close": 0.0, "bid": 0.0, "ask": 0.0}, "C": {"close": 0.0, "bid": 0.0, "ask": 0.0}, "P": {"close": 0.0, "bid": 0.0, "ask": 0.0}}
        # [Wave 2 optimization] Use deque for O(1) append/trim instead of DataFrame.loc + slicing
        self._mtx_tick_bars_deque = deque(maxlen=300)
        self._mtx_tick_bars_cache = None  # Cached DF for indicator calculations
        self._current_mtx_bar = {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ts": None}
        self._last_mtx_bar_ts = int(time.time() / 300) * 300
        self.active_contracts = {}
        self.lock = threading.Lock()
        self.last_tick_at = time.time()  # [GSD Fix] Initialize to NOW, not 0 — avoids false staleness on restart
        self.position, self.active_side, self.entry_price, self.has_tp1_hit, self.stop_loss_price = 0, None, 0.0, False, 0.0
        self.entry_mtx_price = 0.0
        self.entry_time = None
        self.peak_premium = 0.0
        self.cooldown_until = 0
        self.cooldown_bars = int(self.strategy_cfg.get("cooldown_bars", 0))
        self.last_signal = None
        self.trailing_stop_pct = float(self.strategy_cfg.get("trailing_stop_pct", 0))
        
        # ThetaGang (sell premium) integration
        self._theta_gang = None
        self._theta_cfg = self.full_cfg.get("theta_gang", {})
        
        # [GSD Fix] 提高 Theta 交易冷卻優先級
        if self._theta_cfg.get("enabled", False):
            theta_cd = int(self._theta_cfg.get("cooldown_bars", 0))
            if theta_cd > self.cooldown_bars:
                self.cooldown_bars = theta_cd
                console.print(f"[cyan][ThetaGang] Using extended cooldown: {self.cooldown_bars} bars[/cyan]")
            
            try:
                from theta_gang import ThetaGangManager
                self._theta_gang = ThetaGangManager(self.full_cfg, self._bs, self.strike_rounding)
                console.print(f"[bold cyan][ThetaGang] {self._theta_gang.strategy} enabled (auto_regime={self._theta_cfg.get('auto_regime', True)})[/bold cyan]")
                if self.live_trading:
                    if self._theta_gang.is_live_combo_strategy_supported():
                        console.print("[cyan][ThetaGang] Live combo execution enabled for 2-leg vertical spreads only; local theta state will stay pending until broker fills reconcile.[/cyan]")
                    else:
                        console.print(f"[yellow][ThetaGang] Live theta strategy {self._theta_gang.strategy} is unsupported tonight; runtime will block live submits without paper fallback.[/yellow]")
            except Exception as e:
                console.print(f"[yellow][ThetaGang] init failed: {e}[/yellow]")
        self._theta_bars_held = 0
        self._theta_release_confirm_count = 0
        self._theta_release_last_bar_ts = None
        self.last_status_print_at = None
        self.last_kbars_fetch_at = 0.0
        self.latest_score = 0.0
        self.latest_iv = 0.25
        self.latest_mid_trend = ""
        self.loop_sleep_secs = 60
        self.status_print_secs = 300
        self._bar_counter = 0  # [GSD Fix] Counter for logging throttling
        self._exit_in_progress = False  # [Rule 3] Reentrancy guard for exit paths
        # 2026-05-26 Hermes Agent: tick-level exit evaluator (60s gap fix)
        # holds intent set by _option_exit_on_tick, consumed by main loop
        self._pending_exit_request: dict | None = None
        # 2026-05-26 Hermes Agent: Options Watchdog (三層防禦)
        self._watchdog_hi_period = 10.0
        self._watchdog_lo_period = 30.0
        self._watchdog_last_hi = 0.0
        self._watchdog_last_lo = 0.0
        self._exit_start_time = 0.0
        self._watchdog_state = "NORMAL"
        self.is_monitoring_ready = True # [GSD 4.13] Phase A Ready
        self.is_trading_ready = False   # [GSD 4.13] Phase B Ready
        self.pending_entry = None
        self.pending_exit_qty = 0
        self.pending_exit_reason = None
        self.pending_exit_trade = None
        self.pending_theta_combo = None
        self.order_timeout_secs = int(self.execution_cfg.get("order_timeout_secs", 30))
        self.max_order_retries = int(self.execution_cfg.get("max_order_retries", 1))
        self.replay_bars = self._load_replay_bars(self.replay_path) if self.replay_path else None
        self.replay_cursor = max(0, self.strategy_cfg.get("length", 20) + 5) if self.replay_bars is not None else None
        self.replay_stats = {"signals": 0, "directional_signals": 0, "entries": 0, "exits": 0, "tp1_hits": 0, "blocked_entries": 0, "last_summary_at": 0}
        self._seen_fill_ordnos = set()  # Dedup for live FDeal callbacks
        self._seen_fill_identities = set()

        # ── [L3] Order Lifecycle Manager ──
        # [GSD Fix] OrderManager initialization moved to run() to ensure broker is available
        # and order recovery happens before callbacks are wired
        cfg = self.load_config()
        self._use_order_manager = cfg.get("monitoring", {}).get("use_order_manager", False)
        self.order_mgr = None

        # 設定日誌路徑
        self._update_log_paths()
        if self.api is not None and hasattr(self.api, "set_order_callback"):
            self.api.set_order_callback(self.on_order_event)

    def _update_log_paths(self):
        log_sub_dir = "live_trading" if self.live_trading else "paper_trading"
        # Use cwd-based path (main.py runs from tw-trading-unified root)
        log_base = Path(os.getcwd()) / "strategies" / "options" / "logs" / log_sub_dir
        try:
            if log_base.exists() and not log_base.is_dir():
                log_base.unlink()
            log_base.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            if "File exists" not in str(e):
                console.print(f"[yellow]Warning creating log path: {e}[/yellow]")
        
        now = datetime.datetime.now()
        date_str = get_session_date_str(now)
        self.indicator_log_path = log_base / f"OPTIONS_{date_str}_indicators.csv"
        self.ledger_path = log_base / "options_trade_ledger.csv"

        logger.info(
            "TRADE_LEDGER_PATH mode=%s live_trading=%s dry_run=%s path=%s",
            "LIVE" if self.live_trading else "PAPER",
            self.live_trading,
            getattr(self, "dry_run", None),
            self.ledger_path.resolve(),
        )

    def load_config(self):
        # BUG FIX 2026-04-14: Read from project root config (same file dashboard writes to)
        # Was: Path(__file__).parent / "config" / "options_strategy.yaml"
        project_root = Path(__file__).parent.parent.parent
        path = project_root / "config" / "options_strategy.yaml"
        with open(path, 'r') as f:
            # Load strategy configuration
            return yaml.safe_load(f)

    @staticmethod
    def _parse_hhmm(value):
        hour, minute = str(value).split(":")
        return int(hour), int(minute)

    def _minutes_since_midnight(self, current_time):
        return current_time.hour * 60 + current_time.minute

    def _current_strategy_time(self):
        if self.dry_run and self.replay_bars is not None and self.replay_cursor is not None and self.replay_cursor > 0:
            index_pos = min(self.replay_cursor - 1, len(self.replay_bars) - 1)
            return self.replay_bars.index[index_pos].to_pydatetime()
        return datetime.datetime.now()

    def _is_market_open(self, current_time):
        """檢查市場是否開盤 (支援日盤 + 夜盤) - Wave 1 unified logic"""
        from core.date_utils import is_day_session, is_night_session

        if is_day_session(current_time):
            return True, "day"
        if is_night_session(current_time):
            return True, "night"

        return False, "closed"
    def _eod_state(self, current_time):
        # 夜盤用 force_close 時間，日盤用 eod_panic_time
        h = current_time.hour
        is_night = h >= 15 or h < 5
        if is_night:
            night_cfg = self.full_cfg.get("night_trading", {})
            fc = night_cfg.get("force_close", "04:30")
            parts = str(fc).split(":")
            panic_h, panic_m = int(parts[0]), int(parts[1])
        else:
            panic_h, panic_m = self.eod_panic_time

        panic_minutes = panic_h * 60 + panic_m
        now_minutes = self._minutes_since_midnight(current_time)
        # 夜盤跨日：04:30 = 270 分鐘，15:00~23:59 不該觸發 panic
        if is_night and now_minutes > 300:  # 05:00 以後 = 日盤時段
            return {"is_passive": False, "is_panic": False, "shutdown_after": 0}

        passive_start = panic_minutes - self.eod_passive_window_mins
        return {
            "is_passive": passive_start <= now_minutes < panic_minutes,
            "is_panic": now_minutes >= panic_minutes and (not is_night or now_minutes < 300),
            "shutdown_after": panic_minutes + self.shutdown_grace_mins,
        }

    @staticmethod
    def _load_replay_bars(replay_path):
        if replay_path is None:
            return None
        frame = pd.read_csv(replay_path)
        lower_cols = {col.lower(): col for col in frame.columns}
        time_col = next((lower_cols[key] for key in ("datetime", "timestamp", "ts", "time") if key in lower_cols), None)
        if time_col is None:
            raise ValueError("Replay CSV must contain one of: datetime, timestamp, ts, time")

        rename_map = {}
        for canonical in ("open", "high", "low", "close", "volume"):
            if canonical in lower_cols:
                rename_map[lower_cols[canonical]] = canonical.capitalize()
        if set(rename_map.values()) != {"Open", "High", "Low", "Close", "Volume"}:
            raise ValueError("Replay CSV must contain columns: Open, High, Low, Close, Volume")

        frame[time_col] = pd.to_datetime(frame[time_col], errors="coerce")
        frame = frame.dropna(subset=[time_col]).rename(columns=rename_map).set_index(time_col).sort_index()
        frame = frame[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce").dropna()
        if frame.empty:
            raise ValueError("Replay CSV does not contain usable OHLCV rows")
        return frame


    def get_nearest_options(self, symbol="TXO"):
        """自動尋找最快到期的選擇權合約 (支援週選/月選)"""
        import datetime
        
        # 1. 抓取該商品所有履約價合約
        target_symbol = str(symbol)
        try:
            # [rshioaji 1.5.10+ Workaround] Use robust list helper to avoid C++ binding crash
            from core.broker.shioaji_compat import get_contracts_list
            all_contracts = get_contracts_list(self.api, "Options", target_symbol)
            if not all_contracts:
                 return None, []
        except (KeyError, TypeError, Exception) as e:
            console.print(f"[yellow]⚠️ Options [{target_symbol}] error: {e}[/yellow]")
            return None, []
        
        # 2. 取得今日日期
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        
        # 3. [GSD Settlement Fix] 過濾掉已過期合約，考慮結算時間 (13:30)
        now = datetime.datetime.now()
        valid_contracts = []
        
        for contract in all_contracts:
            try:
                # 解析合約到期日 - 支援多種格式
                contract_date = None
                delivery_date = contract.delivery_date
                
                # 嘗試不同格式
                for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]:
                    try:
                        contract_date = datetime.datetime.strptime(delivery_date, fmt).date()
                        break
                    except ValueError:
                        continue
                
                if contract_date is None:
                    console.print(f"[yellow]⚠️ Cannot parse contract date: {delivery_date}[/yellow]")
                    continue
                
                today_date = now.date()
                
                # 如果合約日期在未來，有效
                if contract_date > today_date:
                    valid_contracts.append(contract)
                # 如果合約日期在今天，檢查是否已過結算時間
                elif contract_date == today_date:
                    # 結算時間為 13:30
                    settlement_time = now.replace(hour=13, minute=30, second=0, microsecond=0)
                    if now < settlement_time:
                        valid_contracts.append(contract)
                # 如果合約日期在過去，已過期
                else:
                    continue
                    
            except Exception as e:
                console.print(f"[yellow]⚠️ Error parsing contract {contract.code}: {e}[/yellow]")
                continue
        
        # 4. 按到期日排序
        def get_contract_date(contract):
            """輔助函數：解析合約日期並返回可排序的日期對象"""
            delivery_date = contract.delivery_date
            for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]:
                try:
                    return datetime.datetime.strptime(delivery_date, fmt).date()
                except ValueError:
                    continue
            return datetime.date.max  # 如果無法解析，返回最大日期
        
        valid_contracts = sorted(valid_contracts, key=get_contract_date)
        
        if not valid_contracts:
            return None, []
        
        # 5. 取出最接近的到期日
        nearest_date_obj = get_contract_date(valid_contracts[0])
        nearest_date = nearest_date_obj.strftime("%Y-%m-%d")
        nearest_list = [c for c in valid_contracts if get_contract_date(c) == nearest_date_obj]
        
        return nearest_date, nearest_list
    
    def get_futures_contract_month(self, futures_monitor=None):
        """[GSD Settlement Fix] 從期貨監控器獲取當前期貨合約的月份
        
        Args:
            futures_monitor: FuturesMonitor 實例 (可選)
            
        Returns:
            str: 期貨合約月份，格式 "YYYY-MM" 或 None
        """
        try:
            # 方法1: 如果提供了期貨監控器，直接獲取其合約
            if futures_monitor and hasattr(futures_monitor, 'contract') and futures_monitor.contract:
                contract = futures_monitor.contract
                if hasattr(contract, 'delivery_date'):
                    # 解析期貨合約的交割日期
                    import datetime
                    try:
                        # Shioaji 期貨合約日期格式可能是 "YYYY/MM/DD"
                        if "/" in contract.delivery_date:
                            contract_date = datetime.datetime.strptime(contract.delivery_date, "%Y/%m/%d").date()
                        else:
                            contract_date = datetime.datetime.strptime(contract.delivery_date, "%Y-%m-%d").date()
                        
                        # 返回月份格式 "YYYY-MM"
                        return contract_date.strftime("%Y-%m")
                    except Exception as e:
                        console.print(f"[yellow]⚠️ Error parsing futures contract date: {e}[/yellow]")
            
            # 方法2: 嘗試從 API 獲取標的期貨合約 (MXF/TXF)
            if self.api:
                from core.broker.shioaji_compat import get_contracts_list
                for symbol in ["MXF", "TXF"]:
                    try:
                        tmf_list = get_contracts_list(self.api, "Futures", symbol)
                        if tmf_list:
                            # 過濾未過期合約並排序
                            import datetime
                            now = datetime.datetime.now()
                            valid_contracts = []
                            
                            for contract in tmf_list:
                                try:
                                    if "/" in contract.delivery_date:
                                        contract_date = datetime.datetime.strptime(contract.delivery_date, "%Y/%m/%d").date()
                                    else:
                                        contract_date = datetime.datetime.strptime(contract.delivery_date, "%Y-%m-%d").date()
                                    
                                    today = now.date()
                                    
                                    # 檢查合約是否有效（未過期）
                                    if contract_date > today:
                                        valid_contracts.append(contract)
                                    elif contract_date == today:
                                        # 檢查是否已過結算時間
                                        settlement_time = now.replace(hour=13, minute=30, second=0, microsecond=0)
                                        if now < settlement_time:
                                            valid_contracts.append(contract)
                                except Exception:
                                    continue
                            
                            if valid_contracts:
                                # 按交割日期排序
                                valid_contracts.sort(key=lambda c: c.delivery_date)
                                first_contract = valid_contracts[0]
                                
                                # 解析並返回月份
                                if "/" in first_contract.delivery_date:
                                    contract_date = datetime.datetime.strptime(first_contract.delivery_date, "%Y/%m/%d").date()
                                else:
                                    contract_date = datetime.datetime.strptime(first_contract.delivery_date, "%Y-%m-%d").date()
                                
                                console.print(f"[green]✅ 從 {symbol} 成功解析合約月份: {contract_date.strftime('%Y-%m')}[/green]")
                                return contract_date.strftime("%Y-%m")
                    except Exception as e:
                        console.print(f"[dim]Note: {symbol} check skipped: {e}[/dim]")
            
            # 方法3: 使用當前時間推斷
            import datetime
            now = datetime.datetime.now()
            return now.strftime("%Y-%m")
            
        except Exception as e:
            console.print(f"[yellow]⚠️ Error getting futures contract month: {e}[/yellow]")
            import datetime
            return datetime.datetime.now().strftime("%Y-%m")
    
    def get_options_by_month(self, symbol="TXO", target_month=None):
        """[GSD Settlement Fix] 根據目標月份獲取選擇權合約
        
        Args:
            symbol: 商品代號 (預設 TXO)
            target_month: 目標月份字串，格式 "YYYY-MM" 或 "YYYY/MM"
            
        Returns:
            tuple: (target_date, contracts_list) 或 (None, []) 如果找不到
        """
        import datetime
        
        # 1. 抓取該商品所有履約價合約
        target_symbol = str(symbol)
        try:
            # [rshioaji 1.5.10+ Workaround] Use robust list helper to avoid C++ binding crash
            from core.broker.shioaji_compat import get_contracts_list
            all_contracts = get_contracts_list(self.api, "Options", target_symbol)
        except (KeyError, TypeError, Exception) as e:
            console.print(f"[yellow]⚠️ Options contract [{target_symbol}] not loaded: {e}[/yellow]")
            return None, []
        
        # 2. 如果沒有指定目標月份，使用 get_nearest_options
        if not target_month:
            return self.get_nearest_options(symbol)
        
        # 3. 標準化目標月份格式
        try:
            # 嘗試解析不同格式
            if "/" in target_month:
                target_date_obj = datetime.datetime.strptime(target_month, "%Y/%m").date()
            else:
                target_date_obj = datetime.datetime.strptime(target_month, "%Y-%m").date()
            
            target_year_month = target_date_obj.strftime("%Y-%m")
        except Exception as e:
            console.print(f"[yellow]⚠️ Error parsing target month {target_month}: {e}[/yellow]")
            return self.get_nearest_options(symbol)
        
        # 4. 過濾符合目標月份的合約
        month_contracts = []
        for contract in all_contracts:
            try:
                # 解析合約到期日 - 支援多種格式
                contract_date = None
                delivery_date = contract.delivery_date
                
                # 嘗試不同格式
                for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]:
                    try:
                        contract_date = datetime.datetime.strptime(delivery_date, fmt).date()
                        break
                    except ValueError:
                        continue
                
                if contract_date is None:
                    console.print(f"[yellow]⚠️ Cannot parse contract date: {delivery_date}[/yellow]")
                    continue
                
                contract_year_month = contract_date.strftime("%Y-%m")
                
                if contract_year_month == target_year_month:
                    month_contracts.append(contract)
            except Exception as e:
                console.print(f"[yellow]⚠️ Error parsing contract {contract.code}: {e}[/yellow]")
                continue
        
        if not month_contracts:
            console.print(f"[yellow]⚠️ No options found for target month {target_year_month}[/yellow]")
            return self.get_nearest_options(symbol)
        
        # 5. 按到期日排序
        def get_contract_date(contract):
            """輔助函數：解析合約日期並返回可排序的日期對象"""
            delivery_date = contract.delivery_date
            for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]:
                try:
                    return datetime.datetime.strptime(delivery_date, fmt).date()
                except ValueError:
                    continue
            return datetime.date.max  # 如果無法解析，返回最大日期
        
        month_contracts = sorted(month_contracts, key=get_contract_date)
        
        # 6. 取出該月份的第一個到期日
        target_date_obj = get_contract_date(month_contracts[0])
        target_date = target_date_obj.strftime("%Y-%m-%d")
        target_list = [c for c in month_contracts if get_contract_date(c) == target_date_obj]
        
        return target_date, target_list
    
    def get_atm_contracts(self, contracts, spot_price, range_pts=200):
        """根據台指現價，過濾出正負 range_pts 點內的 ATM 合約"""
        atm_list = [
            c for c in contracts
            if abs(c.strike_price - spot_price) <= range_pts
        ]
        return atm_list


    def find_best_contracts(self, futures_monitor=None):
        """[GSD Settlement Fix] 尋找最佳選擇權合約，與期貨合約同步月份"""
        self.fm = futures_monitor  # Store reference for ThetaGate access
        if self.dry_run:
            return self._setup_dry_run_contracts()
        
        try:
            # [GSD Settlement Fix] 獲取期貨合約月份
            futures_month = self.get_futures_contract_month(futures_monitor)
            console.print(f"[cyan]📅 Futures contract month: {futures_month}[/cyan]")
            
            # 1. 確保合約資訊是最新的
            if self.api:
                # [rshioaji 1.5.9+] Use a safer check that doesn't trigger concurrent API call
                try:
                    # Check if property access works
                    txo = self.api.Contracts.Options["TXO"]
                    all_txo = list(txo) if txo else []
                except Exception:
                    all_txo = []
                    
                if not all_txo:
                    console.print("[yellow]🔍 正在檢查合約資訊 (Waiting for background fetch)...[/yellow]")
                    from core.broker.shioaji_compat import wait_for_contracts
                    # [Wave 3 Fix] Don't trigger fetch_contracts here as it causes "exclusive access lost"
                    # main.py already handles the initial sync. Just wait.
                    if not wait_for_contracts(self.api, "Options", "TXO", timeout=30):
                        console.print("[yellow]⚠️ Options contracts still not found after 30s wait[/yellow]")
            
            # 2. [GSD Settlement Fix] 根據期貨月份獲取選擇權合約
            nearest_date, contracts = self.get_options_by_month("TXO", futures_month)
            if not contracts:
                console.print("[red]❌ 錯誤：找不到任何有效的 TXO 選擇權合約。[/red]")
                return False
            # [Skew Integration] Store all month contracts for OTM strike resolution
            self._all_month_contracts = contracts
            
            console.print(f"[green]✅ 找到 {len(contracts)} 筆 {nearest_date} 到期合約 (同步期貨月份)[/green]")
            
            # 3. 獲取標的期貨並取得現價
            # Shioaji 中 小台指可能是 MTX 或 MXF，優先嘗試 MTX
            mtx_group = []
            from core.broker.shioaji_compat import get_contracts_list
            for symbol in ["MTX", "MXF"]:
                try:
                    mtx_group = get_contracts_list(self.api, "Futures", symbol)
                    if mtx_group: 
                        console.print(f"[dim]💡 成功在 {symbol} 分類下找到期貨合約[/dim]")
                        break
                except Exception:
                    continue
            
            if not mtx_group:
                console.print("[red]❌ 錯誤：找不到任何有效的台指期貨合約 (MTX/MXF)。[/red]")
                return False
            
            # [GSD Settlement Fix] 過濾與期貨月份相符的標準合約
            # 優先找月份相符的，如果找不到再找最近的 (fallback)
            all_valid_mtx = []
            for c in mtx_group:
                # 💡 GSD: Use safe getattr to avoid Shioaji error on broken objects
                c_code = getattr(c, "code", "")
                if c_code and len(c_code) in [5, 6, 7, 8, 9]:
                    all_valid_mtx.append(c)
            
            if not all_valid_mtx:
                all_valid_mtx = mtx_group # Absolute fallback

            all_valid_mtx = sorted(all_valid_mtx, key=lambda x: getattr(x, "delivery_date", ""))
            mtx_cons = [c for c in all_valid_mtx if str(getattr(c, "delivery_date", "")).replace("/", "-").startswith(futures_month)]
            
            if not mtx_cons:
                console.print(f"[yellow]⚠️  找不到月份 {futures_month} 的 MTX 合約，使用最近可用合約。[/yellow]")
                mtx_cons = all_valid_mtx
                
            if not mtx_cons:
                console.print("[red]❌ 錯誤：找不到任何有效的 MTX 期貨合約。[/red]")
                return False
                
            target_mtx = mtx_cons[0]
            snaps = self.api.snapshots([target_mtx])
            if not snaps:
                console.print(f"[yellow]⚠️ snapshots 回傳空，使用 fallback 價格: {self.fallback_underlying_price}[/yellow]")
                S = self.fallback_underlying_price
            else:
                snap = snaps[0]
                S = snap.close if snap.close > 0 else self.fallback_underlying_price
            
            # 初始化標的行情
            self.market_data["MTX"]["close"] = float(S)
            if snaps:
                self.market_data["MTX"]["bid"] = float(getattr(snaps[0], 'buy_price', S) or S)
                self.market_data["MTX"]["ask"] = float(getattr(snaps[0], 'sell_price', S) or S)
            else:
                self.market_data["MTX"]["bid"] = float(S)
                self.market_data["MTX"]["ask"] = float(S)
            
            # 4. 根據標的現價過濾 ATM 合約
            atm_strike = resolve_option_strike(S, self.strike_rounding)
            cons_at_strike = [c for c in contracts if abs(c.strike_price - atm_strike) < 1]
            
            # [Wave 3 Fix] Check for BOTH Call and Put presence
            calls = [c for c in cons_at_strike if "call" in str(getattr(c, "option_right", "")).lower()]
            puts = [c for c in cons_at_strike if "put" in str(getattr(c, "option_right", "")).lower()]

            if not calls or not puts:
                console.print(f"[yellow]⚠️  Strike {atm_strike} is missing {'Calls' if not calls else 'Puts'}. Searching nearby...[/yellow]")
                # 嘗試在附近範圍內尋找具備雙邊報價的合約
                atm_contracts = self.get_atm_contracts(contracts, S, range_pts=300)
                # 依據距離 S 的絕對值排序
                candidate_strikes = sorted(list(set([c.strike_price for c in atm_contracts])), key=lambda x: abs(x - S))
                
                found_pairing = False
                for candidate_strike in candidate_strikes:
                    c_at = [c for c in atm_contracts if abs(c.strike_price - candidate_strike) < 1]
                    c_calls = [c for c in c_at if "call" in str(getattr(c, "option_right", "")).lower()]
                    c_puts = [c for c in c_at if "put" in str(getattr(c, "option_right", "")).lower()]
                    if c_calls and c_puts:
                        atm_strike = candidate_strike
                        cons_at_strike = c_at
                        calls = c_calls
                        puts = c_puts
                        console.print(f"[green]✅ Found pairing at strike: {atm_strike}[/green]")
                        found_pairing = True
                        break
                
                if not found_pairing:
                    console.print(f"[red]❌ 錯誤：無法在附近範圍內找到成對的 Call/Put 合約。[/red]")
                    return False

            # 5. 鎖定監控合約 (MTX, Call, Put)
            self.active_contracts = {
                "MTX": target_mtx,
                "C": calls[0],
                "P": puts[0]
            }
            
            # 6. 同步初始選擇權行情
            try:
                opt_snaps = self.api.snapshots([self.active_contracts["C"], self.active_contracts["P"]])
                for i, side in enumerate(["C", "P"]):
                    s = opt_snaps[i]
                    self.market_data[side]["close"] = float(s.close if s.close > 0 else 0.0)
                    self.market_data[side]["bid"] = float(getattr(s, 'buy_price', s.close) or s.close)
                    self.market_data[side]["ask"] = float(getattr(s, 'sell_price', s.close) or s.close)
            except Exception as e:
                console.print(f"[yellow]⚠️  無法預同步選擇權行情：{e}[/yellow]")

            console.print(f"[bold cyan][MODE {self.mode}][/bold cyan] Monitoring {nearest_date} | ATM {atm_strike} | MTX: {target_mtx.code}")
            
            # [GSD Settlement Fix] Validate contracts haven't expired
            # On settlement day, contracts are valid until 13:30.
            now = datetime.datetime.now()
            today = now.date()
            settlement_time = now.replace(hour=13, minute=30, second=0, microsecond=0)
            
            for side, contract in [("C", self.active_contracts["C"]), ("P", self.active_contracts["P"])]:
                try:
                    if not hasattr(contract, 'delivery_date') or not contract.delivery_date:
                        continue
                    dd_str = contract.delivery_date
                    # Parse contract delivery date
                    parsed_dd = None
                    for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]:
                        try:
                            parsed_dd = datetime.datetime.strptime(dd_str, fmt).date()
                            break
                        except ValueError:
                            continue
                    
                    if parsed_dd is None:
                        continue
                        
                    if parsed_dd < today:
                        console.print(f"[red]🚫 Contract {contract.code} has already expired ({dd_str}). Rejecting.[/red]")
                        return False
                    elif parsed_dd == today:
                        if now >= settlement_time:
                            console.print(f"[red]🚫 Contract {contract.code} expired today at 13:30. Rejecting.[/red]")
                            return False
                        else:
                            console.print(f"[yellow]⚠️ Contract {contract.code} expires TODAY at 13:30. Proceeding with caution.[/yellow]")
                except Exception as e:
                    console.print(f"[dim]⚠️ Expiry check error for {side}: {e}, allowing contract[/dim]")
                    continue
            
            return True

        except Exception as e:
            console.print(f"[red]❌ find_best_contracts 發生異常：[/red] {e}")
            import traceback
            console.print(traceback.format_exc())
            return False

    def _check_options_contract_staleness(self):
        """[Phase 1 Fix] Check if options ticks are stale and attempt recovery."""
        if self.dry_run or not self.api:
            return
        
        secs_since_tick = time.time() - self.last_tick_at
        if secs_since_tick < 120:  # Less than 2 min, all good
            return
        
        console.print(f"[yellow]⚠️ Options data stale for {secs_since_tick/60:.1f} min, checking contracts...[/yellow]")
        
        # [GSD Settlement Fix] Check if current contracts have expired
        now = datetime.datetime.now()
        today = now.date()
        settlement_time = now.replace(hour=13, minute=30, second=0, microsecond=0)
        needs_refresh = False

        for side, contract in [("C", self.active_contracts.get("C")), ("P", self.active_contracts.get("P"))]:
            if not contract:
                needs_refresh = True
                break
            
            dd_str = getattr(contract, 'delivery_date', None)
            if dd_str and isinstance(dd_str, str):
                # Parse contract delivery date
                parsed_dd = None
                for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]:
                    try:
                        parsed_dd = datetime.datetime.strptime(dd_str, fmt).date()
                        break
                    except ValueError:
                        continue
                
                if parsed_dd:
                    if parsed_dd < today:
                        console.print(f"[yellow]⚠️ {side} contract {contract.code} has expired ({dd_str})[/yellow]")
                        needs_refresh = True
                    elif parsed_dd == today and now >= settlement_time:
                        console.print(f"[yellow]⚠️ {side} contract {contract.code} expired today at 13:30[/yellow]")
                        needs_refresh = True
        
        if needs_refresh:
            console.print("[bold yellow]🔄 Refreshing options contracts...[/bold yellow]")
            try:
                # Clear existing to trigger resolve in next loop or immediate
                for side in ["C", "P"]:
                    self.active_contracts[side] = None
                self.find_best_contracts()
                
                # Re-subscribe will happen in the next iteration via run() logic
                # or we can force it here
                self.last_tick_at = time.time() # Reset timer to prevent loop
            except Exception as e:
                console.print(f"[red]Refresh contracts error:[/red] {e}")
        else:
            # Contracts not expired, but no ticks - could be market lull
            # [GSD Fix] DO NOT re-subscribe — Shioaji C++ crashes on repeated subscribe/unsubscribe
            # Instead, just log and let the main.py sentinel handle process-level recovery
            console.print(f"[dim]⚠️ Options ticks quiet but contracts valid — letting sentinel handle if needed[/dim]")
            # Reset tick timestamp to avoid repeated warnings
            self.last_tick_at = time.time()

    def _save_options_bar(self):
        """[GSD Fix] Save options market data to CSV every loop iteration — mirrors futures _save_bar().
        Ensures data persists even when no signal is generated."""
        try:
            from pathlib import Path
            from core.date_utils import get_session, get_session_date_str
            import datetime as _dt

            now = _dt.datetime.now()
            date_str = get_session_date_str(now)
            log_base = Path("logs/market_data")
            log_base.mkdir(parents=True, exist_ok=True)
            csv_path = log_base / f"OPTIONS_{date_str}_indicators.csv"

            md = self.market_data
            mtx_close = float(md.get("MTX", {}).get("close", 0))
            c_close = float(md.get("C", {}).get("close", 0))
            p_close = float(md.get("P", {}).get("close", 0))
            c_bid = float(md.get("C", {}).get("bid", 0))
            c_ask = float(md.get("C", {}).get("ask", 0))
            p_bid = float(md.get("P", {}).get("bid", 0))
            p_ask = float(md.get("P", {}).get("ask", 0))
            c_code = getattr(self.active_contracts.get("C"), "code", "")
            p_code = getattr(self.active_contracts.get("P"), "code", "")
            mtx_code = getattr(self.active_contracts.get("MTX"), "code", "")

            row = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                "session": get_session(now),
                "mtx_code": mtx_code, "mtx_close": mtx_close,
                "call_code": c_code, "call_close": c_close, "call_bid": c_bid, "call_ask": c_ask,
                "put_code": p_code, "put_close": p_close, "put_bid": p_bid, "put_ask": p_ask,
                "position": self.position, "side": self.active_side or "",
                "entry_price": self.entry_price, "stop_loss": self.stop_loss_price,
                "score": self.latest_score, "trend": self.latest_mid_trend,
            }

            df_row = pd.DataFrame([row])
            if csv_path.exists():
                try:
                    df_existing = pd.read_csv(csv_path)
                    df_combined = pd.concat([df_existing, df_row], ignore_index=True)
                    df_combined.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
                    df_combined.to_csv(csv_path, index=False)
                except Exception:
                    df_row.to_csv(csv_path, mode="a", index=False, header=False)
            else:
                df_row.to_csv(csv_path, index=False, header=True)
        except Exception as e:
            # Never crash the main loop over saving
            pass

    def _resolve_dry_run_contract_spec(self, current_time=None, underlying_hint=None):
        current_time = current_time or (self.replay_bars.index[0].to_pydatetime() if self.replay_bars is not None else datetime.datetime.now())
        underlying_hint = float(underlying_hint if underlying_hint is not None else (self.replay_bars.iloc[0]["Close"] if self.replay_bars is not None else self.fallback_underlying_price))
        near_delivery = (current_time + datetime.timedelta(days=2)).strftime("%Y%m%d")
        monthly_delivery = (current_time + datetime.timedelta(days=max(self.monthly_delivery_min_days + 3, 14))).strftime("%Y%m%d")
        target_date = monthly_delivery if self.m_cfg['delivery_pref'] == 'monthly' else near_delivery
        atm_strike = resolve_option_strike(underlying_hint, self.strike_rounding)
        return target_date, atm_strike, underlying_hint

    def _setup_dry_run_contracts(self, current_time=None, underlying_hint=None):
        target_date, atm_strike, underlying_hint = self._resolve_dry_run_contract_spec(current_time=current_time, underlying_hint=underlying_hint)
        self.active_contracts = {
            "MTX": SimpleNamespace(code="MXFD999", delivery_date=target_date),
            "C": SimpleNamespace(code=f"TXO{target_date}C", delivery_date=target_date, strike_price=atm_strike, option_right="Call"),
            "P": SimpleNamespace(code=f"TXO{target_date}P", delivery_date=target_date, strike_price=atm_strike, option_right="Put"),
        }
        self.market_data["MTX"] = {"close": underlying_hint, "bid": underlying_hint - 1, "ask": underlying_hint + 1}
        self.market_data["C"] = {"close": 120.0, "bid": 119.0, "ask": 121.0}
        self.market_data["P"] = {"close": 118.0, "bid": 117.0, "ask": 119.0}
        console.print(
            f"[bold cyan][DRY RUN][/bold cyan] Mocking {target_date} | ATM {atm_strike} | "
            f"{'replay=' + self.replay_path.name if self.replay_path else f'fallback_underlying={self.fallback_underlying_price:.0f}'}"
        )
        return True

    def _refresh_dry_run_contracts(self, current_time, underlying_price):
        if self.position > 0 or self.pending_entry or self.pending_exit_qty > 0:
            return
        target_date, atm_strike, _ = self._resolve_dry_run_contract_spec(current_time=current_time, underlying_hint=underlying_price)
        current_call = self.active_contracts.get("C")
        if (
            current_call is not None
            and getattr(current_call, "delivery_date", None) == target_date
            and getattr(current_call, "strike_price", None) == atm_strike
        ):
            return
        self.active_contracts = {
            "MTX": SimpleNamespace(code="MXFD999", delivery_date=target_date),
            "C": SimpleNamespace(code=f"TXO{target_date}C", delivery_date=target_date, strike_price=atm_strike, option_right="Call"),
            "P": SimpleNamespace(code=f"TXO{target_date}P", delivery_date=target_date, strike_price=atm_strike, option_right="Put"),
        }

    def on_tick(self, exchange: Exchange, tick: TickFOPv1):
        self.last_tick_at = time.time()  # Sentinel: track tick freshness
        with self.lock:
            code = tick.code
            c_contract = self.active_contracts.get("C")
            p_contract = self.active_contracts.get("P")
            m_contract = self.active_contracts.get("MTX")
            
            key = None
            if c_contract and code == getattr(c_contract, "code", None):
                key = "C"
            elif p_contract and code == getattr(p_contract, "code", None):
                key = "P"
            elif m_contract and code == getattr(m_contract, "code", None):
                key = "MTX"
            
            if key:
                # Use normalizer to force float
                self._normalize_and_update_market_data(
                    key, 
                    bid=getattr(tick, 'bid_price', None), 
                    ask=getattr(tick, 'ask_price', None), 
                    close=tick.close
                )

            # Build 5m bars from MTX ticks
            if key == "MTX":
                price = float(tick.close)
                vol = int(getattr(tick, "volume", 1))
                # ... (rest of bar logic unchanged)
                
                # [Wave 1 optimization] Use integer time bucketing to avoid expensive pd.Timestamp().floor()
                # Only compute Timestamp when bar changes (every 5 minutes)
                tick_ts = pd.Timestamp(tick.datetime)
                ts_int = int(tick_ts.timestamp() / 300) * 300
                
                bar = self._current_mtx_bar
                if bar["ts"] is None or ts_int > self._last_mtx_bar_ts:
                    if bar["ts"] is not None and bar["open"] > 0:
                        # [Wave 2 optimization] Use deque for O(1) append/trim instead of DataFrame.loc + slicing
                        bar_dict = {
                            "open": bar["open"],
                            "high": bar["high"],
                            "low": bar["low"],
                            "close": bar["close"],
                            "volume": bar["volume"],
                            "ts": bar["ts"],
                        }
                        self._mtx_tick_bars_deque.append(bar_dict)
                        # Invalidate DF cache (will be rebuilt lazily on next indicator calc)
                        self._mtx_tick_bars_cache = None
                    # Convert to Timestamp only when bar changes
                    ts = pd.Timestamp(ts_int, unit='s')
                    bar["ts"] = ts
                    self._last_mtx_bar_ts = ts_int
                    bar["open"] = bar["high"] = bar["low"] = bar["close"] = price
                    bar["volume"] = vol
                elif ts_int == self._last_mtx_bar_ts:
                    bar["high"] = max(bar["high"], price)
                    bar["low"] = min(bar["low"], price)
                    bar["close"] = price
                    bar["volume"] += vol

                # 2026-05-26 Hermes Agent: tick-level exit evaluation for held option position
                # Only evaluate on the option tick that matches the position side (C or P)
                if key in ("C", "P") and key == self.active_side and self.position > 0:
                    self._option_exit_on_tick(tick)
            else:
                    return

    def _normalize_and_update_market_data(self, key, bid=None, ask=None, close=None):
        """Unified entry point for market data updates. Force float and detect Decimal pollution."""
        from decimal import Decimal
        
        target = self.market_data.get(key)
        if not target: return
        
        try:
            if bid is not None:
                if isinstance(bid, Decimal): self.replay_stats["decimal_detected_count"] = self.replay_stats.get("decimal_detected_count", 0) + 1
                target["bid"] = float(bid)
            if ask is not None:
                if isinstance(ask, Decimal): self.replay_stats["decimal_detected_count"] = self.replay_stats.get("decimal_detected_count", 0) + 1
                target["ask"] = float(ask)
            if close is not None:
                if isinstance(close, Decimal): self.replay_stats["decimal_detected_count"] = self.replay_stats.get("decimal_detected_count", 0) + 1
                target["close"] = float(close)
            
            # Auto-calculate Mid if possible
            if target["bid"] > 0 and target["ask"] > 0:
                target["close"] = (target["bid"] + target["ask"]) / 2.0
                
        except Exception as e:
            console.print(f"[red]❌ Data Normalization Error ({key}): {e}[/red]")

    def _failsafe_fallback_entry(self, exc):
        """
        Emergency entry logic if primary strategy loop crashes.
        Triggered only during high-confidence regimes to ensure we don't miss 'the big one'.
        """
        self.replay_stats["strategy_loop_error_count"] = self.replay_stats.get("strategy_loop_error_count", 0) + 1
        
        # 1. 取得最新指標 (從緩存或直接從 data_manager)
        try:
            bs_atr = getattr(self, "latest_bs_atr", 0.0)
            bear_bs_atr = getattr(self, "latest_bear_bs_atr", 0.0)
            score = self.latest_score
            
            # 2. 定義緊急門檻 (極其嚴格，避免亂進場)
            # BS > 0.4 ATR 代表絕對突破
            is_emergency_bull = bs_atr >= 0.4 and score >= 60
            is_emergency_bear = bear_bs_atr >= 0.4 and score <= -60
            
            if (is_emergency_bull or is_emergency_bear) and self.position == 0:
                side = "C" if is_emergency_bull else "P"
                reason = f"FAILSAFE_ENTRY: {side} (StrategyCrash: {exc})"
                console.print(f"[bold red]🆘 [FAILSAFE] Primary engine crashed but HIGH CONFIDENCE detected. Attempting emergency {side} entry...[/bold red]")
                
                # Create a minimal mock signal
                emergency_signal = {
                    "side": side,
                    "score": score,
                    "type": "FAILSAFE",
                    "price_mtx": self.market_data["MTX"]["close"]
                }
                
                if self._enable_vertical_spread:
                    self.enter_spread_paper_position(side, emergency_signal)
                else:
                    self.enter_paper_position(side, emergency_signal)
        except Exception as e:
            console.print(f"[red]❌ Failsafe Fallback failed: {e}[/red]")

    def on_bidask(self, exchange, bidask):
        """Update bid/ask from BidAsk callback — more frequent than Tick in off-hours."""
        self.last_tick_at = time.time()  # Sentinel: track data freshness
        with self.lock:
            code = bidask.code
            bid = bidask.bid_price[0] if hasattr(bidask.bid_price, '__getitem__') else bidask.bid_price
            ask = bidask.ask_price[0] if hasattr(bidask.ask_price, '__getitem__') else bidask.ask_price
            
            c_contract = self.active_contracts.get("C")
            p_contract = self.active_contracts.get("P")
            m_contract = self.active_contracts.get("MTX")
            key = None
            if c_contract and code == getattr(c_contract, "code", None):
                key = "C"
            elif p_contract and code == getattr(p_contract, "code", None):
                key = "P"
            elif m_contract and (code == getattr(m_contract, "code", None) or code.startswith("MXF")):
                key = "MTX"
            if not key:
                return
            
            # Use normalizer to force float and calculate mid
            self._normalize_and_update_market_data(key, bid=bid, ask=ask)

    def _save_orders_file_wrapper(self):
        """Export all orders to JSON for dashboard with unrealized PnL."""
        if not self.order_mgr:
            return
        try:
            import json
            from pathlib import Path
            all_orders = self.order_mgr.get_completed() + self.order_mgr.get_pending()
            export_data = []

            # Get current option price for unrealized PnL
            cur_price = None
            try:
                # Try to get current option premium from market data using active_side
                if self.active_side and self.active_side in self.market_data:
                    bid = self.market_data[self.active_side].get("bid", 0)
                    ask = self.market_data[self.active_side].get("ask", 0)
                    if bid > 0 and ask > 0:
                        cur_price = (bid + ask) / 2
            except Exception:
                pass

            for o in all_orders:
                d = o.to_dict()
                d["unrealized_pnl"] = None
                d["unrealized_pnl_pts"] = None
                d["current_price"] = cur_price if cur_price and cur_price > 0 else None

                if o.status in ("filled", "partial_filled") and self.position > 0:
                    entry = self.entry_price
                    if cur_price and cur_price > 0 and entry > 0:
                        # GSD Fix: Side-aware PnL
                        from core.order_management.order import OrderSide
                        if o.side == OrderSide.SELL:
                            # Short position: profit if price DROPS
                            pnl_pts = entry - cur_price
                        else:
                            # Long position: profit if price RISES
                            pnl_pts = cur_price - entry
                            
                        pnl_cash = pnl_pts * self.pricing_cfg.get("point_value", 50) * self.position
                        d["unrealized_pnl"] = round(pnl_cash, 0)
                        d["unrealized_pnl_pts"] = round(pnl_pts, 1)

                export_data.append(d)

            orders_file = self._options_orders_file_path()
            write_orders_file(orders_file, export_data)
        except Exception as e:
            print(f"⚠️ Failed to save options orders: {e}")

    def _options_orders_file_path(self, now=None):
        current_time = now or datetime.datetime.now()
        date_str = current_time.strftime("%Y%m%d")
        return Path(f"exports/trades/OPTIONS_{date_str}_orders.json")

    def audit_order_lifecycle_health_and_repair(self, timestamp=None):
        current_time = timestamp
        if isinstance(timestamp, pd.Timestamp):
            current_time = timestamp.to_pydatetime()
        if current_time is None:
            current_time = datetime.datetime.now()

        orders_file = self._options_orders_file_path(current_time)
        existing_orders = read_orders_file(orders_file)

        ledger_df = None
        if self.ledger_path.exists():
            try:
                ledger_df = pd.read_csv(self.ledger_path)
            except Exception as exc:
                return f"order_lifecycle_read_error:{type(exc).__name__}"

        ledger_events = count_option_ledger_order_events(ledger_df)
        if ledger_events == 0:
            return "order_lifecycle_idle"

        if len(existing_orders) >= ledger_events:
            return f"order_lifecycle_healthy orders={len(existing_orders)} ledger={ledger_events}"

        repairs = []
        if self.order_mgr and (self.order_mgr.get_completed() or self.order_mgr.get_pending()):
            self._save_orders_file_wrapper()
            repairs.append("export_from_order_mgr")
            existing_orders = read_orders_file(orders_file)

        if len(existing_orders) < ledger_events and ledger_df is not None:
            rebuilt_orders = rebuild_options_orders_from_ledger(ledger_df)
            if rebuilt_orders:
                write_orders_file(orders_file, rebuilt_orders)
                repairs.append("rebuild_from_ledger")
                existing_orders = rebuilt_orders

        if len(existing_orders) >= ledger_events:
            return f"order_lifecycle_repair_ok orders={len(existing_orders)} ledger={ledger_events} repairs={','.join(repairs)}"
        return f"order_lifecycle_repair_failed orders={len(existing_orders)} ledger={ledger_events}"

    def _wire_order_callbacks(self):
        """Wire OrderManager callbacks for options (export + logging)."""
        import json
        from core.order_management.order import OrderStatus

        def _on_status_change(event):
            self._save_orders_file_wrapper()

        def _on_fill_callback(event):
            console.print(f"[green]📦 Options Order FILLED: {event.side.value} {event.fill_qty} @ {event.fill_price:.0f}[/green]")
            self._save_orders_file_wrapper()

        def _on_cancel_callback(event):
            console.print(f"[yellow]🚫 Options Order CANCELLED: {event.order_id} ({event.reason})[/yellow]")
            self._save_orders_file_wrapper()

        def _on_reject_callback(event):
            console.print(f"[red]❌ Options Order REJECTED: {event.order_id} ({event.reason})[/red]")
            self._save_orders_file_wrapper()

        self.order_mgr.register_callback("on_status_change", _on_status_change)
        self.order_mgr.register_callback("on_fill", _on_fill_callback)
        self.order_mgr.register_callback("on_cancel", _on_cancel_callback)
        self.order_mgr.register_callback("on_reject", _on_reject_callback)
        
        # [GSD Fix] 啟動時立即存檔，確保 Dashboard 抓得到檔案 (包含已恢復的部位)
        self._save_orders_file_wrapper()

    def _resolve_entry_lots(self, pos_scale: float = 1.0) -> int:
        base_lots = max(1, int(getattr(self, "base_lots", self.paper_lots)))
        scaled_lots = max(1, round(base_lots * pos_scale))
        remaining_capacity = max(0, int(self.max_positions) - int(self.position))
        if remaining_capacity <= 0:
            return 0
        return min(scaled_lots, remaining_capacity)

    # ──────────────────────────────────────────────────────────────
    # Trade Ledger Durability — append_csv_row_durable + make_trade_id
    # Ensures every trade row is fsynced to disk before notification.
    # ──────────────────────────────────────────────────────────────
    @staticmethod
    def _make_trade_id() -> str:
        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S.%f")
        return f"trade_{ts}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _append_csv_row_durable(path: Path, row: dict) -> None:
        """Append one CSV row and force it to disk (flush + fsync).

        This is a durability-safe replacement for
            pd.DataFrame([row]).to_csv(path, mode='a', header=not exists)

        The old pattern could lose rows on abnormal process exit because
        pandas to_csv(..., mode='a') writes through a buffered stream
        without flushing to kernel buffers or disk.
        """
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)

        write_header = not path.exists() or path.stat().st_size == 0

        df_row = pd.DataFrame([row])
        buf = io.StringIO()
        df_row.to_csv(buf, index=False, header=write_header)

        with open(path, "a", encoding="utf-8", newline="") as f:
            f.write(buf.getvalue())
            f.flush()
            os.fsync(f.fileno())

        # Post-write integrity check
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError(f"Trade ledger write failed (empty/missing): {path}")

        # Verify last row has the expected trade_id
        ShioajiOptionsSmartMonitor._verify_last_trade_id(path, row.get("trade_id", ""))

    @staticmethod
    def _verify_last_trade_id(path: Path, expected_trade_id: str) -> None:
        """Read back last row and confirm trade_id matches."""
        try:
            last_row = pd.read_csv(path).iloc[-1]
            actual = str(last_row.get("trade_id", "")).strip()
            if actual != expected_trade_id:
                raise RuntimeError(
                    f"Trade ledger last-row trade_id mismatch: "
                    f"expected={expected_trade_id} actual={actual} path={path}"
                )
        except (IndexError, pd.errors.EmptyDataError) as e:
            raise RuntimeError(
                f"Trade ledger verify failed (empty/corrupt): {e} path={path}"
            )

    def log_trade(self, action, side, price, note="", quantity=None, entry_price_override=None):
        trade_id = self._make_trade_id()
        pnl = 0
        point_value = self.pricing_cfg.get("point_value", 50)
        # ── [PnL Fix] Use passed quantity; never fallback to self.position (already cleared) ──
        qty = quantity if quantity is not None else 1

        # ── [PnL Fix] Use explicit entry_price from snapshot override; never fallback to self.entry_price (may be mixed) ──
        entry_price_for_pnl = entry_price_override if entry_price_override is not None else self.entry_price

        # GSD fix: Explicit whitelist of exit actions that require PnL calculation
        exit_keywords = ["EXIT", "THETA_EXIT", "TP1", "TRAIL", "TIME", "REVERSAL", "TRAP", "EOD", "FILL"]
        is_exit_action = any(kw in action for kw in exit_keywords) and entry_price_for_pnl > 0

        # Skip non-trade entries (cancelled orders, retries, etc.)
        if any(kw in action for kw in ["CLEARED", "RETRY", "SUBMITTED"]):
            is_exit_action = False

        if is_exit_action:
            # ── [PnL Fix] Side mapping: C and P are LONG (buy to open, sell to close)
            # THETA and SHORT are short (sell to open, buy to close)
            is_long = side in ("C", "P", "CALL", "PUT", "BUY", "LONG")
            is_short = side in ("SELL", "THETA", "SHORT", "IRON_CONDOR")

            if is_short:
                # Short: profit if exit price is lower than entry
                gross_pnl = (entry_price_for_pnl - price) * point_value * qty
            elif is_long:
                # Long: profit if exit price is higher than entry
                gross_pnl = (price - entry_price_for_pnl) * point_value * qty
            else:
                # Unknown side — log warning and skip PnL
                console.print(f"[yellow]⚠️ Unknown side '{side}' — PnL=0 for {action} @ {price}[/yellow]")
                gross_pnl = 0.0

            # 扣除交易成本 (RULES.md Rule 4: PnL Must Include All Costs)
            broker_fee_per_side = getattr(self, "broker_fee_per_side", float(self.execution_cfg.get("broker_fee_per_side", 20.0)))
            exchange_fee_per_side = getattr(self, "exchange_fee_per_side", float(self.execution_cfg.get("exchange_fee_per_side", 5.0)))
            broker_fee = broker_fee_per_side * 2 * qty
            exchange_fee = exchange_fee_per_side * 2 * qty
            # 交易稅: 期權約 0.1% 權利金
            tax_rate = self.pricing_cfg.get("tax_rate", 0.001)
            tax = (abs(entry_price_for_pnl) + abs(price)) * point_value * tax_rate * qty
            pnl = round(gross_pnl - broker_fee - exchange_fee - tax, 0)

            # GSD validation: warn if exit PnL is 0
            if pnl == 0 and "ENTRY" not in action:
                console.print(f"[yellow]⚠️ Exit PnL=0 for {action} {side} @ {price} — check entry_price_override or side mapping[/yellow]")

        # ── [PnL Fix] Balance from last Balance row, not from summing PnL column ──
        balance = 0
        if self.ledger_path.exists():
            try:
                prev = pd.read_csv(self.ledger_path)
                if "Balance" in prev.columns and not prev["Balance"].empty:
                    balance = pd.to_numeric(prev["Balance"].iloc[-1], errors="coerce")
                    if pd.isna(balance):
                        balance = 0
                else:
                    balance = pd.to_numeric(prev["PnL"], errors="coerce").fillna(0).sum()
            except Exception as e:
                console.print(f"[yellow]⚠️ Ledger read error: {e} — balance reset to 0[/yellow]")
        balance = (balance if not pd.isna(balance) else 0) + pnl
        balance = round(balance, 0)
        data = {
            "trade_id": trade_id,
            "Timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Mode": self.mode, "Action": action, "Side": side,
            "Price": price, "Quantity": qty,
            "PnL": pnl, "Balance": balance, "Note": str(note).replace(",", ";"),
        }
        self._append_csv_row_durable(self.ledger_path, data)

        logger.info(
            "TRADE_LEDGER_WRITTEN path=%s trade_id=%s action=%s side=%s price=%s size=%d",
            self.ledger_path.resolve(),
            trade_id,
            action,
            side,
            price,
            self.ledger_path.stat().st_size,
        )

        # [GSD Phase B] Log outcome attribution
        if is_exit_action and hasattr(self, "_entry_features") and self._entry_features:
            from core.decision_logger import DecisionLogger
            outcome = {
                "pnl": float(pnl),
                "exit_price": float(price),
                "exit_reason": str(action)
            }
            DecisionLogger.log_trade_outcome(
                trade_id=trade_id,
                strategy=f"{self.mode}_squeeze",
                regime=self._entry_features.get("regime", "NORMAL"),
                features=self._entry_features,
                outcome=outcome
            )
            # Clear features after exit
            if "TP1" not in action: # Only clear if full exit
                self._entry_features = {}

        return trade_id

    def _find_lifecycle_order(self, order_id):
        if not self.order_mgr or not order_id:
            return None
        order = self.order_mgr.get_order(order_id)
        if order is not None:
            return order
        for completed in self.order_mgr.get_completed():
            if completed.order_id == order_id:
                return completed
        return None

    def _extract_live_fill_identities(self, msg):
        identities = []
        trade_id = str(msg.get("trade_id") or "").strip()
        exchange_seq = str(msg.get("exchange_seq") or "").strip()
        ordno = str(msg.get("ordno") or "").strip()
        action = str(msg.get("action") or "").strip()
        code = str(msg.get("code") or "").strip()
        price = float(msg.get("price", 0.0) or 0.0)
        quantity = int(msg.get("quantity", 0) or 0)

        if trade_id:
            identities.append(("trade_id", trade_id))
        if exchange_seq:
            identities.append(("exchange_seq", exchange_seq))
        if ordno and quantity > 0 and price > 0:
            identities.append(("ordno_fill", ordno, action, code, quantity, round(price, 6)))
        return identities

    def _is_duplicate_live_fill(self, order_id, msg):
        if not hasattr(self, "_seen_fill_identities"):
            self._seen_fill_identities = set()

        identities = self._extract_live_fill_identities(msg)
        if not identities:
            return False

        if any(identity in self._seen_fill_identities for identity in identities):
            return True

        order = self._find_lifecycle_order(order_id)
        if order is None or not self.order_mgr:
            return False

        trade_id = str(msg.get("trade_id") or "").strip() or None
        exchange_seq = str(msg.get("exchange_seq") or "").strip() or None
        return self.order_mgr._has_fill_identity(
            order,
            deal_id=trade_id,
            broker_trade_id=trade_id,
            exchange_fill_id=trade_id,
            exchange_seq=exchange_seq,
        )

    def _remember_live_fill(self, msg):
        if not hasattr(self, "_seen_fill_identities"):
            self._seen_fill_identities = set()

        for identity in self._extract_live_fill_identities(msg):
            self._seen_fill_identities.add(identity)

        ordno = msg.get("ordno")
        if ordno:
            self._seen_fill_ordnos.add(ordno)

    def on_order_event(self, stat, msg):
        # set_order_callback receives OrderState enum:
        #   OrderState.FuturesDeal / OrderState.StockDeal — actual fills
        #   OrderState.FuturesOrder / OrderState.StockOrder — order status changes
        is_deal = stat in (sj.constant.OrderState.FuturesDeal, sj.constant.OrderState.StockDeal)
        is_mock_deal = self.dry_run_live_orders and stat == "MOCK_FILL"
        if not is_mock_deal and not is_deal and stat not in (sj.constant.OrderState.FuturesOrder, sj.constant.OrderState.StockOrder):
            return
        action = str(msg.get("action", ""))
        price = float(msg.get("price", 0.0) or 0.0)
        quantity = int(msg.get("quantity", 0) or 0)
        code = msg.get("code")
        ordno = msg.get("ordno")
        broker_order_id = msg.get("id") or ordno
        seqno = msg.get("seqno")
        raw_status = msg.get("status") or getattr(stat, "value", stat)
        side = self.active_side
        tracked_order_id = None
        if self.pending_entry and code == self.pending_entry.get("contract_code") and action == "Buy":
            tracked_order_id = self.pending_entry.get("order_id")
        elif self.pending_exit_trade and action == "Sell":
            tracked_order_id = self.pending_exit_trade.get("order_id")
        if self.order_mgr and not (is_deal or is_mock_deal):
            if tracked_order_id:
                self.order_mgr.apply_order_update(
                    tracked_order_id,
                    raw_status=raw_status,
                    reason=str(msg.get("errmsg", "") or msg.get("reason", "")),
                    raw_payload=msg,
                    broker_order_id=broker_order_id,
                    seqno=seqno,
                    ordno=ordno,
                )
            return
        if self.pending_entry and code == self.pending_entry["contract_code"] and action == "Buy":
            side = self.pending_entry["side"]
            ordno = msg.get("ordno", "")
            if self._is_duplicate_live_fill(tracked_order_id, msg):
                console.print(f"[yellow]⚠️ Duplicate fill ignored: ordno={ordno}[/yellow]")
                return
            if self.order_mgr and self.pending_entry.get("order_id"):
                self.order_mgr.apply_deal_fill(
                    self.pending_entry["order_id"],
                    fill_price=price,
                    fill_qty=quantity,
                    broker_order_id=broker_order_id,
                    ordno=ordno,
                    exchange_fill_id=msg.get("trade_id"),
                    broker_trade_id=msg.get("trade_id"),
                    exchange_seq=msg.get("exchange_seq"),
                    raw_payload=msg,
                )
            self._remember_live_fill(msg)
            self.position += quantity
            self.active_side = side
            self.entry_price = price
            self.entry_mtx_price = self.pending_entry["entry_mtx_price"]
            self.entry_time = self.pending_entry.get("signal_time") or self._current_strategy_time()
            self.has_tp1_hit = False
            self.stop_loss_price = price * (1 - self.stop_loss_pct)
            self.peak_premium = price
            self.replay_stats["entries"] += 1
            
            # [GSD Phase B] Capture entry features
            self._entry_features = {
                "momentum": float(self.latest_score),
                "regime": str(self.latest_mid_trend),
                "iv": float(self.latest_iv or 0.25),
                "entry_price": float(price)
            }
            
            trade_id = self.log_trade("LIVE_ENTRY_FILLED", side, price, f"qty={quantity}")
            if _notify:
                if _has_notification_system:
                    from core.notification.schemas import TradeEvent as _TE
                    te = _TE(trade_id=trade_id, action="LIVE_ENTRY_FILLED",
                             side=side, price=price, quantity=quantity)
                    _notify_trade_event(event=te, formatter="options", monitor=self)
                elif _has_legacy_formatter:
                    from strategies.options.email_formatter import (
                        TradeEvent as _OldTE, build_from_monitor as _bfm,
                        format_subject as _fs, format_body as _fb,
                    )
                    old_te = _OldTE(trade_id=trade_id, action="LIVE_ENTRY_FILLED",
                                    side=side, price=price, quantity=quantity)
                    payload = _bfm(self, old_te)
                    _notify(_fs(payload), _fb(payload))
                else:
                    _notify(
                        f"[TXO] ENTRY {side} @ {price:.1f} | {trade_id}",
                        f"🟢 ENTRY {side} qty={quantity} @ {price:.1f}\ntrade_id={trade_id}",
                    )
            if self.position >= int(self.pending_entry.get("requested_qty", self.base_lots)):
                self.pending_entry = None
            return
        if self.active_side and action == "Sell":
            if self._is_duplicate_live_fill(tracked_order_id, msg):
                console.print(f"[yellow]⚠️ Duplicate fill ignored: ordno={ordno}[/yellow]")
                return
            if self.order_mgr and self.pending_exit_trade and self.pending_exit_trade.get("order_id"):
                self.order_mgr.apply_deal_fill(
                    self.pending_exit_trade["order_id"],
                    fill_price=price,
                    fill_qty=quantity,
                    broker_order_id=broker_order_id,
                    ordno=ordno,
                    exchange_fill_id=msg.get("trade_id"),
                    broker_trade_id=msg.get("trade_id"),
                    exchange_seq=msg.get("exchange_seq"),
                    raw_payload=msg,
                )
            self._remember_live_fill(msg)
            self.position = max(0, self.position - quantity)
            trade_id = self.log_trade("LIVE_EXIT_FILLED", self.active_side, price, f"qty={quantity} reason={self.pending_exit_reason or ''}".strip())
            if _notify:
                if _has_notification_system:
                    from core.notification.schemas import TradeEvent as _TE
                    te = _TE(trade_id=trade_id, action="LIVE_EXIT_FILLED",
                             side=self.active_side or "", price=price, quantity=quantity)
                    _notify_trade_event(event=te, formatter="options", monitor=self,
                                        realized_pnl=float(pd.read_csv(self.ledger_path).iloc[-1].get("PnL", 0.0))
                                        if hasattr(self, "ledger_path") and self.ledger_path and self.ledger_path.exists() else 0.0)
                elif _has_legacy_formatter:
                    from strategies.options.email_formatter import (
                        TradeEvent as _OldTE, build_from_monitor as _bfm,
                        format_subject as _fs, format_body as _fb,
                        compute_unrealized_pnl as _cupnl,
                    )
                    old_te = _OldTE(trade_id=trade_id, action="LIVE_EXIT_FILLED",
                                    side=self.active_side or "", price=price, quantity=quantity)
                    payload = _bfm(self, old_te)
                    payload.position.unrealized_pnl = _cupnl(payload.position)
                    try:
                        last = pd.read_csv(self.ledger_path).iloc[-1]
                        payload.position.realized_pnl = float(last.get("PnL", 0.0) or 0.0)
                    except Exception:
                        pass
                    _notify(_fs(payload), _fb(payload))
                else:
                    _notify(
                        f"[TXO] EXIT {self.active_side} @ {price:.1f} | {trade_id}",
                        f"🔴 EXIT {self.active_side} qty={quantity} @ {price:.1f} reason={self.pending_exit_reason or ''}\ntrade_id={trade_id}",
                    )
            if self.pending_exit_reason == "LIVE_TP1_SUBMITTED" and self.position > 0:
                self.has_tp1_hit = True
                self.replay_stats["tp1_hits"] += 1
            if self.position == 0:
                self.active_side = None
                self.entry_price = 0.0
                self.entry_mtx_price = 0.0
                self.entry_time = None
                self.has_tp1_hit = False
                self.stop_loss_price = 0.0
                self.peak_premium = 0.0
                self.cooldown_until = self.cooldown_bars
                self.replay_stats["exits"] += 1
            self.pending_exit_qty = max(0, self.pending_exit_qty - quantity)
            if self.pending_exit_qty == 0:
                self.pending_exit_reason = None
                self.pending_exit_trade = None

    def status_mode_label(self):
        if self.dry_run_live_orders:
            return "DRY-LIVE"
        return "LIVE" if self.live_trading else "PAPER"

    def print_status_summary(self, signal=None, force=False):
        now = datetime.datetime.now()
        if not force and self.last_status_print_at and (now - self.last_status_print_at).total_seconds() < self.status_print_secs:
            return
        position_desc = f"{self.position}x {self.active_side}" if self.position > 0 and self.active_side else "flat"
        signal_desc = "none"
        if signal:
            signal_score = signal.get("score", 0.0)
            signal_side = signal.get("side") or "-"
            signal_trend = signal.get("mid_trend") or "-"
            signal_desc = f"score={signal_score:.1f} side={signal_side} trend={signal_trend}"
        elif self.last_signal:
            last_score = self.last_signal.get("score", 0.0)
            last_side = self.last_signal.get("side") or "-"
            last_trend = self.last_signal.get("mid_trend") or "-"
            signal_desc = f"score={last_score:.1f} side={last_side} trend={last_trend}"
        last_price = self.market_data.get("MTX", {}).get("close", 0.0) or 0.0
        console.print(
            f"[cyan][{self.status_mode_label()}][/cyan] mode={self.mode} position={position_desc} "
            f"mtx={last_price:.1f} signal={signal_desc}"
        )
        self.last_status_print_at = now

    def current_option_quote(self, side):
        quote = self.market_data.get(side, {})
        bid = float(quote.get("bid", 0.0) or 0.0)
        ask = float(quote.get("ask", 0.0) or 0.0)
        close = float(quote.get("close", 0.0) or 0.0)
        if bid <= 0:
            bid = close
        if ask <= 0:
            ask = close
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else close
        return {"bid": bid, "ask": ask, "mid": mid, "close": close}

    def validate_quote(self, side, context="GENERIC"):
        """
        Single-source-of-truth quote validation for both entry and exit.

        Returns dict:
            valid: bool
            reason: str (OK / MISSING_QUOTE / INVALID_QUOTE_CROSSED / INVALID_QUOTE_ZERO_MID / WIDE_SPREAD)
            bid: float
            ask: float
            mid: float
            spread_ratio: float | None  (None when quote is invalid)
            max_spread_ratio: float  (threshold used)
        """
        quote = self.current_option_quote(side)
        _ask = quote.get("ask", 0.0)
        _bid = quote.get("bid", 0.0)
        _mid = quote.get("mid", 0.0)
        _max_spread = getattr(self, 'max_spread_pct', 0.3)
        _spread_ratio = None

        if _bid <= 0 or _ask <= 0:
            _reason = "MISSING_QUOTE"
            _valid = False
        elif _ask <= _bid:
            _reason = "INVALID_QUOTE_CROSSED"
            _valid = False
        elif _mid <= 0:
            _reason = "INVALID_QUOTE_ZERO_MID"
            _valid = False
        else:
            _spread_ratio = (_ask - _bid) / _mid
            assert _spread_ratio is None or _spread_ratio >= 0, \
                f"spread_ratio={_spread_ratio} must be None or >= 0"
            if _spread_ratio >= _max_spread:
                _reason = "WIDE_SPREAD"
                _valid = False
            else:
                _reason = "OK"
                _valid = True

        result = {
            "valid": _valid,
            "reason": _reason,
            "bid": _bid,
            "ask": _ask,
            "mid": _mid,
            "spread_ratio": _spread_ratio,
            "max_spread_ratio": _max_spread,
        }

        if not _valid:
            sr_str = f"{_spread_ratio:.1%}" if _spread_ratio is not None else "None"
            console.print(
                f"[yellow]⚠️ [QUOTE_GUARD][{context}_BLOCK] "
                f"reason={_reason} "
                f"side={side} "
                f"bid={_bid} ask={_ask} mid={_mid:.1f} "
                f"spread_ratio={sr_str} "
                f"max_spread={_max_spread:.0%}[/yellow]"
            )

        return result

    def spread_is_tradeable(self, side):
        vq = self.validate_quote(side, context="ENTRY")
        return vq["valid"]

    def sync_contract_quotes(self):
        # Shioaji Option 物件不支援動態 setattr，quotes 直接從 market_data 取
        pass

    def record_signal_snapshot(self, signal):
        # 即使沒有訊號也用即時報價算 Greeks
        iv, delta_val, gamma_val, vega_val = 0.0, 0.0, 0.0, 0.0
        # Fix: initialize strike/dte_years outside try block to prevent NameError
        strike = 0.0
        dte_years = 3.0 / 365.0
        price_mtx = float(self.market_data["MTX"]["close"])
        
        # 優先使用最新計算出的分數與趨勢，若 signal 存在則覆蓋
        score = float(signal.get("score", 0)) if signal else self.latest_score
        mid_trend = (signal.get("mid_trend") or "") if signal else self.latest_mid_trend
        side_label = (signal.get("side") or "") if signal else ""

        if price_mtx <= 0:
            # 💡 GSD: Don't record if price is 0 (initialization spike)
            return

        try:
            calc_side = (signal.get("side") if signal and signal.get("side") else "C")
            quote = self.current_option_quote(calc_side)
            contract = self.active_contracts.get(calc_side)
            strike = float(getattr(contract, "strike_price", resolve_option_strike(price_mtx, self.strike_rounding)))
            delivery_date = getattr(contract, "delivery_date", None)
            dte_years = float(self._dte(delivery_date) if delivery_date else 3.0 / 365.0)
            option_price = float(quote["mid"])
            option_type = 'c' if calc_side == 'C' else 'p'

            if option_price > 0 and strike > 0:
                try:
                    iv = float(self._iv(option_price, price_mtx, strike, dte_years, self.risk_free_rate, option_type))
                    res = self._bs(price_mtx, strike, dte_years, self.risk_free_rate, iv, option_type=calc_side)
                    delta_val, gamma_val, vega_val = res["delta"], res["gamma"], res["vega"]
                except Exception:
                    res = self._bs(price_mtx, strike, dte_years, self.risk_free_rate, 0.25, option_type=calc_side)
                    iv, delta_val, gamma_val, vega_val = 0.25, res["delta"], res["gamma"], res["vega"]
            elif strike > 0:
                res = self._bs(price_mtx, strike, dte_years, self.risk_free_rate, 0.25, option_type=calc_side)
                iv, delta_val, gamma_val, vega_val = 0.25, res["delta"], res["gamma"], res["vega"]
        except Exception as e:
            console.print(f"[red]Greeks calculation error:[/red] {e}")

        now = datetime.datetime.now()
        row = build_options_snapshot_row(
            signal,
            now=now,
            price_mtx=price_mtx,
            score=score,
            side_label=side_label,
            strike=strike,
            dte_days=dte_years * 365,
            mid_trend=mid_trend,
            iv=iv,
            delta_val=delta_val,
            gamma_val=gamma_val,
            vega_val=vega_val,
        )
        
        if iv > 0:
            self.latest_iv = iv
            
        ordered_columns = OPTION_SNAPSHOT_COLUMNS + [col for col in row if col not in OPTION_SNAPSHOT_COLUMNS]
        df_row = pd.DataFrame([row], columns=ordered_columns)
        if self.indicator_log_path.exists():
            try:
                df_existing = pd.read_csv(self.indicator_log_path)
                df_combined = pd.concat([df_existing, df_row], ignore_index=True)
                # For options, we might have multiple signals per minute if it refreshes frequently
                # but usually it's once per minute or per tick. Let's keep last by timestamp.
                df_combined.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
                df_combined.to_csv(self.indicator_log_path, index=False)
            except Exception:
                df_row.to_csv(self.indicator_log_path, mode="a", index=False, header=False)
        else:
            df_row.to_csv(self.indicator_log_path, index=False, header=True)

    def _get_mtx_tick_bars_df(self):
        """[Wave 2 optimization] Lazy DF conversion: rebuild cache only on new bar."""
        if self._mtx_tick_bars_cache is None and len(self._mtx_tick_bars_deque) > 0:
            # Build DataFrame from deque
            records = list(self._mtx_tick_bars_deque)
            self._mtx_tick_bars_cache = pd.DataFrame({
                "Open": [r["open"] for r in records],
                "High": [r["high"] for r in records],
                "Low": [r["low"] for r in records],
                "Close": [r["close"] for r in records],
                "Volume": [r["volume"] for r in records],
            }, index=[r["ts"] for r in records])
        return self._mtx_tick_bars_cache if self._mtx_tick_bars_cache is not None else pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    def _normalize_prefill_mtx_bars(self, frame, *, source_timeframe):
        """Convert raw MTX history into canonical 5m bars before warming the tick fallback cache."""
        frames = build_canonical_bar_frames(
            frame,
            source_timeframe=source_timeframe,
            max_gap_minutes=15,
        )
        return frames.get("5m")

    def _live_min_bars_required(self, now=None):
        from core.date_utils import is_night_session

        current_time = now or datetime.datetime.now()
        return 10 if is_night_session(current_time) else max(30, self.strategy_cfg.get("length", 20) + 5)

    def _evaluate_signal_bar_quality(self, signal, reference_price=None):
        ohlc = {}
        issues = []
        aliases = {
            "Open": ("Open", "open"),
            "High": ("High", "high"),
            "Low": ("Low", "low"),
            "Close": ("Close", "close"),
        }

        for key, names in aliases.items():
            value = None
            for name in names:
                raw = signal.get(name)
                if raw is None or pd.isna(raw):
                    continue
                value = float(raw)
                break
            ohlc[key] = value
            if value is None:
                issues.append(f"missing_{key.lower()}")
            elif value <= 0:
                issues.append(f"non_positive_{key.lower()}")

        if not issues:
            if ohlc["High"] < ohlc["Low"]:
                issues.append("high_below_low")
            body_low = min(ohlc["Open"], ohlc["Close"])
            body_high = max(ohlc["Open"], ohlc["Close"])
            if ohlc["Low"] > body_low:
                issues.append("low_above_body")
            if ohlc["High"] < body_high:
                issues.append("high_below_body")

        reference = float(reference_price or signal.get("price_mtx") or 0.0)
        max_drift = float(self._theta_cfg.get("max_bar_price_deviation_pts", 250))
        max_reference_deviation = 0.0
        if reference > 0 and not issues:
            max_reference_deviation = max(
                abs(value - reference) for value in ohlc.values() if value is not None
            )
            if max_reference_deviation > max_drift:
                issues.append(f"price_drift>{max_drift:.0f}")

        return {
            "quality": "PASS" if not issues else "BLOCK",
            "issues": issues,
            "reference_price": reference,
            "max_reference_deviation": round(max_reference_deviation, 1),
        }

    def _resolve_futures_squeeze_state(self, bar_ts):
        bar_time = pd.Timestamp(bar_ts)
        date_str = get_session_date_str(bar_time.to_pydatetime())
        market_dir = Path("logs/market_data")
        patterns = [
            f"TMF_{date_str}_*_indicators.csv",
            "TMF_*_indicators.csv",
        ]
        candidates = []
        seen = set()
        for pattern in patterns:
            for path in sorted(market_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True):
                if path in seen:
                    continue
                seen.add(path)
                candidates.append(path)

        for path in candidates:
            try:
                df = pd.read_csv(path)
            except Exception:
                continue

            if df.empty or "timestamp" not in df.columns or "sqz_on" not in df.columns:
                continue

            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            usable = df[df["timestamp"] <= bar_time].dropna(subset=["timestamp"])
            if usable.empty:
                continue

            latest = usable.iloc[-1]
            matched_ts = pd.Timestamp(latest["timestamp"])
            if (bar_time - matched_ts) > pd.Timedelta(minutes=10):
                continue

            raw_sqz = latest.get("sqz_on")
            if pd.isna(raw_sqz):
                continue
            return bool(raw_sqz), f"{path.name}@{matched_ts.isoformat()}"

        return None, "futures_unavailable"

    def _update_theta_release_confirmation(self, signal, reference_price):
        bar_ts = pd.Timestamp(
            signal.get("completed_bar_timestamp")
            or signal.get("timestamp")
            or datetime.datetime.now()
        ).floor("5min")
        quality = self._evaluate_signal_bar_quality(signal, reference_price=reference_price)
        signal["bar_quality"] = quality["quality"]
        signal["bar_quality_issues"] = ";".join(quality["issues"])
        signal["bar_reference_price"] = quality["reference_price"]
        signal["bar_max_reference_deviation"] = quality["max_reference_deviation"]
        signal["completed_bar_timestamp"] = bar_ts.isoformat()

        raw_release = not bool(signal.get("squeeze_on", False))
        futures_sqz_on, futures_source = self._resolve_futures_squeeze_state(bar_ts)
        signal["futures_sqz_on"] = futures_sqz_on
        signal["futures_sqz_source"] = futures_source

        release_bar_confirmed = (
            raw_release
            and quality["quality"] == "PASS"
            and futures_sqz_on is not True
        )

        if self._theta_release_last_bar_ts != bar_ts:
            self._theta_release_last_bar_ts = bar_ts
            self._theta_release_confirm_count = (
                self._theta_release_confirm_count + 1 if release_bar_confirmed else 0
            )

        confirm_bars = max(1, int(self._theta_cfg.get("squeeze_release_confirm_bars", 2)))
        confirmed = release_bar_confirmed and self._theta_release_confirm_count >= confirm_bars

        if quality["quality"] != "PASS":
            reason = f"bar_quality:{signal['bar_quality_issues']}"
        elif futures_sqz_on is True:
            reason = "futures_sqz_conflict"
        elif raw_release and not confirmed:
            reason = f"waiting_release_confirmation:{self._theta_release_confirm_count}/{confirm_bars}"
        elif raw_release:
            reason = f"release_confirmed:{self._theta_release_confirm_count}/{confirm_bars}"
        else:
            reason = "squeeze_still_on"

        return {
            "confirmed": confirmed,
            "raw_release_candidate": raw_release,
            "reason": reason,
            "confirm_count": self._theta_release_confirm_count,
            "confirm_bars": confirm_bars,
            "futures_sqz_on": futures_sqz_on,
        }

    def _select_live_bar_frames(self, now=None):
        current_time = now or datetime.datetime.now()
        min_bars = self._live_min_bars_required(current_time)
        return build_preferred_canonical_bar_frames(
            [
                {"name": "api-1m", "frame": self._fetch_today_futures_bars(), "source_timeframe": "1min"},
                {"name": "tick-5m", "frame": self._get_tick_bars_fallback(), "source_timeframe": "5min"},
            ],
            min_5m_bars=min_bars,
            now=pd.Timestamp(current_time),
            max_gap_minutes=15,
            validator=lambda df: validate_ohlcv_bars(
                df,
                min_bars=min_bars,
                expected_interval_minutes=5,
                max_intraday_gap_minutes=30,
                # [BUG FIX 2026-04-20] 380 min rejects valid Monday/weekend windows.
                max_session_gap_minutes=7200,
            ),
        )

    def _inspect_indicator_log_health(self, now=None):
        current_time = now or datetime.datetime.now()
        self._update_log_paths()
        if not self.indicator_log_path.exists():
            return "indicator_file_missing"

        try:
            df = pd.read_csv(self.indicator_log_path)
        except Exception as exc:
            return f"indicator_read_error:{type(exc).__name__}"

        if df.empty:
            return "indicator_file_empty"
        if "timestamp" not in df.columns:
            return "indicator_timestamp_missing"

        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        recent = df[df["timestamp"] >= pd.Timestamp(current_time) - pd.Timedelta(minutes=20)].tail(3)
        if recent.empty:
            return "indicator_rows_stale"

        required_cols = ["trading_day", "Open", "High", "Low", "Close", "Volume"]
        missing_cols = [col for col in required_cols if col not in recent.columns]
        if missing_cols:
            return f"indicator_cols_missing:{','.join(missing_cols)}"

        nan_cols = [col for col in required_cols if recent[col].isna().all()]
        if nan_cols:
            return f"indicator_nan:{','.join(nan_cols)}"

        if "mid_trend" in recent.columns and "score" in recent.columns:
            mid_trend_blank = recent["mid_trend"].fillna("").astype(str).str.strip().eq("").all()
            score_zero = pd.to_numeric(recent["score"], errors="coerce").fillna(0.0).eq(0.0).all()
            if mid_trend_blank and score_zero:
                return "indicator_signal_blank"

        return ""

    def audit_indicator_health_and_repair(self, timestamp=None):
        current_time = timestamp
        if isinstance(timestamp, pd.Timestamp):
            current_time = timestamp.to_pydatetime()
        if current_time is None:
            current_time = datetime.datetime.now()

        raw_frames, bar_source = self._select_live_bar_frames(now=current_time)
        df_5m = raw_frames.get("5m")
        issues = []
        repairs = []
        min_bars = self._live_min_bars_required(current_time)

        if df_5m is None or len(df_5m) < min_bars:
            rejected = ",".join(bar_source.get("rejected", [])) or "no_bar_source"
            issues.append(f"bars_unavailable:{rejected}")
        else:
            latest_bar = df_5m.iloc[-1].to_dict()
            latest_bar["timestamp"] = df_5m.index[-1]
            latest_bar["price_mtx"] = float(
                self.market_data.get("MTX", {}).get("close", 0.0)
                or latest_bar.get("Close", 0.0)
            )
            latest_quality = self._evaluate_signal_bar_quality(
                latest_bar,
                reference_price=latest_bar["price_mtx"],
            )
            if latest_quality["quality"] != "PASS":
                issues.append(f"bar_quality:{';'.join(latest_quality['issues'])}")

        indicator_issue = self._inspect_indicator_log_health(now=current_time)
        if indicator_issue:
            issues.append(indicator_issue)

        order_issue = self.audit_order_lifecycle_health_and_repair(current_time)
        if order_issue and not order_issue.endswith("idle") and "healthy" not in order_issue:
            issues.append(order_issue)

        if not issues:
            return f"healthy source={bar_source.get('source') or 'unknown'} bars={len(df_5m) if df_5m is not None else 0}; {order_issue}"

        missing_contracts = [key for key in ("MTX", "C", "P") if self.active_contracts.get(key) is None]
        if missing_contracts and self.find_best_contracts():
            repairs.append(f"refresh_contracts:{','.join(missing_contracts)}")

        self.pre_fill_bars()
        repairs.append("prefill_bars")

        repaired_frames, repaired_source = self._select_live_bar_frames(now=current_time)
        repaired_df_5m = repaired_frames.get("5m")
        if repaired_df_5m is None or len(repaired_df_5m) < min_bars:
            rejected = ",".join(repaired_source.get("rejected", [])) or "no_bar_source"
            return (
                f"repair_failed issues={';'.join(issues)} repairs={','.join(repairs)} "
                f"after={rejected}"
            )

        return (
            f"repair_ok issues={';'.join(issues)} repairs={','.join(repairs)} "
            f"source={repaired_source.get('source') or 'unknown'} bars={len(repaired_df_5m)}"
        )

    def _fetch_today_futures_bars(self):
        if self.dry_run:
            return self._build_dry_run_bars()
        
        # 增加頻率限制：每 5 分鐘才調用一次 kbars API
        # [Wave 2 optimization] Use lazy DF conversion from deque
        now_ts = time.time()
        if now_ts - self.last_kbars_fetch_at < 300 and len(self._mtx_tick_bars_deque) > 0:
            return self._get_mtx_tick_bars_df().copy()

        if not hasattr(self.api, "kbars") or "MTX" not in self.active_contracts:
            return None
        
        # 確保 api.quote 對象存在且健康，避免 NoneType 錯誤
        if not self.api.quote:
            return None

        # Shioaji server stores K-bars by calendar date, not trading day.
        today = datetime.datetime.now()
        start_date = (today - datetime.timedelta(days=3)).strftime("%Y-%m-%d")
        if today.hour < 5:
            today = today - datetime.timedelta(days=1)
        date_str = today.strftime("%Y-%m-%d")
        try:
            import threading as _th
            _fut_result = [None]
            _fut_done = [False]
            def _kbars_target():
                try:
                    _fut_result[0] = self.api.kbars(self.active_contracts["MTX"], start=start_date, end=date_str)
                except Exception as e:
                    _fut_result[0] = e
                finally:
                    _fut_done[0] = True
            _t = _th.Thread(target=_kbars_target, daemon=True)
            _t.start()
            _t.join(timeout=22)
            if not _fut_done[0]:
                console.print("[red]⏱️ kbar fetch timed out (22s) — Shioaji API may be unreachable. Proceeding without API bars.[/red]")
                return None
            if isinstance(_fut_result[0], Exception):
                raise _fut_result[0]
            bars = _fut_result[0]
            self.last_kbars_fetch_at = now_ts
            frame = pd.DataFrame({**bars})
        except Exception as e:
            console.print(f"[red]Error fetching kbars:[/red] {e}")
            return None
        
        if frame.empty or "ts" not in frame.columns:
            return None
        frame["ts"] = pd.to_datetime(frame["ts"])
        frame = frame.rename(columns={"Open": "Open", "High": "High", "Low": "Low", "Close": "Close", "Volume": "Volume"})
        if "open" in frame.columns:
            frame = frame.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
        required = {"Open", "High", "Low", "Close", "Volume"}
        if not required.issubset(frame.columns):
            return None
        frame = frame.set_index("ts")[["Open", "High", "Low", "Close", "Volume"]].sort_index()
        return canonicalize_ohlcv(frame)

    def _validate_kbar_data(self, df):
        """Validate 5m kbar data via the shared canonical contract."""
        from core.date_utils import is_night_session as _is_night

        is_night = _is_night(datetime.datetime.now())
        min_bars_required = 10 if is_night else 30
        return validate_ohlcv_bars(
            df,
            min_bars=min_bars_required,
            expected_interval_minutes=5,
            max_intraday_gap_minutes=30,
            # [BUG FIX 2026-04-20] 380 min (~6.5 h) is too small: the 3-day API window
            # always spans a weekend on Mondays (Fri night → Mon morning = ~3106 min).
            # 7200 min (5 days) accommodates weekends and national holidays while still
            # catching genuine multi-week data gaps that would indicate a real API problem.
            max_session_gap_minutes=7200,
        )

    def _fill_small_kbar_gaps(self, df, max_gap_minutes=15):
        """Fill small 5m gaps via the shared canonical contract."""
        if df is None or df.empty or len(df) < 2:
            return df
        try:
            return fill_small_ohlcv_gaps(
                df,
                expected_freq="5min",
                max_gap_minutes=max_gap_minutes,
            )
        except Exception as e:
            console.print(f"[yellow]⚠️ 資料填充失敗: {e}[/yellow]")
            return df

    def _validate_and_fill_kbar_gaps(self, df):
        """驗證並填充kbar資料缺口
        
        Args:
            df: 原始kbar資料
            
        Returns:
            處理後的kbar資料，如果驗證失敗則返回None
        """
        if df is None or df.empty:
            return None
        
        # 第一步：驗證資料完整性
        is_valid, msg = self._validate_kbar_data(df)
        
        if not is_valid:
            console.print(f"[yellow]⚠️ Kbar資料驗證失敗: {msg}[/yellow]")
            
            # 嘗試填充小缺口後再驗證
            df_filled = self._fill_small_kbar_gaps(df)
            is_valid_filled, msg_filled = self._validate_kbar_data(df_filled)
            
            if is_valid_filled:
                console.print(f"[green]✓ 資料填充後驗證通過[/green]")
                return df_filled
            else:
                console.print(f"[red]✗ 資料填充後仍驗證失敗: {msg_filled}[/red]")
                return None
        
        # 第二步：填充小缺口
        df_filled = self._fill_small_kbar_gaps(df)
        
        # 第三步：最終驗證
        is_final_valid, final_msg = self._validate_kbar_data(df_filled)
        
        if not is_final_valid:
            console.print(f"[red]✗ 最終資料驗證失敗: {final_msg}[/red]")
            return None
        
        return df_filled

    def pre_fill_bars(self):
        """Pre-fill tick bar buffer from kbars on startup."""
        try:
            # 💡 GSD: Try CSV fallback first or if bars are insufficient
            csv_path = Path("data/tmf_full_2026.csv")
            if csv_path.exists():
                try:
                    df_hist = pd.read_csv(csv_path)
                    # Handle both 'ts' and 'timestamp' column names
                    if 'ts' in df_hist.columns:
                        df_hist["ts"] = pd.to_datetime(df_hist["ts"])
                    elif 'timestamp' in df_hist.columns:
                        df_hist["ts"] = pd.to_datetime(df_hist["timestamp"])
                    else:
                        raise ValueError("CSV must have 'ts' or 'timestamp' column")
                    df_hist = df_hist.set_index("ts").sort_index()
                    df_warm = self._normalize_prefill_mtx_bars(df_hist, source_timeframe="1min")
                    if df_warm is None or df_warm.empty:
                        raise ValueError("CSV warmup produced no canonical 5m bars")
                    df_warm = df_warm.tail(100)
                    for ts, row in df_warm.iterrows():
                        self._mtx_tick_bars_deque.append({
                            "open": row["Open"], "high": row["High"], "low": row["Low"], 
                            "close": row["Close"], "volume": row["Volume"], "ts": ts
                        })
                    self._mtx_tick_bars_cache = df_warm[["Open", "High", "Low", "Close", "Volume"]].copy()
                    console.print(f"[green][OptionsMonitor] ✓ Pre-filled {len(self._mtx_tick_bars_deque)} MTX bars from local CSV[/green]")
                except Exception as e:
                    console.print(f"[dim][OptionsMonitor] CSV pre-fill failed: {e}[/dim]")

            # Attempt API fetch to get most recent bars
            bars = self._fetch_today_futures_bars()
            bars_5m = self._normalize_prefill_mtx_bars(bars, source_timeframe="1min")
            if bars_5m is not None and not bars_5m.empty and len(bars_5m) >= 30:
                # [Wave 2 optimization] Convert pre-filled bars to deque format
                for _, row in bars_5m[["Open", "High", "Low", "Close", "Volume"]].iterrows():
                    bar_dict = {
                        "open": row["Open"],
                        "high": row["High"],
                        "low": row["Low"],
                        "close": row["Close"],
                        "volume": row["Volume"],
                        "ts": row.name,  # DataFrame index is timestamp
                    }
                    self._mtx_tick_bars_deque.append(bar_dict)
                self._mtx_tick_bars_cache = bars_5m[["Open", "High", "Low", "Close", "Volume"]].copy()
                self.is_trading_ready = True # [GSD 4.13] Trading Phase Ready
                from core.shioaji_session import set_system_status, SystemReadiness
                set_system_status(SystemReadiness.TRADING)
                console.print(f"[bold green]🔥 [OptionsMonitor] Trading READY: {len(self._mtx_tick_bars_deque)} bars loaded.[/bold green]")
        except Exception:
            pass

    def _get_tick_bars_fallback(self):
        """Fallback: use tick-built bars when kbars API is unavailable."""
        from core.date_utils import is_night_session

        df = self._get_mtx_tick_bars_df()
        min_bars_required = 10 if is_night_session(datetime.datetime.now()) else max(30, self.strategy_cfg.get("length", 20) + 5)
        if len(df) >= min_bars_required:
            return df.copy()
        return None

    def _build_dry_run_bars(self):
        if self.replay_bars is not None:
            if self.replay_cursor is None or self.replay_cursor > len(self.replay_bars):
                return None
            replay_slice = self.replay_bars.iloc[:self.replay_cursor].copy()
            last_close = float(replay_slice.iloc[-1]["Close"])
            current_time = replay_slice.index[-1].to_pydatetime()
            self._refresh_dry_run_contracts(current_time, last_close)
            self.market_data["MTX"] = {"close": last_close, "bid": last_close - 1.0, "ask": last_close + 1.0}
            self._sync_mock_option_quotes(last_close)
            self.replay_cursor += 1
            return replay_slice
        end_time = pd.Timestamp.now().floor("5min")
        start_time = end_time - pd.Timedelta(minutes=5 * 239)
        index = pd.date_range(start=start_time, end=end_time, freq="5min")
        if len(index) < 200:
            return None
        base = self.fallback_underlying_price
        close = pd.Series(base + (pd.RangeIndex(len(index)) * 2.5), index=index, dtype=float)
        open_ = close.shift(1).fillna(close.iloc[0] - 3.0)
        high = pd.concat([open_, close], axis=1).max(axis=1) + 4.0
        low = pd.concat([open_, close], axis=1).min(axis=1) - 4.0
        volume = pd.Series(150 + (pd.RangeIndex(len(index)) % 20) * 5, index=index, dtype=float)
        frame = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}, index=index)
        self.market_data["MTX"] = {"close": float(frame.iloc[-1]["Close"]), "bid": float(frame.iloc[-1]["Close"]) - 1.0, "ask": float(frame.iloc[-1]["Close"]) + 1.0}
        self._sync_mock_option_quotes(float(frame.iloc[-1]["Close"]))
        return frame

    def _sync_mock_option_quotes(self, underlying_price):
        strike = getattr(self.active_contracts.get("C"), "strike_price", resolve_option_strike(underlying_price, self.strike_rounding))
        call_intrinsic = max(underlying_price - strike, 0.0)
        put_intrinsic = max(strike - underlying_price, 0.0)
        call_mid = max(15.0, 25.0 + call_intrinsic * 0.12)
        put_mid = max(15.0, 25.0 + put_intrinsic * 0.12)
        self.market_data["C"] = {"close": call_mid, "bid": call_mid - 1.0, "ask": call_mid + 1.0}
        self.market_data["P"] = {"close": put_mid, "bid": put_mid - 1.0, "ask": put_mid + 1.0}

    def fetch_live_signal(self):
        raw_frames, bar_source = self._select_live_bar_frames()
        df5_raw = raw_frames.get("5m")
        if df5_raw is None or len(df5_raw) < 2:
            # GSD: Still record snapshot with current price even if bars are missing
            console.print("[red]✗ 無法獲取有效的kbar資料，跳過信號生成[/red]")
            self.record_signal_snapshot(None)
            return None

        if df5_raw is not None:
            console.print(f"[debug] Raw bars: {len(df5_raw)} rows, from {df5_raw.index[0]} to {df5_raw.index[-1]}")
            # DEBUG: Check time intervals
            diffs = df5_raw.index.to_series().diff().dropna()
            if len(diffs) > 0:
                console.print(f"[debug] Intervals: min={diffs.min()}, median={diffs.median()}, max={diffs.max()}")

        try:
            p5 = attach_bar_metadata(calculate_futures_squeeze(canonicalize_ohlcv(df5_raw), self.strategy_cfg.get("length", 20)))

            def safe_indicator_frame(frame):
                base = canonicalize_ohlcv(frame)
                if len(base) < 2:
                    return base
                return attach_bar_metadata(calculate_futures_squeeze(base, self.strategy_cfg.get("length", 20)))

            p15 = safe_indicator_frame(raw_frames.get("15m"))
            p1h = safe_indicator_frame(raw_frames.get("1h"))

            row = p5.iloc[-1]
            timestamp = row.name
            
            # 💡 GSD: Initialize default signal early to ensure snapshot recording
            row_data = row.to_dict()
            
            def safe_bool(val):
                if pd.isna(val) or val is None:
                    return False
                return bool(val)

            signal = {
                "score": 0.0,
                "side": None,
                "price_mtx": float(row.get("Close", 0)),
                "mid_trend": "",
                "timestamp": timestamp,
                "completed_bar_timestamp": timestamp,
                "squeeze_on": safe_bool(row.get("sqz_on")),
                "fired": safe_bool(row.get("fired")),
                "bullish_align": safe_bool(row.get("bullish_align")),
                "bearish_align": safe_bool(row.get("bearish_align")),
                "bar_source": bar_source.get("source"),
                "bar_freshness_minutes": bar_source.get("freshness_minutes"),
            }
            # GSD: Include all raw indicators for dashboard visibility
            for k, v in row_data.items():
                if k not in signal:
                    signal[k] = v

            bar_quality = self._evaluate_signal_bar_quality(
                signal,
                reference_price=float(self.market_data["MTX"]["close"] or signal.get("price_mtx", 0.0)),
            )
            signal["bar_quality"] = bar_quality["quality"]
            signal["bar_quality_issues"] = ";".join(bar_quality["issues"])
            signal["bar_reference_price"] = bar_quality["reference_price"]
            signal["bar_max_reference_deviation"] = bar_quality["max_reference_deviation"]

            m15 = p15[p15.index <= timestamp]
            h1 = p1h[p1h.index <= timestamp]

            if m15.empty or h1.empty:
                console.print(f"[yellow]Insufficient MTF data: 15m={len(m15)}, 1h={len(h1)}[/yellow]")
                self.record_signal_snapshot(signal)
                return signal

            # Check for momentum column availability
            has_momentum_15m = "momentum" in m15.columns and not m15["momentum"].empty
            has_momentum_1h = "momentum" in h1.columns and not h1["momentum"].empty
            
            if not has_momentum_15m:
                console.print("[yellow]15m momentum not available yet, waiting for more data...[/yellow]")
                self.record_signal_snapshot(signal)
                return signal
            
            # Use available timeframes for alignment score
            available_data = {"5m": p5, "15m": m15}
            if has_momentum_1h:
                available_data["1h"] = h1
            else:
                console.print("[yellow]1h momentum not available (insufficient data), using 5m+15m only[/yellow]")
                # Adjust weights for available timeframes
                if len(available_data) == 2:
                    available_data["1h"] = m15  # Use 15m as proxy for 1h

            score = calculate_mtf_alignment(available_data, weights=self.weights)["score"]
            mid_trend = infer_mid_trend(m15)
            
            # 更新最新狀態
            self.latest_score = score
            self.latest_mid_trend = mid_trend or ""
            # Cache BS for failsafe
            self.latest_bs_atr = float(row.get("breakout_strength_atr", 0.0) or 0.0)
            self.latest_bear_bs_atr = float(row.get("bear_breakout_strength_atr", 0.0) or 0.0)

            side = resolve_entry_side(row, score, row["Close"], self.entry_score, mid_trend=mid_trend, require_mid_trend=True)

            # V2 filters: require_fire and require_align
            if self.strategy_cfg.get("require_fire"):
                row_fired = row.get("fired", False)
                fire_threshold = float(self.strategy_cfg.get("fire_score_threshold", 80))
                if not row_fired and abs(score) < fire_threshold:
                    console.print(f"[dim]🚫 FIRE blocked: fired={row_fired} score={score:.1f}<{fire_threshold:.0f}[/dim]")
                    side = None
            if side and self.strategy_cfg.get("require_align"):
                if side == "C" and not row.get("bullish_align", False):
                    console.print(f"[dim]🚫 ALIGN blocked (C): fast={row.get('ema_fast',0):.0f} slow={row.get('ema_slow',0):.0f}[/dim]")
                    side = None
                elif side == "P" and not row.get("bearish_align", False):
                    console.print(f"[dim]🚫 ALIGN blocked (P): fast={row.get('ema_fast',0):.0f} slow={row.get('ema_slow',0):.0f}[/dim]")
                    side = None

            # Update signal with calculated results
            signal.update({
                "score": score,
                "side": side,
                "mid_trend": mid_trend,
            })
            
            self.last_signal = signal
            self.replay_stats["signals"] += 1
            if side:
                self.replay_stats["directional_signals"] += 1

            if not signal.get("side"):
                if abs(score) < 1.0 and self.replay_stats["signals"] % 5 == 0:
                    console.print(f"[dim]⏳ Waiting for indicators to mature... (Score={score:.1f})[/dim]")
                elif self.replay_stats["signals"] % 10 == 0:
                    reasons = []
                    if row.get("sqz_on", True):
                        reasons.append("SQZ_ON")
                    else:
                        if abs(score) < self.entry_score:
                            reasons.append(f"SCORE<{self.entry_score} ({score:.1f})")
                        if score > 0 and row.get("Close", 0) <= row.get("vwap", 0):
                            reasons.append("price<=VWAP")
                        if score < 0 and row.get("Close", 0) >= row.get("vwap", 0):
                            reasons.append("price>=VWAP")
                        if not row.get("fired", False) and abs(score) < 90:
                            reasons.append("not_fired")
                        if score > 0 and not row.get("bullish_align", False):
                            reasons.append("!bullish_align")
                        if score < 0 and not row.get("bearish_align", False):
                            reasons.append("!bearish_align")
                        if mid_trend not in ("BULL", "BEAR"):
                            reasons.append(f"mid_trend={mid_trend}")
                    if reasons:
                        console.print(f"[dim]⏳ No entry ({', '.join(reasons)})[/dim]")

            self.record_signal_snapshot(signal)
            return signal
        except Exception as e:
            console.print(f"[red]Error in fetch_live_signal:[/red] {e}")
            import traceback
            console.print(traceback.format_exc())
            return None

    def _dte_allows_entry(self, side, now=None):
        """進場前檢查 DTE，不足則拒絕進場"""
        if self.min_dte_to_exit is None:
            return True
        contract = self.active_contracts.get(side)
        if contract is None:
            return True
        now = now or self._current_strategy_time()
        dte_days = calculate_dte(contract.delivery_date, now=now) * 365
        if dte_days <= self.min_dte_to_exit:
            console.print(f"[yellow]⛔ Entry blocked: DTE={dte_days:.1f} <= min_dte={self.min_dte_to_exit}[/yellow]")
            return False
        return True

    # ── [Vertical Spread v1] Wrapper: select spread → paper entry ──
    # 2026-05-22 Hermes Agent: shared position guard extracted from enter_paper_position
    # Spread entry paths were bypassing max_positions / _daily_entries / cooldown checks
    def _can_enter_position(self, side, signal) -> tuple[bool, str]:
        """Shared guard: check position limits before any entry path.

        Returns (True, "") if allowed, (False, "reason") if blocked.
        Called by enter_paper_position, enter_spread_paper_position, enter_spread_live_position.
        """
        # 1. Entry lots check
        entry_lots = int(signal.get("entry_lots", self.base_lots)) if isinstance(signal, dict) else self.base_lots
        if entry_lots <= 0:
            return False, f"entry_lots={entry_lots}"

        # 2. Max positions cap
        if self.position >= self.max_positions:
            return False, f"max positions ({self.max_positions}) reached (currently {self.position})"

        # 3. Max daily entries
        if not hasattr(self, '_daily_entries'):
            self._daily_entries = 0
        max_daily = self.full_cfg.get("risk_mgmt", {}).get("max_daily_entries", 3)
        if self._daily_entries >= max_daily:
            return False, f"max daily entries ({max_daily}) reached"

        # 4. DTE check
        if not self._dte_allows_entry(side, now=signal.get("timestamp")):
            return False, "dte too low"

        return True, ""

    def enter_spread_paper_position(self, direction, signal):
        """
        Wrapper: converts CALL/PUT signal into debit vertical spread via spread_selector.
        Falls back to enter_paper_position() if spread selector rejects.
        
        Args:
            direction: "CALL" or "PUT" (or "C"/"P")
            signal: signal dict from fetch_live_signal()
        """
        direction_full = "CALL" if direction in ("C", "CALL") else "PUT" if direction in ("P", "PUT") else direction
        if direction_full not in ("CALL", "PUT"):
            console.print(f"[yellow][VerticalSpread] enter_spread_paper_position: unknown direction {direction}, falling back[/yellow]")
            return self.enter_paper_position(direction, signal)
        
        # ── [PositionGuard] Check position limits before spread selection ──
        allowed, reason = self._can_enter_position(direction_full, signal)
        if not allowed:
            console.print(f"[red]🚫 [VerticalSpread] Entry blocked: {reason}[/red]")
            return

        # Get option chain and index price
        from strategies.options.spread_selector import select_vertical_spread, build_combo_legs
        
        index_price = self.market_data.get("MTX", {}).get("close", 0.0) or signal.get("price_mtx", 0)
        if index_price <= 0:
            console.print("[yellow][VerticalSpread] No index price available, falling back to single-leg[/yellow]")
            return self.enter_paper_position(direction, signal)
        
        # Build market_data dict for all contracts we track
        tx_code = self.active_contracts.get("MTX", None)
        mtx_close = self.market_data.get("MTX", {}).get("close", index_price)
        
        option_chain = getattr(self, '_all_month_contracts', [])
        if not option_chain:
            console.print("[yellow][VerticalSpread] No option chain available, falling back to single-leg[/yellow]")
            return self.enter_paper_position(direction, signal)
        
        spread, reason = select_vertical_spread(
            direction=direction_full,
            index_price=mtx_close,
            option_chain=option_chain,
            width=self._spread_width,
        )
        
        if spread is None:
            console.print(f"[yellow][VerticalSpread][SKIP] {reason} — falling back to single-leg[/yellow]")
            return self.enter_paper_position(direction, signal)
        
        # Spread selected — record paper entry for both legs
        console.print(f"[bold green][VerticalSpread][ALLOW] {spread.direction} spread: "
                      f"Buy {spread.long_leg.strike}{spread.long_leg.option_type[0]} "
                      f"Sell {spread.short_leg.strike}{spread.short_leg.option_type[0]} "
                      f"net_debit={spread.net_debit:.1f} max_profit={spread.max_profit:.1f}[/bold green]")
        
        entry_lots = int(signal.get("entry_lots", self.base_lots)) if isinstance(signal, dict) else self.base_lots
        if entry_lots <= 0:
            return
        
        # Record entry in paper ledger as a combo-style entry
        note = (f"VERTICAL_SPREAD {direction_full} "
                f"Buy {spread.long_leg.strike}{spread.long_leg.option_type[0]}@{spread.long_mid:.1f} "
                f"Sell {spread.short_leg.strike}{spread.short_leg.option_type[0]}@{spread.short_mid:.1f} "
                f"net_debit={spread.net_debit:.1f}")
        
        paper_order = self._record_paper_order(
            direction_full, "BUY", entry_lots, spread.net_debit,
            note,
            strategy_override="VERTICAL_SPREAD",
        )
        if self.order_mgr and paper_order is None:
            return
        
        # Track position as spread (use direction as active_side, net_debit as entry_price)
        self.position += entry_lots
        self.active_side = direction_full
        prev_cost = self.entry_price * (self.position - entry_lots) if self.position > entry_lots else 0
        self.entry_price = (prev_cost + spread.net_debit * entry_lots) / self.position if self.position > 0 else spread.net_debit
        self.entry_mtx_price = mtx_close
        self.entry_time = signal.get("timestamp") or self._current_strategy_time()
        self.has_tp1_hit = False
        self.stop_loss_price = spread.net_debit * (1 - self.stop_loss_pct)
        self.peak_premium = spread.net_debit
        self.replay_stats["entries"] += 1
        self._daily_entries = getattr(self, '_daily_entries', 0) + 1
        
        # Store spread metadata for exit decisions
        self._current_spread = {
            "long_strike": spread.long_leg.strike,
            "short_strike": spread.short_leg.strike,
            "option_type": spread.long_leg.option_type,
            "net_debit": spread.net_debit,
            "max_profit": spread.max_profit,
            "max_risk": spread.max_risk,
            "strike_width": spread.strike_width,
            "expiration": spread.expiration,
        }
        
        logger.info(
            "[VerticalSpread] ENTRY %s qty=%d net_debit=%.1f max_profit=%.1f max_risk=%.1f",
            direction_full, entry_lots, spread.net_debit, spread.max_profit, spread.max_risk
        )

    # ── [Vertical Spread v1] Live trading spread entry ──
    def enter_spread_live_position(self, direction, signal):
        """
        Convert CALL/PUT signal into debit vertical spread and place combo order.
        Falls back to enter_live_position() if spread selector rejects.
        """
        direction_full = "CALL" if direction in ("C", "CALL") else "PUT" if direction in ("P", "PUT") else direction
        if direction_full not in ("CALL", "PUT"):
            console.print(f"[yellow][VerticalSpread] enter_spread_live_position: unknown direction {direction}, falling back[/yellow]")
            self.enter_live_position(direction, signal)
            return

        # ── [PositionGuard] Check position limits before spread selection ──
        allowed, reason = self._can_enter_position(direction_full, signal)
        if not allowed:
            console.print(f"[red]🚫 [VerticalSpread][LIVE] Entry blocked: {reason}[/red]")
            return

        from strategies.options.spread_selector import select_vertical_spread, build_combo_legs

        index_price = self.market_data.get("MTX", {}).get("close", 0.0) or signal.get("price_mtx", 0)
        if index_price <= 0:
            console.print("[yellow][VerticalSpread] No index price available for live spread, falling back to single-leg[/yellow]")
            self.enter_live_position(direction, signal)
            return

        tx_code = self.active_contracts.get("MTX", None)
        mtx_close = self.market_data.get("MTX", {}).get("close", index_price)
        option_chain = getattr(self, '_all_month_contracts', [])
        if not option_chain:
            console.print("[yellow][VerticalSpread] No option chain available for live spread, falling back to single-leg[/yellow]")
            self.enter_live_position(direction, signal)
            return

        # Build market_data dict for spread selector quotes
        md_for_spread = {}
        for c in option_chain:
            code = getattr(c, 'code', '')
            if code in self.market_data:
                md_for_spread[code] = self.market_data[code]

        spread, reason = select_vertical_spread(
            direction=direction_full,
            index_price=mtx_close,
            option_chain=option_chain,
            market_data=md_for_spread,
            width=self._spread_width,
        )

        if spread is None:
            console.print(f"[yellow][VerticalSpread][SKIP] {reason} — falling back to single-leg live entry[/yellow]")
            self.enter_live_position(direction, signal)
            return

        entry_lots = int(signal.get("entry_lots", self.base_lots)) if isinstance(signal, dict) else self.base_lots
        if entry_lots <= 0:
            console.print(f"[yellow][VerticalSpread] entry_lots={entry_lots}, skipping[/yellow]")
            return

        console.print(f"[bold green][VerticalSpread][LIVE] {spread.direction} spread: "
                      f"Buy {spread.long_leg.strike}{spread.long_leg.option_type[0]} "
                      f"Sell {spread.short_leg.strike}{spread.short_leg.option_type[0]} "
                      f"net_debit={spread.net_debit:.1f} max_profit={spread.max_profit:.1f}[/bold green]")

        # Place combo order via broker
        legs = build_combo_legs(spread)
        trade = self.broker.place_comboorder(
            legs=legs,
            price=spread.net_debit,
            quantity=entry_lots,
            action=sj.constant.Action.Buy,
            price_type='LMT',
        )

        if trade is None:
            console.print("[red]❌ [VerticalSpread] Live combo order returned None (margin?)[/red]")
            self._audit_signal("LIVE_SPREAD_ENTRY_SUBMITTED", direction_full, signal, "place_comboorder_returned_none")
            return

        self._audit_signal("LIVE_SPREAD_ENTRY_SUBMITTED", direction_full, signal, "")

        # Record lifecycle order via OrderManager
        if self.order_mgr:
            from core.order_management.order import OrderType, OrderSide
            lifecycle_order = self.order_mgr.create_order(
                symbol=f"VERTICAL_{spread.long_leg.strike}/{spread.short_leg.strike}",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=entry_lots,
                strategy=self.mode,
                comment=f"LIVE_SPREAD score={signal.get('score', 0.0):.1f}",
            )
            if lifecycle_order:
                self.order_mgr.attach_submission(
                    lifecycle_order.order_id,
                    broker_trade=trade,
                    broker_order_id=getattr(trade, "id", None),
                    seqno=getattr(trade, "seqno", None),
                    ordno=getattr(trade, "ordno", None),
                    raw_status="Submitted",
                )

        # Track position state
        self.position += entry_lots
        self.active_side = direction_full
        prev_cost = self.entry_price * (self.position - entry_lots) if self.position > entry_lots else 0
        self.entry_price = (prev_cost + spread.net_debit * entry_lots) / self.position if self.position > 0 else spread.net_debit
        self.entry_mtx_price = mtx_close
        self.entry_time = signal.get("timestamp") or self._current_strategy_time()
        self.has_tp1_hit = False
        self.stop_loss_price = spread.net_debit * (1 - self.stop_loss_pct)
        self.peak_premium = spread.net_debit
        self.replay_stats["entries"] += 1
        self._daily_entries = getattr(self, '_daily_entries', 0) + 1

        # Store spread metadata for exit decisions
        self._current_spread = {
            "long_strike": spread.long_leg.strike,
            "short_strike": spread.short_leg.strike,
            "option_type": spread.long_leg.option_type,
            "net_debit": spread.net_debit,
            "max_profit": spread.max_profit,
            "max_risk": spread.max_risk,
            "strike_width": spread.strike_width,
            "expiration": spread.expiration,
        }

        logger.info(
            "[VerticalSpread] LIVE ENTRY %s qty=%d net_debit=%.1f max_profit=%.1f",
            direction_full, entry_lots, spread.net_debit, spread.max_profit,
        )

        if self.dry_run_live_orders:
            self._simulate_dry_run_fill(trade, spread.long_leg.contract, "Buy", entry_lots)

    # ── [Vertical Spread v1] Live trading spread management ──
    def manage_spread_live_position(self, signal, option_chain=None):
        """
        Manage an open vertical spread position in live trading mode.
        Same logic as manage_spread_paper_position but calls live broker exit.
        Returns True if position was closed.
        """
        if self.position <= 0 or self._current_spread is None:
            return False

        now = signal.get("timestamp") if signal else self._current_strategy_time()
        sd = self._current_spread
        opt_type = sd["option_type"]

        option_chain = option_chain or getattr(self, '_all_month_contracts', [])
        from strategies.options.spread_selector import _contract_for_strike, _current_quote

        long_contract = _contract_for_strike(option_chain, sd["long_strike"], opt_type)
        short_contract = _contract_for_strike(option_chain, sd["short_strike"], opt_type)

        if not long_contract or not short_contract:
            return False

        md = {}
        lc = getattr(long_contract, 'code', '')
        sc = getattr(short_contract, 'code', '')
        if lc in self.market_data:
            md[lc] = self.market_data[lc]
        if sc in self.market_data:
            md[sc] = self.market_data[sc]

        long_bid, long_ask, long_mid = _current_quote(long_contract, md)
        short_bid, short_ask, short_mid = _current_quote(short_contract, md)

        if long_mid <= 0 or short_mid <= 0:
            return False

        spread_value = long_bid - short_ask
        entry_debit = sd["net_debit"]
        pnl_pts = spread_value - entry_debit
        pnl_pct = pnl_pts / entry_debit if entry_debit > 0 else 0
        holding_bars = getattr(self, '_spread_holding_bars', 0)

        # TAKE_PROFIT
        take_profit_target = entry_debit + sd["max_profit"] * 0.5
        if spread_value >= take_profit_target:
            console.print(f"[bold green][VerticalSpread][LIVE] TAKE_PROFIT spread_value={spread_value:.1f} "
                          f"pnl={pnl_pts:.1f}pts ({pnl_pct*100:.1f}%)[/bold green]")
            self._exit_spread_live_position("LIVE_SPREAD_TP", spread_value)
            return True

        # STOP_LOSS
        stop_loss_target = entry_debit * 0.55
        if spread_value <= stop_loss_target:
            console.print(f"[bold red][VerticalSpread][LIVE] STOP_LOSS spread_value={spread_value:.1f} "
                          f"pnl={pnl_pts:.1f}pts ({pnl_pct*100:.1f}%)[/bold red]")
            self._exit_spread_live_position("LIVE_SPREAD_SL", spread_value)
            return True

        # TIME_STOP
        if holding_bars >= 6 and pnl_pts <= 0:
            console.print(f"[bold yellow][VerticalSpread][LIVE] TIME_STOP held {holding_bars} bars, pnl={pnl_pts:.1f}pts[/bold yellow]")
            self._exit_spread_live_position("LIVE_SPREAD_TIME", spread_value)
            return True

        # EOD_EXIT
        if hasattr(self, 'm_cfg') and self.m_cfg.get('force_close_at_end', False):
            eod_state = self._get_eod_state(now)
            if eod_state["is_panic"]:
                console.print(f"[bold yellow][VerticalSpread][LIVE] EOD_PANIC exit spread_value={spread_value:.1f}[/bold yellow]")
                self._exit_spread_live_position("LIVE_SPREAD_EOD", spread_value)
                return True

        self._spread_holding_bars = holding_bars + 1
        return False

    def _exit_spread_live_position(self, action, exit_value):
        """
        Close a spread position in live mode via combo order.
        """
        if self.position <= 0 or self._current_spread is None:
            return

        qty = self.position
        sd = self._current_spread

        # Build reverse combo legs (sell long, buy back short)
        # Look up contracts from option_chain
        opt_type = sd["option_type"]
        option_chain = getattr(self, '_all_month_contracts', [])
        from strategies.options.spread_selector import _contract_for_strike

        long_contract = _contract_for_strike(option_chain, sd["long_strike"], opt_type)
        short_contract = _contract_for_strike(option_chain, sd["short_strike"], opt_type)

        if not long_contract or not short_contract:
            console.print("[red][VerticalSpread][LIVE] Cannot close — contracts not found in chain[/red]")
            return

        legs = [
            {"contract": long_contract, "action": sj.constant.Action.Sell},
            {"contract": short_contract, "action": sj.constant.Action.Buy},
        ]

        console.print(f"[cyan][VerticalSpread][LIVE] Closing spread: {action} qty={qty} exit_value={exit_value:.1f}[/cyan]")

        # Place combo order to close
        trade = self.broker.place_comboorder(
            legs=legs,
            price=exit_value,
            quantity=qty,
            action=sj.constant.Action.Sell,
            price_type='LMT',
        )
        if trade is None:
            console.print("[red]❌ [VerticalSpread][LIVE] Exit combo order returned None[/red]")
            return

        # Log exit via lifecycle manager
        if self.order_mgr:
            from core.order_management.order import OrderType, OrderSide
            lo = self.order_mgr.create_order(
                symbol="VERTICAL_SPREAD_EXIT",
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=qty,
                strategy=self.mode,
                comment=action,
            )

        # Compute PnL
        pnl_cash = (exit_value - sd['net_debit']) * self.pricing_cfg.get("point_value", 50)
        logger.info("[VerticalSpread] LIVE EXIT %s qty=%d entry=%.1f exit=%.1f pnl=%.1f",
                     action, qty, sd["net_debit"], exit_value, pnl_cash)

        if hasattr(self, 'log_trade'):
            self.log_trade(
                "VERTICAL_SPREAD_LIVE", self.active_side or "CALL",
                sd["net_debit"], exit_value, qty, action,
            )

        # Clear position state
        self.position = 0
        self.active_side = None
        self._current_spread = None
        self._spread_holding_bars = 0
        self.entry_price = 0.0
        self.entry_mtx_price = 0.0
        self.replay_stats["exits"] += 1

    def enter_paper_position(self, side, signal):
        signal_side = signal.get("side") if isinstance(signal, dict) else None
        if not signal_side or signal_side != side:
            console.print(f"[yellow]⚠️ enter_paper_position blocked: signal side mismatch/cleared (requested {side}, signal={signal_side})[/yellow]")
            return
        entry_lots = int(signal.get("entry_lots", self.base_lots)) if isinstance(signal, dict) else self.base_lots
        if entry_lots <= 0:
            console.print(f"[yellow]⚠️ enter_paper_position blocked: resolved entry_lots={entry_lots}[/yellow]")
            return
        # ── [PositionGuard] Use shared guard ──
        allowed, reason = self._can_enter_position(side, signal)
        if not allowed:
            console.print(f"[red]🚫 enter_paper_position blocked: {reason}[/red]")
            return
        if not self.spread_is_tradeable(side):
            console.print(f"[yellow]⚠️ enter_paper_position blocked: spread too wide for {side}[/yellow]")
            return

        # ── [DirectionLock] Block directional entry against regime bias ──
        _regime = str(getattr(self, 'latest_mid_trend', 'NORMAL')).upper()
        _score = float(getattr(self, 'latest_score', 0.0))
        # [Fix] Naming correction: positive score = bullish, negative score = bearish
        _is_bullish_bias = _score > 0
        _is_bearish_bias = _score < 0
        # side 'C' = long CALL (bullish), side 'P' = long PUT (bearish)
        _is_long_call = side in ("C", "CALL")
        _is_long_put = side in ("P", "PUT")
        # Block: regime is BEARish + score says bullish + trying to buy CALL (wait, no, block Call in Bear market)
        # Correct logic:
        # If market is BEAR/STRETCHED, and we have Bearish score, block CALL (Bullish) entry
        if _regime in ("BEAR", "STRETCHED") and _is_bearish_bias and _is_long_call:
            console.print(f"[bold yellow]🛑 [DirectionLock] Block CALL long in {_regime}/BEARISH (score={_score:.1f}) — direction conflict[/bold yellow]")
            return
        # If market is BULL/STRETCHED, and we have Bullish score, block PUT (Bearish) entry
        if _regime in ("BULL", "STRETCHED") and _is_bullish_bias and _is_long_put:
            console.print(f"[bold yellow]🛑 [DirectionLock] Block PUT long in {_regime}/BULLISH (score={_score:.1f}) — direction conflict[/bold yellow]")
            return
        # Also block: STRETCHED + any directional (keep only theta for stretched)
        if _regime == "STRETCHED" and (_is_long_call or _is_long_put):
            console.print(f"[bold yellow]🛑 [DirectionLock] Block directional {side} in STRETCHED — theta only[/bold yellow]")
            return

        quote = self.current_option_quote(side)
        entry_price = quote["ask"]
        if entry_price <= 0:
            console.print(f"[yellow]⚠️ enter_paper_position blocked: invalid entry price ({entry_price}) for {side}[/yellow]")
            return
        
        # 💡 GSD: Premium Cap Protection
        if entry_price > self.entry_premium_limit:
            console.print(f"[red]🚫 Entry blocked: premium {entry_price:.1f} exceeds limit {self.entry_premium_limit:.1f}[/red]")
            return
            
        if not self._paper_margin_check(entry_price, lots=entry_lots):
            return
        paper_order = self._record_paper_order(side, "BUY", entry_lots, entry_price, f"ENTRY score={signal.get('score', 0):.1f}")
        if self.order_mgr and paper_order is None:
            return
        self.position += entry_lots
        self.active_side = side
        # Average entry price for multiple positions
        prev_cost = self.entry_price * (self.position - entry_lots) if self.position > entry_lots else 0
        self.entry_price = (prev_cost + entry_price * entry_lots) / self.position if self.position > 0 else entry_price
        self.entry_mtx_price = signal.get("price_mtx", 0)
        self.entry_time = signal.get("timestamp") or self._current_strategy_time()
        self.has_tp1_hit = False
        self.stop_loss_price = entry_price * (1 - self.stop_loss_pct)
        self.peak_premium = entry_price
        self.replay_stats["entries"] += 1
        self._daily_entries = getattr(self, '_daily_entries', 0) + 1  # [Fix] track daily entry count
        
        # [GSD Phase B] Capture entry features
        self._entry_features = {
            "momentum": float(self.latest_score),
            "regime": str(self.latest_mid_trend),
            "iv": float(self.latest_iv or 0.25),
            "entry_price": float(entry_price)
        }
        
        sig_score = signal.get("score", 0)
        self.log_trade("PAPER_ENTRY", side, entry_price, f"score={sig_score:.1f}")

    def _record_paper_order(self, side_label, action, quantity, price, note="", strategy_override=None):
        """Helper to create a mock filled order for paper trades."""
        if not self.order_mgr:
            return None
            
        try:
            from core.order_management.order import Order, OrderStatus, OrderType, OrderSide
            now = datetime.datetime.now()
            
            # Map side: C/P are LONG strategies, THETA/SHORT are SELL entries
            is_short_entry = (side_label == "THETA" or side_label == "SHORT")
            
            if action == "BUY":
                side = OrderSide.SELL if is_short_entry else OrderSide.BUY
            else: # EXIT
                side = OrderSide.BUY if is_short_entry else OrderSide.SELL
                
            symbol = "TXO"
            contract = self.active_contracts.get(side_label)
            if contract: symbol = contract.code
            
            order = self.order_mgr.create_order(
                symbol=symbol,
                side=side,
                order_type=OrderType.MARKET,
                quantity=quantity,
                price=price,
                strategy=strategy_override or self.mode,
                comment=note,
            )
            self.order_mgr.submit(order, exchange_ordno=f"PAPER-{order.order_id}")
            self.order_mgr.apply_deal_fill(
                order.order_id,
                deal_id=f"deal-{order.order_id}",
                fill_price=price,
                fill_qty=quantity,
                fill_time=now,
                raw_payload={"paper": True, "side_label": side_label, "action": action, "note": note},
            )
            self._save_orders_file_wrapper()
            return order
        except Exception as e:
            console.print(f"[yellow]⚠️ Failed to record paper order: {e}[/yellow]")
            return None

    def enter_live_position(self, side, signal):
        self.submit_live_entry(side, signal, retries=0)

    def submit_live_entry(self, side, signal, retries):
        signal_side = signal.get("side") if isinstance(signal, dict) else None
        entry_lots = int(signal.get("entry_lots", self.base_lots)) if isinstance(signal, dict) else self.base_lots
        if entry_lots <= 0:
            self._audit_signal("LIVE_ENTRY_SUBMITTED", side, signal, "entry_lots_zero")
            return
        if not signal_side or signal_side != side:
            self._audit_signal("LIVE_ENTRY_SUBMITTED", side, signal, f"signal_side_mismatch:{signal_side}")
            return
        if self.pending_entry is not None:
            self._audit_signal("LIVE_ENTRY_SUBMITTED", side, signal, "pending_entry_exists")
            return
        if not self._dte_allows_entry(side, now=signal.get("timestamp")):
            self._audit_signal("LIVE_ENTRY_SUBMITTED", side, signal, "dte_blocked")
            return
        if not self.spread_is_tradeable(side):
            self._audit_signal("LIVE_ENTRY_SUBMITTED", side, signal, "spread_not_tradeable")
            return
        # 保證金檢查
        if not self._margin_sufficient():
            console.print(f"[red]⛔ 保證金不足，取消 {side} entry[/red]")
            self._audit_signal("LIVE_ENTRY_SUBMITTED", side, signal, "margin_insufficient")
            return
        self.sync_contract_quotes()
        contract = self.active_contracts.get(side)
        if contract is None:
            self._audit_signal("LIVE_ENTRY_SUBMITTED", side, signal, "no_contract_for_side")
            return
        lifecycle_order = None
        if self.order_mgr:
            from core.order_management.order import OrderType, OrderSide
            lifecycle_order = self.order_mgr.create_order(
                symbol=contract.code,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=entry_lots,
                strategy=self.mode,
                comment=f"ENTRY score={signal.get('score', 0.0):.1f}",
            )
        trade = self.broker.place_entry_order(contract, entry_lots)
        if trade is None:
            console.print("[red]❌ 下單未執行（可能保證金不足）[/red]")
            if lifecycle_order is not None:
                self.order_mgr.reject(lifecycle_order.order_id, "place_order_returned_none")
            self._audit_signal("LIVE_ENTRY_SUBMITTED", side, signal, "place_order_returned_none")
            return
        if lifecycle_order is not None:
            self.order_mgr.attach_submission(
                lifecycle_order.order_id,
                broker_trade=trade,
                broker_order_id=getattr(trade, "id", None),
                seqno=getattr(trade, "seqno", None),
                ordno=getattr(trade, "ordno", None),
                raw_status="Submitted",
            )
        self.pending_entry = {
            "side": side,
            "contract_code": contract.code,
            "entry_mtx_price": signal.get("price_mtx", 0.0),
            "signal_time": signal.get("timestamp"),
            "submitted_at": datetime.datetime.now(),
            "trade": trade,
            "order_id": lifecycle_order.order_id if lifecycle_order is not None else None,
            "requested_qty": entry_lots,
            "retries": retries,
        }
        self._audit_signal("LIVE_ENTRY_SUBMITTED", side, signal, "")
        sig_score = signal.get("score", 0.0)
        self.log_trade(
            "LIVE_ENTRY_SUBMITTED",
            side,
            getattr(contract, "ask_price", 0.0),
            f"score={sig_score:.1f} qty={entry_lots} trade={self.broker.describe_trade(trade)}",
        )
        if self.dry_run_live_orders:
            self._simulate_dry_run_fill(trade, contract, "Buy", entry_lots)

    def exit_paper_position(self, action, price, note=""):
        if self.position <= 0 or not self.active_side:
            return
        
        # ── [Rule 3] Reentrancy Guard ──
        if getattr(self, "_exit_in_progress", False):
            return
        self._exit_in_progress = True
        
        try:
            # ── [Rule 3] 1. Freeze Snapshot ──
            exit_qty = self.position
            exit_side = self.active_side
            exit_entry_p = self.entry_price
            
            # ── [Rule 3] 2. Establish order intent ──
            paper_order = self._record_paper_order(exit_side, "SELL", exit_qty, price, f"{action} {note}".strip())
            if paper_order is None:
                return  # Order failed, keep position for next tick retry
                
            # ── [Rule 3] 3. Update SSOT immediately AFTER order accepted ──
            self.position = 0
            self.active_side = None
            self.entry_price = 0.0
            self.entry_mtx_price = 0.0
            self.entry_time = None
            self.has_tp1_hit = False
            self.stop_loss_price = 0.0
            self.peak_premium = 0.0
            
            # ── [Rule 3] 4. Side Effects (IO/Logging) ──
            self.log_trade(action, exit_side, price, note, quantity=exit_qty, entry_price_override=exit_entry_p)
            
            self.cooldown_until = self.cooldown_bars
            self.replay_stats["exits"] += 1
            
        finally:
            self._exit_in_progress = False

    def apply_paper_tp1(self, price, note=""):
        if self.position <= 0 or not self.active_side:
            return False
        
        # ── [Rule 3] Reentrancy Guard ──
        if getattr(self, "_exit_in_progress", False):
            return False
        self._exit_in_progress = True
        
        try:
            # ── [Rule 3] 1. Freeze Snapshot ──
            exit_side = self.active_side
            exit_qty = min(1, self.position)
            exit_entry_price = self.entry_price
            mode_label = self.status_mode_label()
            
            # ── [Rule 3] 2. Update Position FIRST (Partial Exit) ──
            self.position = max(0, self.position - exit_qty)
            self.has_tp1_hit = True
            self.replay_stats["tp1_hits"] += 1
            
            # If fully closed after partial exit, clear metadata
            if self.position == 0:
                self.active_side = None
                self.entry_price = 0.0
                self.entry_mtx_price = 0.0
                self.entry_time = None
                self.stop_loss_price = 0.0
                self.peak_premium = 0.0
                self.cooldown_until = self.cooldown_bars
                self.replay_stats["exits"] += 1
                self.has_tp1_hit = False

            # ── [Rule 3] 3. Side Effects (IO/Logging) ──
            paper_order = self._record_paper_order(exit_side, "SELL", exit_qty, price, f"{mode_label}_TP1 {note}".strip())
            
            self.log_trade(f"{mode_label}_TP1", exit_side, price, note, quantity=exit_qty, entry_price_override=exit_entry_price)
            
            return True
            
        finally:
            self._exit_in_progress = False

    def exit_live_position(self, action, note="", quantity=None):
        self.submit_live_exit(action, note=note, quantity=quantity, retries=0)

    def submit_live_exit(self, action, note="", quantity=None, retries=0):
        if not self.active_side or self.position <= 0 or self.pending_exit_qty > 0:
            self._audit_signal("LIVE_EXIT_SUBMITTED", self.active_side or "", {"action": action, "note": note}, "no_position_or_pending_exit")
            return
        self.sync_contract_quotes()
        contract = self.active_contracts.get(self.active_side)
        if contract is None:
            self._audit_signal("LIVE_EXIT_SUBMITTED", self.active_side, {"action": action, "note": note}, "no_contract")
            return
        exit_quantity = min(self.position, quantity or self.position)
        bid = self.current_option_quote(self.active_side)["bid"]
        lifecycle_order = None
        if self.order_mgr:
            from core.order_management.order import OrderType, OrderSide
            lifecycle_order = self.order_mgr.create_order(
                symbol=contract.code,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=exit_quantity,
                strategy=self.mode,
                comment=action,
            )
        trade = self.broker.place_exit_order(contract, exit_quantity, bid_price=bid)
        if trade is None:
            console.print("[red]❌ 出場下單未執行[/red]")
            if lifecycle_order is not None:
                self.order_mgr.reject(lifecycle_order.order_id, "place_order_returned_none")
            self._audit_signal("LIVE_EXIT_SUBMITTED", self.active_side, {"action": action, "note": note}, "place_order_returned_none")
            return
        if lifecycle_order is not None:
            self.order_mgr.attach_submission(
                lifecycle_order.order_id,
                broker_trade=trade,
                broker_order_id=getattr(trade, "id", None),
                seqno=getattr(trade, "seqno", None),
                ordno=getattr(trade, "ordno", None),
                raw_status="Submitted",
            )
        self.pending_exit_qty = exit_quantity
        self.pending_exit_reason = action
        self.pending_exit_trade = {
            "submitted_at": datetime.datetime.now(),
            "trade": trade,
            "order_id": lifecycle_order.order_id if lifecycle_order is not None else None,
            "quantity": exit_quantity,
            "retries": retries,
        }
        self._audit_signal("LIVE_EXIT_SUBMITTED", self.active_side, {"action": action, "note": note}, "")
        self.log_trade(action, self.active_side, self.current_option_quote(self.active_side)["bid"], f"{note} trade={self.broker.describe_trade(trade)}".strip())
        if self.dry_run_live_orders:
            self._simulate_dry_run_fill(trade, contract, "Sell", exit_quantity)

    def _audit_signal(self, signal_type, side, signal_data, rejection_reason):
        """記錄信號審計軌跡到 CSV（與期貨系統共用格式）"""
        from pathlib import Path
        log_dir = Path("logs/market_data")
        log_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.datetime.now().strftime("%Y%m%d")
        audit_file = log_dir / f"MTX_{date_str}_signals_audit.csv"
        ts = signal_data.get("timestamp", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")) if isinstance(signal_data, dict) else datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        score = signal_data.get("score", 0) if isinstance(signal_data, dict) else 0
        header = not audit_file.exists()
        record = {
            "timestamp": ts,
            "signal": signal_type,
            "side": side,
            "score": score,
            "rejection": rejection_reason,
            "note": signal_data.get("note", "") if isinstance(signal_data, dict) else "",
        }
        pd.DataFrame([record]).to_csv(audit_file, mode='a', index=False, header=header)

    def _simulate_dry_run_fill(self, trade, contract, action, quantity):
        msg = {
            "action": action,
            "price": getattr(getattr(trade, "order", None), "price", 0.0),
            "quantity": quantity,
            "code": contract.code,
        }
        self.on_order_event("MOCK_FILL", msg)

    def _order_age_secs(self, order_info):
        if not order_info or "submitted_at" not in order_info:
            return 0
        return (datetime.datetime.now() - order_info["submitted_at"]).total_seconds()

    def _clear_stale_entry(self, note):
        if not self.pending_entry:
            return
        self.log_trade("LIVE_ENTRY_CLEARED", self.pending_entry["side"], 0.0, note)
        self.pending_entry = None

    def _clear_stale_exit(self, note):
        if not self.pending_exit_trade:
            return
        self.log_trade("LIVE_EXIT_CLEARED", self.active_side or "", 0.0, note)
        self.pending_exit_trade = None
        self.pending_exit_qty = 0
        self.pending_exit_reason = None

    def refresh_live_orders(self):
        if not self.live_trading or self.broker is None:
            return
        self.broker.refresh_status(account=getattr(self.api, "futopt_account", None))
        self._reconcile_theta_combo_orders(source="combo_poll", reason="runtime_refresh")
        if self.pending_entry and self._order_age_secs(self.pending_entry) >= self.order_timeout_secs:
            pending = dict(self.pending_entry)
            trade = pending.get("trade")
            self.broker.cancel_order(trade)
            self._clear_stale_entry("entry timeout cancelled")
            if pending.get("retries", 0) < self.max_order_retries:
                retry_signal = {
                    "score": self.last_signal.get("score", 0.0) if self.last_signal else 0.0,
                    "price_mtx": pending.get("entry_mtx_price", 0.0),
                }
                self.submit_live_entry(pending["side"], retry_signal, retries=pending.get("retries", 0) + 1)
                self.log_trade("LIVE_ENTRY_RETRY", pending["side"], 0.0, f"retry={pending.get('retries', 0) + 1}")
        if self.pending_exit_trade and self._order_age_secs(self.pending_exit_trade) >= self.order_timeout_secs:
            pending = dict(self.pending_exit_trade)
            trade = pending.get("trade")
            self.broker.cancel_order(trade)
            reason = self.pending_exit_reason
            self._clear_stale_exit("exit timeout cancelled")
            if pending.get("retries", 0) < self.max_order_retries and self.active_side and self.position > 0:
                self.submit_live_exit(reason or "LIVE_EXIT_RESUBMITTED", note=f"retry={pending.get('retries', 0) + 1}", quantity=pending.get("quantity"), retries=pending.get("retries", 0) + 1)

    def _margin_sufficient(self, combo_entry=None):
        """Check account margin before live entry."""
        if not self.api:
            return True
        try:
            margin = self.api.margin(self.api.futopt_account)
            equity = margin.equity
            reserve_pct = 0.20
            available = equity * (1 - reserve_pct)
            if combo_entry is not None and self._theta_gang is not None:
                required = self._theta_gang.live_combo_margin_required(combo_entry)
            else:
                # 選擇權買方需要權利金，用 order_margin_premium 或 fallback 估算
                required = margin.order_margin_premium if margin.order_margin_premium > 0 else 10000
            if available < required:
                console.print(f"[red]Margin check: equity={equity:.0f} available={available:.0f} < required={required:.0f}[/red]")
                return False
            return True
        except Exception as e:
            console.print(f"[yellow]Margin check failed: {e} — allowing order[/yellow]")
            return True

    def _normalize_delivery_date(self, delivery_date):
        if not delivery_date:
            return None
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.datetime.strptime(str(delivery_date), fmt).date()
            except ValueError:
                continue
        return None

    def _option_right_matches(self, contract, side):
        option_right = str(getattr(contract, "option_right", "")).upper()
        expected = str(side).upper()
        return option_right.startswith(expected) or option_right.endswith("CALL" if expected == "C" else "PUT")

    def _resolve_theta_combo_contract(self, leg):
        contract = getattr(leg, "contract", None)
        if contract is not None:
            return contract

        expected_side = str(getattr(leg, "side", "")).upper()
        reference_contract = self.active_contracts.get(expected_side)
        desired_delivery = self._normalize_delivery_date(getattr(reference_contract, "delivery_date", None)) if reference_contract else None
        try:
            contracts = list(self.api.Contracts.Options["TXO"]) if self.api and hasattr(self.api, "Contracts") else []
        except Exception:
            contracts = []

        for candidate in contracts:
            if float(getattr(candidate, "strike_price", 0) or 0) != float(getattr(leg, "strike", 0) or 0):
                continue
            if not self._option_right_matches(candidate, expected_side):
                continue
            if desired_delivery is not None:
                candidate_delivery = self._normalize_delivery_date(getattr(candidate, "delivery_date", None))
                if candidate_delivery != desired_delivery:
                    continue
            leg.contract = candidate
            return candidate
        return None

    def _build_theta_combo_legs(self, legs, *, reverse_actions=False):
        combo_legs = []
        for leg in legs:
            contract = self._resolve_theta_combo_contract(leg)
            if contract is None:
                raise ValueError(f"missing_combo_contract:{getattr(leg, 'side', '?')}{getattr(leg, 'strike', '?')}")
            action = str(getattr(leg, "action", "")).upper()
            if reverse_actions:
                action = "BUY" if action == "SELL" else "SELL"
            combo_legs.append(
                {
                    "contract": contract,
                    "action": action,
                    "side": getattr(leg, "side", None),
                    "strike": float(getattr(leg, "strike", 0) or 0),
                    "code": getattr(contract, "code", None),
                }
            )
        return combo_legs

    def _block_live_theta_combo(self, strategy, reason, payload=None):
        console.print(f"[yellow]⚠️ [ThetaGang] Live {strategy} blocked: {reason}[/yellow]")
        self._audit_signal("THETA_LIVE_BLOCKED", strategy or "THETA", payload or {}, reason)
        return False

    def _create_theta_combo_lifecycle_order(self, strategy, combo_legs, quantity, price, order_side, comment):
        if not self.order_mgr:
            return None
        from core.order_management.order import OrderType
        return self.order_mgr.create_order(
            symbol="TXO-COMBO",
            side=order_side,
            order_type=OrderType.LIMIT,
            quantity=int(quantity),
            price=float(price),
            strategy=self.mode,
            comment=comment,
            truth_source="broker_combo",
            combo_strategy=strategy,
            combo_legs=[
                {
                    "code": leg.get("code"),
                    "action": leg.get("action"),
                    "side": leg.get("side"),
                    "strike": leg.get("strike"),
                }
                for leg in combo_legs
            ],
        )

    def _infer_theta_combo_phase(self, order):
        if self.pending_theta_combo and self.pending_theta_combo.get("order_id") == order.order_id:
            return self.pending_theta_combo.get("phase")
        comment = str(getattr(order, "comment", "") or "").upper()
        if "EXIT" in comment:
            return "exit"
        if "ENTRY" in comment:
            return "entry"
        return "exit" if getattr(order.side, "value", "") == "buy" else "entry"

    def _remember_pending_theta_combo(self, order):
        self.pending_theta_combo = {
            "phase": self._infer_theta_combo_phase(order),
            "strategy": order.combo_strategy or getattr(getattr(self._theta_gang, "position", None), "strategy", None),
            "submitted_at": order.submitted_at or datetime.datetime.now(),
            "trade": None,
            "order_id": order.order_id,
            "requested_qty": max(1, int(order.quantity)),
            "combo_legs": list(order.combo_legs or []),
            "limit_price": float(order.price or 0.0),
            "truth_source": order.truth_source or "broker_combo",
        }
        return self.pending_theta_combo

    def _build_theta_entry_info_from_order(self, order):
        from strategies.options.theta_gang import SpreadLeg, calculate_vertical_max_loss

        legs = [
            SpreadLeg(
                side=str(leg.get("side", "")),
                strike=float(leg.get("strike", 0) or 0.0),
                action=str(leg.get("action", "")),
            )
            for leg in (order.combo_legs or [])
        ]
        net_credit = float(order.avg_fill_price or order.price or 0.0)
        return {
            "strategy": order.combo_strategy or getattr(self._theta_gang, "strategy", "bull_put_spread"),
            "legs": legs,
            "net_credit": net_credit,
            "max_loss": calculate_vertical_max_loss(legs, net_credit),
            "quantity": int(order.quantity or 1),
        }

    def _clear_theta_combo_runtime(self):
        self.pending_theta_combo = None
        self._theta_bars_held = 0
        self._theta_release_confirm_count = 0
        self._theta_release_last_bar_ts = None

    def _apply_theta_combo_fill_truth(self, order, *, source="", reason=""):
        if not order or order.symbol != "TXO-COMBO" or order.truth_source != "broker_combo":
            return False

        phase = self._infer_theta_combo_phase(order)
        fill_price = float(order.avg_fill_price or order.price or 0.0)
        fill_time = order.filled_at or datetime.datetime.now()

        if order.status.value == "filled":
            if phase == "entry":
                if self._theta_gang and not (self._theta_gang.position and self._theta_gang.position.is_open):
                    entry_info = self._build_theta_entry_info_from_order(order)
                    pos = self._theta_gang.open_position(entry_info)
                    self.position = pos.quantity
                    self.active_side = "THETA"
                    self.entry_price = fill_price
                    self.entry_time = fill_time
                    self.stop_loss_price = fill_price * (1 + self.stop_loss_pct) if fill_price > 0 else 0.0
                    self.peak_premium = fill_price
                    self.has_tp1_hit = False
                    self.log_trade(
                        "THETA_LIVE_ENTRY_FILLED",
                        "THETA",
                        fill_price,
                        f"strategy={pos.strategy} source={source} reason={reason}".strip(),
                        quantity=pos.quantity,
                    )
                    self._save_orders_file_wrapper()
                self._clear_theta_combo_runtime()
                return True

            if phase == "exit":
                current_pos = getattr(self._theta_gang, "position", None)
                if current_pos and current_pos.is_open:
                    closed = self._theta_gang.close_position()
                    self.log_trade(
                        "THETA_LIVE_EXIT_FILLED",
                        "THETA",
                        fill_price,
                        f"strategy={closed.strategy} source={source} reason={reason}".strip(),
                        quantity=closed.quantity,
                    )
                self.position = 0
                self.active_side = None
                self.entry_price = 0.0
                self.entry_time = None
                self.stop_loss_price = 0.0
                self.peak_premium = 0.0
                self.has_tp1_hit = False
                self.cooldown_until = self.cooldown_bars
                self._save_orders_file_wrapper()
                self._clear_theta_combo_runtime()
                return True

        if order.is_completed():
            self._clear_theta_combo_runtime()
            self._save_orders_file_wrapper()
            return False

        self._remember_pending_theta_combo(order)
        return False

    def _reconcile_theta_combo_orders(self, *, combo_trades=None, source="combo_poll", reason=""):
        if not self.live_trading or not self.order_mgr or not self.broker:
            return {"combo_trades": 0, "matched": 0, "fills_applied": 0}

        account = getattr(self.api, "futopt_account", None) if self.api else None
        if combo_trades is None:
            if hasattr(self.broker, "list_combo_status_trades"):
                combo_trades = list(self.broker.list_combo_status_trades(account=account))
            else:
                if hasattr(self.broker, "update_combostatus"):
                    self.broker.update_combostatus(account=account)
                combo_trades = list(self.broker.list_combotrades()) if hasattr(self.broker, "list_combotrades") else []

        matched = 0
        fills_applied = 0
        for combo_trade in combo_trades or []:
            result = self.order_mgr.reconcile_combo_trade_snapshot(
                combo_trade=combo_trade,
                source=source,
                reason=reason,
                create_if_missing=(source == "combo_startup"),
            )
            if not result.get("matched"):
                continue
            matched += 1
            fills_applied += int(result.get("fills_added", 0))
            order = self._find_lifecycle_order(result.get("order_id"))
            if order is not None:
                self._apply_theta_combo_fill_truth(order, source=source, reason=reason)
        return {"combo_trades": len(combo_trades or []), "matched": matched, "fills_applied": fills_applied}

    def _startup_recover_live_order_state(self):
        recovered = {"filled": 0, "open": 0, "failed": 0}
        if self.live_trading:
            recovered = self._recover_live_orders_from_broker()
        if not self.live_trading or (recovered.get("filled", 0) + recovered.get("open", 0) == 0):
            self._recover_orders_from_ledger()
        return recovered

    def _submit_live_theta_combo_entry(self, entry_info):
        if self.pending_theta_combo is not None:
            return self._block_live_theta_combo(entry_info.get("strategy"), "pending_theta_combo_exists", entry_info)
        valid, payload = self._theta_gang.validate_live_combo(entry_info)
        if not valid:
            return self._block_live_theta_combo(entry_info.get("strategy"), payload, entry_info)
        if not self._margin_sufficient(combo_entry=payload):
            return self._block_live_theta_combo(payload["strategy"], "margin_insufficient", payload)
        try:
            combo_legs = self._build_theta_combo_legs(payload["legs"], reverse_actions=False)
        except ValueError as exc:
            return self._block_live_theta_combo(payload["strategy"], str(exc), payload)

        from core.order_management.order import OrderSide

        lifecycle_order = self._create_theta_combo_lifecycle_order(
            payload["strategy"],
            combo_legs,
            payload["quantity"],
            payload["net_credit"],
            OrderSide.SELL,
            f"THETA_LIVE_ENTRY strategy={payload['strategy']}",
        )
        trade = self.broker.place_comboorder(
            combo_legs,
            price=payload["net_credit"],
            quantity=payload["quantity"],
            action=sj.constant.Action.Sell,
        )
        if trade is None:
            if lifecycle_order is not None:
                self.order_mgr.reject(lifecycle_order.order_id, "place_comboorder_returned_none")
            return self._block_live_theta_combo(payload["strategy"], "place_comboorder_returned_none", payload)
        if lifecycle_order is not None:
            self.order_mgr.attach_submission(
                lifecycle_order.order_id,
                broker_trade=trade,
                broker_order_id=getattr(trade, "id", None),
                seqno=getattr(trade, "seqno", None),
                ordno=getattr(trade, "ordno", None),
                raw_status="Submitted",
                source="broker_combo_submit",
            )
        self.pending_theta_combo = {
            "phase": "entry",
            "strategy": payload["strategy"],
            "submitted_at": datetime.datetime.now(),
            "trade": trade,
            "order_id": lifecycle_order.order_id if lifecycle_order is not None else None,
            "requested_qty": payload["quantity"],
            "combo_legs": combo_legs,
            "limit_price": payload["net_credit"],
            "truth_source": "broker_combo",
        }
        self._audit_signal("THETA_LIVE_ENTRY_SUBMITTED", "THETA", payload, "")
        self.log_trade(
            "THETA_LIVE_ENTRY_SUBMITTED",
            "THETA",
            payload["net_credit"],
            f"strategy={payload['strategy']} qty={payload['quantity']} trade={self.broker.describe_trade(trade)}",
        )
        return True

    def _submit_live_theta_combo_exit(self, exit_info):
        if self.pending_theta_combo is not None:
            strategy = getattr(getattr(self._theta_gang, "position", None), "strategy", "theta")
            return self._block_live_theta_combo(strategy, "pending_theta_combo_exists", exit_info)
        position = getattr(self._theta_gang, "position", None)
        if position is None or not position.is_open:
            return self._block_live_theta_combo("theta", "no_open_theta_position", exit_info)

        valid, payload = self._theta_gang.validate_live_combo(
            {
                "strategy": position.strategy,
                "legs": position.legs,
                "net_credit": position.net_credit,
                "max_loss": position.max_loss,
                "quantity": position.quantity,
            }
        )
        if not valid:
            return self._block_live_theta_combo(position.strategy, payload, exit_info)
        try:
            combo_legs = self._build_theta_combo_legs(payload["legs"], reverse_actions=True)
        except ValueError as exc:
            return self._block_live_theta_combo(position.strategy, str(exc), exit_info)

        from core.order_management.order import OrderSide

        limit_price = float(exit_info.get("current_value", 0.0) or 0.0)
        lifecycle_order = self._create_theta_combo_lifecycle_order(
            position.strategy,
            combo_legs,
            payload["quantity"],
            limit_price,
            OrderSide.BUY,
            f"THETA_LIVE_EXIT strategy={position.strategy} reason={exit_info.get('reason', '')}",
        )
        trade = self.broker.place_comboorder(
            combo_legs,
            price=limit_price,
            quantity=payload["quantity"],
            action=sj.constant.Action.Buy,
        )
        if trade is None:
            if lifecycle_order is not None:
                self.order_mgr.reject(lifecycle_order.order_id, "place_comboorder_returned_none")
            return self._block_live_theta_combo(position.strategy, "place_comboorder_returned_none", exit_info)
        if lifecycle_order is not None:
            self.order_mgr.attach_submission(
                lifecycle_order.order_id,
                broker_trade=trade,
                broker_order_id=getattr(trade, "id", None),
                seqno=getattr(trade, "seqno", None),
                ordno=getattr(trade, "ordno", None),
                raw_status="Submitted",
                source="broker_combo_submit",
            )
        self.pending_theta_combo = {
            "phase": "exit",
            "strategy": position.strategy,
            "submitted_at": datetime.datetime.now(),
            "trade": trade,
            "order_id": lifecycle_order.order_id if lifecycle_order is not None else None,
            "requested_qty": payload["quantity"],
            "combo_legs": combo_legs,
            "limit_price": limit_price,
            "truth_source": "broker_combo",
            "reason": exit_info.get("reason"),
        }
        self._audit_signal("THETA_LIVE_EXIT_SUBMITTED", "THETA", exit_info, "")
        self.log_trade(
            "THETA_LIVE_EXIT_SUBMITTED",
            "THETA",
            limit_price,
            f"strategy={position.strategy} qty={payload['quantity']} reason={exit_info.get('reason', '')} trade={self.broker.describe_trade(trade)}",
        )
        return True

    # ──────────────────────────────────────────────────────────────
    # Capital Calculation Utilities
    # 2026-05-25 Hermes Agent: extract from _paper_margin_check for reuse
    # ──────────────────────────────────────────────────────────────

    def _calc_required_margin(self, entry_price, lots=1):
        """計算 option 買方資金需求 = 權利金 × 點值 × 張數"""
        pv = self.pricing_cfg.get("point_value", 50)
        if entry_price > 0:
            return entry_price * pv * lots
        return 10000 * lots  # fallback for zero/invalid price

    def _calc_available_capital(self):
        """計算可用資金 = (初始資金 + 已實現損益) × (1 - 保留比例)

        保守估計：只用已實現 PnL，不計未實現損益。
        """
        risk_cfg = self.full_cfg.get("risk_mgmt", {})
        initial_capital = float(risk_cfg.get("initial_capital", 100000))
        reserve_pct = 0.20

        realized_pnl = 0
        if self.ledger_path.exists():
            try:
                prev = pd.read_csv(self.ledger_path)
                realized_pnl = pd.to_numeric(prev["PnL"], errors="coerce").fillna(0).sum()
            except Exception:
                pass

        return (initial_capital + realized_pnl) * (1 - reserve_pct)

    def _paper_margin_check(self, entry_price, lots=None):
        """
        Paper mode margin check — enforce capital limits.
        Buyer: need premium × point_value × lots
        Seller (spread): need wing_width × point_value × lots

        2026-05-25 Hermes Agent: add existing position capital consumption.
        Previously only checked new entry cost, ignoring accumulated positions.
        Now total_required = new_entry_cost + existing_position_cost.
        """
        available = self._calc_available_capital()

        lots = int(lots or self.base_lots)

        # New entry cost
        new_required = self._calc_required_margin(entry_price, lots)

        # Existing position capital consumption
        existing_required = self._calc_required_margin(
            self.entry_price, self.position
        ) if self.position > 0 else 0

        total_required = new_required + existing_required

        if available < total_required:
            console.print(
                f"[red]⛔ Paper margin: available={available:.0f} < total_required={total_required:.0f} "
                f"(new={new_required:.0f} + existing={existing_required:.0f})[/red]"
            )
            return False
        console.print(
            f"[dim]Paper margin OK: available={available:.0f} >= total_required={total_required:.0f} "
            f"(new={new_required:.0f} + existing={existing_required:.0f})[/dim]"
        )
        return True

    def _recover_position_from_api(self):
        """Recover open position on restart. Live: from broker API. Paper: from ledger file."""
        # Paper mode: check ledger for unclosed position
        if not self.live_trading:
            try:
                import csv
                ledger_path = self.ledger_path
                if os.path.exists(ledger_path):
                    with open(ledger_path) as f:
                        rows = list(csv.DictReader(f))
                    if rows:
                        # 從最後一筆交易計算當前持倉
                        # 注意: TP1 會減少持倉，必須納入計算
                        current_qty = 0
                        last_side = None
                        last_entry_price = 0
                        last_note = ""
                        total_cost = 0
                        last_ts = None
                        for i, r in enumerate(rows):
                            action = str(r.get("Action", "")).upper()
                            try:
                                qty = int(float(r.get("Quantity", 0) or 0))
                                price = float(r.get("Price", 0) or 0)
                            except (ValueError, TypeError):
                                continue

                            if "ENTRY" in action:
                                # For Theta/Short, we use the specific entry price, not cumulative
                                total_cost = price * qty 
                                current_qty = qty
                                last_side = r.get("Side")
                                last_entry_price = price
                                last_ts = r.get("Timestamp")
                                last_note = r.get("Note", "")
                            elif "TP1" in action:
                                current_qty = max(0, current_qty - qty)
                            elif any(kw in action for kw in ["EXIT", "PANIC", "TRAIL", "TIME", "REVERSAL", "TRAP", "EOD"]):
                                current_qty = 0
                                last_side = None
                                total_cost = 0
                                last_entry_price = 0
                                last_note = ""

                        if current_qty > 0 and last_side:
                            # ── Capital check before position recovery ──
                            # 2026-05-25 Hermes Agent: prevent recovery of positions
                            # that exceed available capital (e.g. after realized losses).
                            # If capital is insufficient, force-close instead of restoring.
                            recovery_required = self._calc_required_margin(last_entry_price, current_qty)
                            recovery_available = self._calc_available_capital()
                            if recovery_available < recovery_required:
                                console.print(
                                    f"[red]⛔ [RECOVERY] Paper position {last_side} qty={current_qty} @ {last_entry_price} "
                                    f"requires {recovery_required:.0f} but only {recovery_available:.0f} available — "
                                    f"force closing at open[/red]"
                                )
                                self.log_trade(
                                    "RECOVERY_FORCE_CLOSE_EXIT", last_side, last_entry_price,
                                    note=(
                                        f"capital_check_failed "
                                        f"available={recovery_available:.0f} "
                                        f"required={recovery_required:.0f}"
                                    ),
                                    quantity=current_qty,
                                )
                                current_qty = 0
                                last_side = None
                                last_entry_price = 0

                            self.position = current_qty
                            self.active_side = last_side
                            self.entry_price = last_entry_price
                            
                            # Parse original timestamp for display
                            try:
                                recovery_ts = datetime.datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
                            except:
                                recovery_ts = datetime.datetime.now()

                            # SL logic depends on side
                            if self.active_side in ["C", "P"]:
                                self.stop_loss_price = self.entry_price * (1 - self.stop_loss_pct)
                            else: # Theta/Short
                                self.stop_loss_price = self.entry_price * (1 + self.stop_loss_pct)
                                
                            self.peak_premium = self.entry_price
                            
                            # [GSD Fix] 把恢復的部位加入 OrderManager
                            if self.order_mgr:
                                from core.order_management.order import Order, OrderStatus, OrderType, OrderSide
                                symbol = "TXO"
                                contract = self.active_contracts.get(self.active_side)
                                if contract: symbol = contract.code
                                
                                # Determine side: Theta/Short strategies are SELL entry
                                is_short = (self.active_side == "THETA" or self.active_side == "SHORT")
                                side = OrderSide.SELL if is_short else OrderSide.BUY
                                
                                rec_order = Order(
                                    symbol=symbol,
                                    side=side,
                                    order_type=OrderType.MARKET,
                                    quantity=self.position,
                                    price=self.entry_price,
                                    order_id=f"RECOV-{recovery_ts.strftime('%H%M%S')}",
                                    strategy="RECOVERED"
                                )
                                rec_order.status = OrderStatus.FILLED
                                rec_order.filled_quantity = self.position
                                rec_order.avg_fill_price = self.entry_price
                                rec_order.filled_at = recovery_ts
                                rec_order.created_at = recovery_ts
                                self.order_mgr.completed.append(rec_order)
                                self._save_orders_file_wrapper()

                            if self.active_side == "THETA" and self._theta_gang:
                                self._theta_gang.restore_position(last_note, timestamp=recovery_ts, quantity=current_qty)
                                
                            console.print(f"[bold cyan]♻️ Recovered paper position from ledger: {self.active_side} qty={self.position} @ {self.entry_price}[/bold cyan]")
                            return
            except Exception as e:
                console.print(f"[yellow]Paper position recovery failed: {e}[/yellow]")
            return
        # Live mode: from broker API
        if not self.api:
            return
        try:
            positions = self.api.list_positions(self.api.futopt_account)
            for p in positions:
                code = getattr(p, 'code', '')
                for key in ['C', 'P']:
                    con = self.active_contracts.get(key)
                    if con and code == getattr(con, 'code', ''):
                        self.position = p.quantity
                        self.active_side = key
                        self.entry_price = float(p.price)
                        console.print(f"[bold cyan]♻️ Recovered position: {key} qty={p.quantity} @ {p.price}[/bold cyan]")
                        return
        except Exception as e:
            console.print(f"[yellow]Position recovery failed: {e}[/yellow]")

    def _recover_live_orders_from_broker(self):
        """Recover live order lifecycle state directly from broker APIs on startup."""
        if not self.order_mgr or not self.live_trading or not self.broker:
            return {"filled": 0, "open": 0, "failed": 0}

        account = getattr(self.api, "futopt_account", None) if self.api else None
        try:
            if hasattr(self.broker, "list_combo_status_trades"):
                combo_trades = list(self.broker.list_combo_status_trades(account=account))
            else:
                if hasattr(self.broker, "update_combostatus"):
                    self.broker.update_combostatus(account=account)
                combo_trades = list(self.broker.list_combotrades()) if hasattr(self.broker, "list_combotrades") else []
            open_orders = list(self.broker.list_open_orders(account=account))
            filled_trades = list(self.broker.list_trades(account=account))
            recovered = self.order_mgr.recover_from_api(
                combo_trades=combo_trades,
                filled_trades=filled_trades,
                open_orders=open_orders,
                source="combo_startup",
                reason="options_live_startup",
            )
            self._reconcile_theta_combo_orders(
                combo_trades=combo_trades,
                source="combo_startup",
                reason="options_live_startup",
            )
            recovered_total = recovered.get("filled", 0) + recovered.get("open", 0)
            if recovered_total > 0:
                console.print(
                    f"[bold cyan]♻️ Recovered live orders from broker: "
                    f"filled={recovered.get('filled', 0)} open={recovered.get('open', 0)}[/bold cyan]"
                )
                self._save_orders_file_wrapper()
            return recovered
        except Exception as e:
            console.print(f"[yellow]Live order recovery from broker failed: {e}[/yellow]")
            return {"filled": 0, "open": 0, "failed": 1}

    def _recover_orders_from_ledger(self):
        """Recover all orders from ledger CSV to rebuild OrderManager state on startup."""
        if not self.order_mgr:
            return
        
        try:
            import csv
            from core.order_management.order import Order, OrderStatus, OrderType, OrderSide
            
            if not os.path.exists(self.ledger_path):
                console.print("[dim]No ledger file to recover orders from[/dim]")
                return
            
            with open(self.ledger_path) as f:
                rows = list(csv.DictReader(f))
            
            if not rows:
                return
            
            recovered_count = 0
            for row in rows:
                try:
                    action = row.get("Action", "")
                    side_label = row.get("Side", "")
                    price = float(row.get("Price", 0))
                    quantity = int(row.get("Quantity", 0) or 1)
                    timestamp_str = row.get("Timestamp", "")
                    note = row.get("Note", "")
                    
                    # Parse timestamp
                    try:
                        ts = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                    except:
                        ts = datetime.datetime.now()
                    
                    # Determine if this is an entry or exit
                    is_entry = "ENTRY" in action
                    is_exit = any(kw in action for kw in ["EXIT", "TP1", "TRAIL", "TIME", "REVERSAL", "TRAP", "EOD"])
                    
                    if not (is_entry or is_exit):
                        continue  # Skip non-trade actions (retries, etc.)
                    
                    # Determine OrderSide based on side_label and action
                    # C/P are LONG strategies, THETA/SHORT/iron_condor are SELL entries
                    is_short_strategy = (side_label in ["THETA", "SHORT"] or "condor" in side_label.lower())
                    
                    if is_entry:
                        order_side = OrderSide.SELL if is_short_strategy else OrderSide.BUY
                    else:  # Exit
                        order_side = OrderSide.BUY if is_short_strategy else OrderSide.SELL
                    
                    # Determine symbol
                    symbol = "TXO"
                    if side_label in ["C", "P"]:
                        contract = self.active_contracts.get(side_label)
                        if contract: 
                            symbol = contract.code
                    
                    # Create order
                    order = Order(
                        symbol=symbol,
                        side=order_side,
                        order_type=OrderType.MARKET,
                        quantity=quantity,
                        price=price,
                        order_id=f"LEDGER-{ts.strftime('%Y%m%d-%H%M%S')}",
                        strategy=side_label if is_short_strategy else "directional",
                        comment=f"{action} {note}",
                    )
                    order.status = OrderStatus.FILLED
                    order.filled_quantity = quantity
                    order.avg_fill_price = price
                    order.filled_at = ts
                    order.created_at = ts
                    
                    # Add to completed orders
                    self.order_mgr.completed.append(order)
                    recovered_count += 1
                    
                except Exception as e:
                    console.print(f"[yellow]⚠️ Failed to recover order from row: {e}[/yellow]")
                    continue
            
            if recovered_count > 0:
                console.print(f"[bold cyan]♻️ Recovered {recovered_count} orders from ledger[/bold cyan]")
                # Save immediately to orders JSON
                self._save_orders_file_wrapper()
            
        except Exception as e:
            console.print(f"[yellow]Order recovery from ledger failed: {e}[/yellow]")

    # ── [Vertical Spread v1] Paper spread position management ──
    def manage_spread_paper_position(self, signal, option_chain=None):
        """
        Manage an open vertical spread position in paper mode.
        
        Calculates current spread value from both legs, then checks:
        - TAKE_PROFIT: spread value >= entry_debit + max_profit * 0.5
        - STOP_LOSS: spread value <= entry_debit * 0.55
        - TIME_STOP: held N bars with no progress
        - EOD_EXIT: market close
        
        Returns True if position was closed.
        """
        if self.position <= 0 or self._current_spread is None:
            return False

        now = signal.get("timestamp") if signal else self._current_strategy_time()
        sd = self._current_spread  # spread data
        opt_type = sd["option_type"]  # "Call" or "Put"
        
        # ── 1. Get current quotes for both legs ──
        option_chain = option_chain or getattr(self, '_all_month_contracts', [])
        
        from strategies.options.spread_selector import _contract_for_strike, _current_quote
        
        long_contract = _contract_for_strike(option_chain, sd["long_strike"], opt_type)
        short_contract = _contract_for_strike(option_chain, sd["short_strike"], opt_type)
        
        if not long_contract or not short_contract:
            return False
        
        # Build temporary market_data from current snapshots
        md = {}
        lc = getattr(long_contract, 'code', '')
        sc = getattr(short_contract, 'code', '')
        
        # Check if we have real-time market_data for these contracts
        if lc in self.market_data:
            md[lc] = self.market_data[lc]
        if sc in self.market_data:
            md[sc] = self.market_data[sc]
        
        long_bid, long_ask, long_mid = _current_quote(long_contract, md)
        short_bid, short_ask, short_mid = _current_quote(short_contract, md)
        
        if long_mid <= 0 or short_mid <= 0:
            return False  # Wait for valid quotes
        
        # ── 2. Calculate spread value and PnL ──
        # Spread value = what we'd get if we closed now
        # Bull Call Spread: sell long leg (at bid), buy back short leg (at ask)
        spread_value = long_bid - short_ask  # net credit to close
        entry_debit = sd["net_debit"]
        
        pnl_pts = spread_value - entry_debit
        pnl_pct = pnl_pts / entry_debit if entry_debit > 0 else 0
        
        # Holding duration (in 5-min bars)
        holding_bars = getattr(self, '_spread_holding_bars', 0)
        
        # ── 3. Exit rule checks ──
        
        # 3a. TAKE_PROFIT: spread value reached 50% of max_profit
        take_profit_target = entry_debit + sd["max_profit"] * 0.5
        if spread_value >= take_profit_target:
            console.print(f"[bold green][VerticalSpread] TAKE_PROFIT spread_value={spread_value:.1f} >= target={take_profit_target:.1f} "
                          f"pnl={pnl_pts:.1f}pts ({pnl_pct*100:.1f}%)[/bold green]")
            self._exit_spread_paper_position("PAPER_SPREAD_TP", spread_value, note=f"take_profit pnl={pnl_pts:.1f}")
            return True
        
        # 3b. STOP_LOSS: loss exceeds 45% of entry_debit
        stop_loss_target = entry_debit * 0.55  # lose 45%
        if spread_value <= stop_loss_target:
            console.print(f"[bold red][VerticalSpread] STOP_LOSS spread_value={spread_value:.1f} <= target={stop_loss_target:.1f} "
                          f"pnl={pnl_pts:.1f}pts ({pnl_pct*100:.1f}%)[/bold red]")
            self._exit_spread_paper_position("PAPER_SPREAD_SL", spread_value, note=f"stop_loss pnl={pnl_pts:.1f}")
            return True
        
        # 3c. TIME_STOP: held 6+ bars with no progress
        if holding_bars >= 6 and pnl_pts <= 0:
            console.print(f"[bold yellow][VerticalSpread] TIME_STOP held {holding_bars} bars, pnl={pnl_pts:.1f}pts[/bold yellow]")
            self._exit_spread_paper_position("PAPER_SPREAD_TIME", spread_value, note=f"time_stop bars={holding_bars}")
            return True
        
        # 3d. EOD_EXIT: market closing panic
        if hasattr(self, 'm_cfg') and self.m_cfg.get('force_close_at_end', False):
            eod_state = self._get_eod_state(now)
            if eod_state["is_panic"]:
                console.print(f"[bold yellow][VerticalSpread] EOD_PANIC exit spread_value={spread_value:.1f}[/bold yellow]")
                self._exit_spread_paper_position("PAPER_SPREAD_EOD", spread_value, note=f"eod_panic pnl={pnl_pts:.1f}")
                return True
        
        # ── 4. Update holding bars ──
        self._spread_holding_bars = holding_bars + 1
        
        return False

    def _exit_spread_paper_position(self, action, exit_value, note=""):
        """
        Close a spread position in paper mode.
        Records exit for both legs and clears spread state.
        """
        if self.position <= 0:
            return

        # ── [Rule 3] Reentrancy Guard ──
        if getattr(self, "_exit_in_progress", False):
            return
        self._exit_in_progress = True

        try:
            # ── [Rule 3] 1. Freeze Snapshot ──
            qty = self.position
            sd = self._current_spread
            side = self.active_side or "CALL"
            entry_p = sd["net_debit"]
            
            # ── [Rule 3] 2. Establish order intent ──
            # Record single paper order (net exit value) with spread context
            paper_order = self._record_paper_order(
                sd["option_type"], "SELL", qty, exit_value,
                f"{action} {note}",
                strategy_override="VERTICAL_SPREAD",
            )
            if paper_order is None:
                return  # Order failed, keep position for next tick retry

            # ── [Rule 3] 3. Update SSOT immediately AFTER order accepted ──
            self.position = 0
            self.active_side = None
            self._current_spread = None
            self._spread_holding_bars = 0
            self.entry_price = 0.0
            self.entry_mtx_price = 0.0
            self.entry_time = None
            self.has_tp1_hit = False
            self.stop_loss_price = 0.0
            self.peak_premium = 0.0
            
            # ── [Rule 3] 4. Side Effects (IO/Logging) ──
            # Compute PnL
            pnl_cash = (exit_value - entry_p) * self.pricing_cfg.get("point_value", 50)
            logger.info("[VerticalSpread] EXIT %s qty=%d entry=%.1f exit=%.1f pnl=%.1f",
                         action, qty, entry_p, exit_value, pnl_cash)
            
            # Log trade with corrected parameter mapping
            if hasattr(self, 'log_trade'):
                self.log_trade(
                    action="VERTICAL_SPREAD", 
                    side=side,
                    price=exit_value, 
                    note=f"{action} {note}".strip(), 
                    quantity=qty, 
                    entry_price_override=entry_p
                )
                
        finally:
            self._exit_in_progress = False


    # ═══════════════════════════════════════════════════════════════
    # 2026-05-26 Hermes Agent: tick-level exit evaluator (60s gap fix)
    # ═══════════════════════════════════════════════════════════════

    def _resolve_tick_to_side(self, code: str) -> str | None:
        """Map a tick's contract code to the position side (C/P/MTX)."""
        for key in ("C", "P", "MTX"):
            contract = self.active_contracts.get(key)
            if contract and code == getattr(contract, "code", None):
                return key
        return None

    def _extract_tick_premium(self, tick) -> float:
        """Get the exit-relevant premium from a tick: bid > close > 0."""
        bid = float(getattr(tick, "bid_price", 0) or 0)
        if bid > 0:
            return bid
        return float(getattr(tick, "close", 0) or 0)

    def _option_exit_on_tick(self, tick) -> None:
        """
        Evaluate exit conditions from a live option tick.
        No IO, no blocking — only sets pending intent.
        Must be called inside self.lock.
        """
        if self._exit_in_progress:
            return
        if self._pending_exit_request is not None:
            return
        if self.position <= 0 or not self.active_side:
            return

        premium = self._extract_tick_premium(tick)
        if premium <= 0:
            return

        # Update peak premium for trailing stop
        if premium > self.peak_premium:
            self.peak_premium = premium

        # Check 1: Stop loss
        if self.stop_loss_pct > 0:
            sl_threshold = self.entry_price * (1 - self.stop_loss_pct)
            if premium <= sl_threshold:
                self._exit_in_progress = True
                self._exit_start_time = time.monotonic()
                self._pending_exit_request = {
                    "reason": "PAPER_STOP_LOSS",
                    "premium": premium,
                    "source": "OPTION_TICK_EXIT",
                }
                return

        # Check 2: Hard stop
        if self.hard_stop_pct > 0:
            hs_threshold = self.entry_price * (1 - self.hard_stop_pct)
            if premium <= hs_threshold:
                self._exit_in_progress = True
                self._exit_start_time = time.monotonic()
                self._pending_exit_request = {
                    "reason": "PAPER_HARD_STOP",
                    "premium": premium,
                    "source": "OPTION_TICK_EXIT",
                }
                return

        # Check 3: Trailing stop
        if self.trailing_stop_pct > 0 and self.peak_premium > 0:
            unrealized_pct = (self.peak_premium - self.entry_price) / self.entry_price if self.entry_price > 0 else 0
            if self.has_tp1_hit or unrealized_pct >= 0.08:
                trail_floor = self.peak_premium * (1 - self.trailing_stop_pct)
                if premium <= trail_floor:
                    self._exit_in_progress = True
                    self._exit_start_time = time.monotonic()
                    self._pending_exit_request = {
                        "reason": "PAPER_TRAIL_EXIT",
                        "premium": premium,
                        "source": "OPTION_TICK_EXIT",
                    }
                    return

    def _drain_pending_option_exit_request(self) -> None:
        """Consume pending exit intent in main thread (safe for IO)."""
        req = self._pending_exit_request
        if not req:
            return
        self._pending_exit_request = None
        reason = req["reason"]
        premium = req["premium"]
        note = f"{reason} premium={premium:.1f} source={req.get('source', '?')}"
        if self.live_trading:
            self.exit_live_position(reason, note)
        else:
            self.exit_paper_position(reason, premium, note)


    # ═══════════════════════════════════════════════════════════════
    # 2026-05-26 Hermes Agent: Options Watchdog (三層防禦)
    # ═══════════════════════════════════════════════════════════════

    def _log_watchdog_alert(self, reason="", **kwargs):
        """FORENSIC metadata for watchdog events. Append-only JSONL."""
        import json
        from pathlib import Path
        import datetime as _dt
        event = {
            "type": "WATCHDOG_ALERT",
            "timestamp": _dt.datetime.now().isoformat(),
            "reason": reason,
            "position": self.position,
            "active_side": self.active_side,
            "exit_in_progress": self._exit_in_progress,
            "pending_exit": self._pending_exit_request is not None,
            "watchdog_state": self._watchdog_state,
        }
        event.update(kwargs)
        log_dir = Path("logs/options_watchdog")
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "watchdog_events.jsonl", "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _run_options_watchdog(self):
        """
        Tiered Options Safety Watchdog.

        Tier 1 (10s): _exit_in_progress stuck detection & self-heal.
        Tier 2 (30s): memory vs ledger reconciliation (告警 only, 不清倉).
        """
        now_mono = time.monotonic()

        # ── Tier 1: High-Frequency (10s) ──
        if (now_mono - self._watchdog_last_hi) < self._watchdog_hi_period:
            return
        self._watchdog_last_hi = now_mono

        if self._exit_in_progress:
            elapsed = now_mono - self._exit_start_time if self._exit_start_time > 0 else 0.0
            if elapsed > 15.0:
                # Case A: pending exit not consumed — retry drain
                if self._pending_exit_request is not None:
                    console.print(f"[bold yellow]♻️ [WATCHDOG] Pending exit not consumed after {elapsed:.0f}s. Re-draining.[/bold yellow]")
                    self._log_watchdog_alert(reason="PENDING_EXIT_RETRY", elapsed_secs=round(elapsed, 1))
                    self._drain_pending_option_exit_request()

                # Case B: stuck with position — retry by setting a fresh exit request
                elif self.position > 0:
                    console.print(f"[bold yellow]♻️ [WATCHDOG] Exit stuck {elapsed:.0f}s. Enqueueing RETRY_EXIT.[/bold yellow]")
                    self._log_watchdog_alert(
                        reason="EXIT_STUCK_RETRY",
                        elapsed_secs=round(elapsed, 1),
                        position=self.position,
                    )
                    self._exit_in_progress = False
                    self._exit_start_time = 0.0
                    quote = self.current_option_quote(self.active_side)
                    retry_price = quote.get("bid", quote.get("close", 0))
                    if retry_price > 0:
                        self._pending_exit_request = {
                            "reason": "PAPER_RETRY_EXIT",
                            "premium": retry_price,
                            "source": "WATCHDOG_RETRY",
                        }
                        self._exit_in_progress = True
                        self._exit_start_time = time.monotonic()

                # Case C: no position but stuck flag — clean up
                else:
                    self._exit_in_progress = False
                    self._exit_start_time = 0.0
                    console.print(f"[dim]🗑️ [WATCHDOG] Cleared stale _exit_in_progress (position=0)[/dim]")

        # ── Tier 2: Low-Frequency (30s) ──
        if (now_mono - self._watchdog_last_lo) < self._watchdog_lo_period:
            return
        self._watchdog_last_lo = now_mono

        # Reconciliation: compare memory position vs ledger (告警 only)
        if self.position > 0 and hasattr(self, "ledger_path") and self.ledger_path.exists():
            try:
                import pandas as _pd
                ledger_df = _pd.read_csv(self.ledger_path)
                has_open = False
                for _, row in ledger_df.iterrows():
                    action = str(row.get("Action", "")).upper()
                    if "ENTRY" in action and "EXIT" not in action and "RETRY" not in action:
                        has_open = True
                    elif has_open and any(kw in action for kw in ["EXIT", "TP1"]):
                        has_open = False
                if not has_open:
                    self._watchdog_state = "RECONCILIATION_MISMATCH"
                    self._log_watchdog_alert(
                        reason="LEDGER_MEMORY_MISMATCH",
                        action="MARK_REVIEW_ONLY",
                        local_position=self.position,
                    )
                    console.print(f"[bold yellow]📋 [WATCHDOG] RECONCILIATION_MISMATCH: memory has position ({self.position}) but ledger shows none. Review recommended.[/bold yellow]")
            except Exception:
                pass


    def manage_open_position(self, signal):
        if self.position <= 0 or not self.active_side:
            return False

        # 2026-05-27 Hermes Agent: [EXIT_AUDIT] trace entry data
        import datetime as _dt
        _now_s = _dt.datetime.now().strftime("%H:%M:%S.%f")[:12]
        # 2026-05-29 Hermes Agent: guard for test mock context
        _mc = getattr(self, 'market_data', {}).get(self.active_side, {})
        _q = self.current_option_quote(self.active_side)
        _vq = self.validate_quote(self.active_side, context="EXIT")
        _o = self._is_market_open(_dt.datetime.now())
        _sig_side = signal.get("side") if signal else "no_signal"
        _sig_score = signal.get("score", 0.0) if signal else 0.0
        print(f"[EXIT_AUDIT] {_now_s} position={self.position} side={self.active_side} entry={self.entry_price} "
              f"bid={_q['bid']} ask={_q['ask']} mid={_q['mid']} close={_q['close']} "
              f"mc_close={_mc.get('close','?')} mc_bid={_mc.get('bid','?')} mc_ask={_mc.get('ask','?')} "
              f"peak={self.peak_premium} sl_pct={self.stop_loss_pct} hard_sl={self.hard_stop_pct} "
              f"trail_pct={self.trailing_stop_pct} has_tp1={self.has_tp1_hit} "
              f"valid_quote={_vq['valid']} quote_reason={_vq['reason']} "
              f"market_open={_o[0]} session={_o[1]} "
              f"_exit_in_progress={self._exit_in_progress} "
              f"signal_score={_sig_score:.1f} signal_side={_sig_side} "
              f"contract={self.active_contracts.get(self.active_side)}")
        
        now = signal.get("timestamp") if signal else self._current_strategy_time()
        quote = self.current_option_quote(self.active_side)
        exit_price = quote["bid"]
        contract = self.active_contracts.get(self.active_side)

        # ── [QuoteGuard] Validate quote quality before any exit decision ──
        vq = self.validate_quote(self.active_side, context="EXIT")
        if not vq["valid"]:
            return False

        # ── [SessionGuard] Skip exit during invalid market state ──
        _market_open, _session = self._is_market_open(now)
        if not _market_open:
            console.print(f"[bold yellow]⏰ [SessionGuard] Market closed ({_session}) — skip exit, mark pending[/bold yellow]")
            return False
        
        # 1. 檢查時間約束 (持有天數與 DTE)
        if contract and self.entry_time:
            dte_days = calculate_dte(contract.delivery_date, now=now) * 365
            should_time_exit, time_reason = should_exit_by_time_constraints(
                self.entry_time, now, dte_days, 
                max_days=self.max_holding_days, 
                min_dte=self.min_dte_to_exit
            )
            if should_time_exit:
                console.print(f"[bold yellow]Time Constraint Exit:[/bold yellow] {time_reason}")
                if self.live_trading:
                    self.exit_live_position("LIVE_TIME_EXIT_SUBMITTED", f"reason={time_reason}")
                else:
                    self.exit_paper_position("PAPER_TIME_EXIT", exit_price, f"reason={time_reason}")
                return True

        if exit_price <= 0:
            console.print(f"[yellow]⚠️ manage_open_position: invalid exit price ({exit_price}) for {self.active_side}, skipping this tick[/yellow]")
            return False

        # 2. Trailing stop: 追蹤最高權利金，回落超過 trailing_stop_pct 就出場
        #    修正: 浮盈 > 8% 即啟動 trailing (原 15% 太高，多數情況沒保護)
        if exit_price > self.peak_premium:
            self.peak_premium = exit_price
        unrealized_pct = (self.peak_premium - self.entry_price) / self.entry_price if self.entry_price > 0 else 0
        if self.trailing_stop_pct > 0 and (self.has_tp1_hit or unrealized_pct >= 0.08) and self.peak_premium > 0:
            trail_floor = self.peak_premium * (1 - self.trailing_stop_pct)
            if exit_price <= trail_floor:
                reason = f"TRAILING_STOP peak={self.peak_premium:.1f} floor={trail_floor:.1f}"
                if self.live_trading:
                    self.exit_live_position("LIVE_TRAIL_EXIT_SUBMITTED", reason)
                else:
                    self.exit_paper_position("PAPER_TRAIL_EXIT", exit_price, reason)
                return True
        
        signal_score = signal.get("score", 0.0) if signal else (self.last_signal.get("score", 0.0) if self.last_signal else 999.0)
        directional_release_state = {"confirmed": False, "reason": "missing_signal"}
        if signal:
            spot = float(self.market_data["MTX"]["close"] or 0.0)
            directional_release_state = self._update_theta_release_confirmation(signal, spot)

        # 3. Score reversal exit: 趨勢翻轉才出場
        #    修正: 出場門檻比進場寬 (1.5x entry_score)，防止 whipsaw
        #    買 P 時 score 很負，翻正超過 reversal_threshold → 趨勢反轉
        #    買 C 時 score 很正，翻負超過 reversal_threshold → 趨勢反轉
        reversal_threshold = self.entry_score * 1.5  # 15 → 22.5
        
        # 💡 GSD: Opening Grace Period Protection
        # Avoid reacting to data spikes or gaps in the first N mins of ANY session
        session_mins = 0
        h, m = now.hour, now.minute
        if 8 <= h < 14: # Day
            session_mins = (h - 8) * 60 + (m - 45)
        elif h >= 15: # Night start
            session_mins = (h - 15) * 60 + m
        elif h < 5: # Night late
            session_mins = (h + 9) * 60 + m
            
        is_in_grace = 0 <= session_mins < self.opening_grace_mins
        
        if self.active_side == "P" and signal_score >= reversal_threshold:
            if is_in_grace:
                console.print(f"[yellow]🛡️ REVERSAL blocked by opening grace ({session_mins}m < {self.opening_grace_mins}m)[/yellow]")
            elif not directional_release_state["confirmed"]:
                console.print(f"[dim]🛡️ REVERSAL gated: {directional_release_state['reason']}[/dim]")
            else:
                reason = f"SCORE_REVERSAL score={signal_score:.1f} >= {reversal_threshold:.0f} (was bearish, now bullish)"
                if self.live_trading:
                    self.exit_live_position("LIVE_REVERSAL_SUBMITTED", reason)
                else:
                    self.exit_paper_position("PAPER_REVERSAL_EXIT", exit_price, reason)
                return True
        if self.active_side == "C" and signal_score <= -reversal_threshold:
            if is_in_grace:
                console.print(f"[yellow]🛡️ REVERSAL blocked by opening grace ({session_mins}m < {self.opening_grace_mins}m)[/yellow]")
            elif not directional_release_state["confirmed"]:
                console.print(f"[dim]🛡️ REVERSAL gated: {directional_release_state['reason']}[/dim]")
            else:
                reason = f"SCORE_REVERSAL score={signal_score:.1f} <= -{reversal_threshold:.0f} (was bullish, now bearish)"
                if self.live_trading:
                    self.exit_live_position("LIVE_REVERSAL_SUBMITTED", reason)
                else:
                    self.exit_paper_position("PAPER_REVERSAL_EXIT", exit_price, reason)
                return True

        # 4. TP1 partial profit
        if should_take_partial_profit(self.position, self.has_tp1_hit, self.entry_price, exit_price, self.m_cfg["tp1_pct"]):
            if self.live_trading:
                self.exit_live_position("LIVE_TP1_SUBMITTED", f"score={signal_score:.1f}", quantity=1)
            else:
                self.apply_paper_tp1(exit_price, f"score={signal_score:.1f}")
        if should_exit_position(
            exit_price,
            self.entry_price,
            self.stop_loss_pct,
            signal_score,
            self.has_tp1_hit,
            score_floor=self.score_floor,
            hard_stop_pct=self.hard_stop_pct,
        ):
            exit_reason = classify_exit_reason(
                exit_price,
                self.entry_price,
                self.stop_loss_pct,
                signal_score,
                self.has_tp1_hit,
                score_floor=self.score_floor,
                hard_stop_pct=self.hard_stop_pct,
            )
            if exit_reason == "score_decay" and not directional_release_state["confirmed"]:
                console.print(f"[dim]🛡️ SCORE_DECAY gated: {directional_release_state['reason']}[/dim]")
                return False
            if self.live_trading:
                self.exit_live_position("LIVE_EXIT_SUBMITTED", f"{exit_reason or 'exit'} score={signal_score:.1f}")
            else:
                self.exit_paper_position("PAPER_EXIT", exit_price, f"{exit_reason or 'exit'} score={signal_score:.1f}")
            return True
        return False

    def run_strategy_logic(self):
        # 2026-05-26 Hermes Agent: Options Watchdog (三層防禦)
        self._run_options_watchdog()

        # 2026-05-26 Hermes Agent: consume pending tick-level exit request
        self._drain_pending_option_exit_request()

        try:
            # Retry contract loading if not yet available
            if not self.active_contracts or not self._all_month_contracts:
                self.find_best_contracts()
                if not self.active_contracts:
                    # 2026-05-27 Hermes Agent: if holding position, allow exit management even without contracts
                    if self.position <= 0:
                        return  # No contracts yet — retry next cycle
            
            # GSD: Update log paths dynamically to handle session rollovers (e.g. 15:00)
            self._update_log_paths()
            
            now = self._current_strategy_time()
            eod_state = self._eod_state(now)
            self.refresh_live_orders()
            signal = self.fetch_live_signal()

            sig_side = signal.get("side") if signal else None
            sig_score = signal.get("score", 0.0) if signal else 0.0

            if signal and sig_side and self.position == 0:
                # [GSD 4.13] Trading Readiness Gate
                if not self.is_trading_ready:
                    if self._bar_counter % 5 == 0:
                        console.print(f"[bold yellow]🛡️ Trading Not Ready: Option {sig_side} Entry Blocked (Stabilizing indicators...)[/bold yellow]")
                    signal = {"score": 0.0, "side": None, "mid_trend": "", "price_mtx": 0.0}
                    return

                # [L4] Decision Intelligence: Edge Evaluation
                from core.edge_model import edge_model

                # Context for EdgeModel
                spot = float(self.market_data["MTX"]["close"])
                edge_context = {
                    "momentum": float(self.latest_score),
                    "regime": str(self.latest_mid_trend),
                    "vwap_dist": 0, # Not readily available
                    "volatility": float(self.latest_iv or 0.25),
                    "price": spot,
                    "side": "LONG" if sig_side == "C" else "SHORT",
                    # Pass proxies for Alpha features if not directly available
                    "breakout_strength": 0.0, 
                    "volume_spike": 1.0,
                    "trend_strength_raw": 0.005 if self.latest_mid_trend == "BULL" else (-0.005 if self.latest_mid_trend == "BEAR" else 0)
                }

                edge_res = edge_model.evaluate(abs(sig_score), edge_context, f"{self.mode}_squeeze")

                if not edge_res["has_edge"]:
                    if self._bar_counter % 5 == 0:
                        console.print(f"[bold yellow]🛡️ Decision Intelligence: Option {sig_side} Blocked - {edge_res['reason']}[/bold yellow]")
                    signal = {"score": 0.0, "side": None, "mid_trend": "", "price_mtx": 0.0}
                else:
                    entry_lots = self._resolve_entry_lots(edge_res["pos_scale"])
                    if entry_lots <= 0:
                        signal = {"score": 0.0, "side": None, "mid_trend": "", "price_mtx": 0.0}
                    elif isinstance(signal, dict):
                        signal["entry_lots"] = entry_lots
                    if edge_res["pos_scale"] != 1.0:
                        console.print(
                            f"[bold cyan]⚖️ Option Position Scaled: {edge_res['rank']} "
                            f"(x{edge_res['pos_scale']}) -> {entry_lots} lots "
                            f"(base {self.base_lots})[/bold cyan]"
                        )

                if signal and signal.get("side"):
                    directional_release_state = self._update_theta_release_confirmation(signal, spot)
                    if not directional_release_state["confirmed"]:
                        if self._bar_counter % 5 == 0:
                            console.print(f"[bold yellow]🛡️ Directional entry gated: {directional_release_state['reason']}[/bold yellow]")
                        signal = {"score": 0.0, "side": None, "mid_trend": "", "price_mtx": 0.0}

            # Re-read the gated signal so later entry logic cannot use stale side/score values.
            sig_side = signal.get("side") if signal else None
            sig_score = signal.get("score", 0.0) if signal else 0.0

            if self.position > 0:
                # [GSD Fix] Handle non-standard active_side (THETA/SHORT)
                if self.active_side in self.market_data:
                    cur_p = self.market_data[self.active_side]["close"]
                    cur_bid = self.market_data[self.active_side]["bid"]
                    cur_ask = self.market_data[self.active_side]["ask"]
                else:
                    # For ThetaGang/Multi-leg, the manager calculates its own 'value'
                    cur_p = float(self.market_data["MTX"]["close"]) # Use underlying as proxy if needed
                    cur_bid = cur_p - 1
                    cur_ask = cur_p + 1

                # ── [Vertical Spread v1] Spread position management ──
                if self._enable_vertical_spread and self._current_spread is not None:
                    if self.live_trading:
                        exited = self.manage_spread_live_position(signal, option_chain=option_chain)
                    else:
                        exited = self.manage_spread_paper_position(signal, option_chain=option_chain)
                    if exited:
                        self.print_status_summary(signal, force=True)
                        return
                elif self.manage_open_position(signal):
                    self.print_status_summary(signal, force=True)
                    return
                
                # 2. EOD 優化出場
                if self.m_cfg['force_close_at_end']:
                    if eod_state["is_panic"]:
                        exit_p = cur_bid # 趕快走
                        if self.live_trading:
                            self.exit_live_position(
                                "LIVE_EOD_PANIC_SUBMITTED",
                                f"Time reached {self.exit_opt['eod_panic_time']}",
                            )
                        else:
                            self.exit_paper_position(
                                "EOD_PANIC_EXIT",
                                exit_p,
                                f"Time reached {self.exit_opt['eod_panic_time']}",
                            )
                        self.print_status_summary(signal, force=True)
                        return
                    elif eod_state["is_passive"]:
                        # 掛高 3 Tick 等成交
                        trap_price = cur_ask + (self.exit_opt['eod_passive_ticks'] * 1.0)
                        console.print(f"[yellow]EOD Trap Active: Hanging sell at {trap_price}...[/yellow]")
                        # 此處模擬檢查：如果市場價撞到 trap_price 就成交
                        if cur_p >= trap_price:
                            if self.live_trading:
                                self.exit_live_position("LIVE_EOD_TRAP_SUBMITTED", "Passive fill achieved!")
                            else:
                                self.exit_paper_position("EOD_TRAP_FILL", trap_price, "Passive fill achieved!")
                            self.print_status_summary(signal, force=True)
                            return

            if self.position < self.max_positions:
                if self.cooldown_until > 0:
                    self.cooldown_until -= 1
                    console.print(f"[dim]Cooldown: {self.cooldown_until} bars remaining[/dim]")
                
                # [GSD 2026-04-24] Directional signal優先於ThetaGang
                # 當 sig_side 有值（方向性訊號+release確認），直接跳過ThetaGang去做方向性交易
                skip_theta_for_directional = bool(signal and sig_side)
                if skip_theta_for_directional:
                    console.print(f"[dim]方向性訊號優先: {sig_side} score={sig_score:.1f} — 跳過 ThetaGang[/dim]")
                
                # ThetaGang: sell premium when squeeze is on (ranging market)
                if not skip_theta_for_directional and self._theta_gang and signal and self.cooldown_until <= 0:
                    squeeze_on = signal.get("squeeze_on", False) if isinstance(signal, dict) else False
                    auto_regime = self._theta_cfg.get("auto_regime", True)
                    bar_quality_pass = signal.get("bar_quality") == "PASS"
                    use_theta = False  # DISABLED: theta gang has no edge vs friction (68pts)

                    if self.pending_theta_combo is not None:
                        console.print(f"[dim][ThetaGang] Pending combo {self.pending_theta_combo.get('phase')} awaiting broker reconciliation[/dim]")
                        return

                    # Manage existing ThetaGang position
                    if self._theta_gang.position and self._theta_gang.position.is_open:
                        self._theta_bars_held += 1
                        spot = float(self.market_data["MTX"]["close"])
                        contract = self.active_contracts.get("C") or self.active_contracts.get("P")
                        dte_years = float(self._dte(getattr(contract, "delivery_date", None))) if contract else 0.03
                        iv = self.latest_iv or 0.25
                        release_state = self._update_theta_release_confirmation(signal, spot)
                        exit_info = self._theta_gang.evaluate_exit(
                            spot,
                            iv,
                            dte_years,
                            squeeze_on,
                            allow_squeeze_release=release_state["confirmed"],
                        )
                        if release_state["raw_release_candidate"] and not release_state["confirmed"]:
                            console.print(f"[dim][ThetaGang] Release gated: {release_state['reason']}[/dim]")
                        
                        # 💡 GSD: 最小持倉時間檢查 (停損 SL 必須優先於持倉時間，強制出場)
                        min_hold = int(self._theta_cfg.get("min_holding_bars", 0))
                        is_sl = exit_info and "SL" in exit_info.get("reason", "")
                        
                        if exit_info and not is_sl and self._theta_bars_held < min_hold:
                            console.print(f"[dim][ThetaGang] Exit signal ({exit_info['reason']}) deferred: held {self._theta_bars_held}/{min_hold} bars[/dim]")
                            exit_info = None

                        if exit_info:
                            if self.live_trading:
                                self._submit_live_theta_combo_exit(exit_info)
                                return
                            pos = self._theta_gang.close_position()
                            self._theta_bars_held = 0
                            self._theta_release_confirm_count = 0
                            self._theta_release_last_bar_ts = None
                            self.position = 0
                            self.active_side = None
                            self.entry_price = 0.0
                            self.entry_time = None
                            self.stop_loss_price = 0.0
                            self.peak_premium = 0.0
                            self.has_tp1_hit = False
                            # GSD fix: ThetaGang has pre-computed PnL, don't recalculate through log_trade
                            theta_pnl = round(exit_info["pnl"], 0)
                            # 計算累計Balance（從現有ledger）
                            current_balance = 0
                            if self.ledger_path.exists():
                                try:
                                    import pandas as pd
                                    prev = pd.read_csv(self.ledger_path)
                                    current_balance = pd.to_numeric(prev["PnL"], errors="coerce").fillna(0).sum()
                                except Exception as e:
                                    console.print(f"[yellow]⚠️ Ledger read error: {e} — balance reset to 0[/yellow]")
                            
                            new_balance = current_balance + theta_pnl
                            
                            # GSD fix: 確保exit_price正確記錄
                            exit_price = float(exit_info.get("current_value", 0))
                            
                            theta_row = {
                                "Timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "Mode": self.mode, "Action": "THETA_EXIT", "Side": pos.strategy,
                                "Price": exit_price,  # 記錄平倉價值
                                "Quantity": pos.quantity,
                                "PnL": theta_pnl, "Balance": new_balance,
                                "Note": f"{exit_info['reason']} credit={pos.net_credit:.0f} pnl={exit_info['pnl']:.0f}",
                            }
                            self._append_csv_row_durable(self.ledger_path, theta_row)
                            self._record_paper_order(
                                "THETA",
                                "EXIT",
                                pos.quantity,
                                exit_price,
                                f"{exit_info['reason']} credit={pos.net_credit:.0f} pnl={exit_info['pnl']:.0f}",
                                strategy_override=pos.strategy,
                            )
                            console.print(f"[bold yellow]🔻 [ThetaGang] EXIT {pos.strategy}: {exit_info['reason']} PnL={exit_info['pnl']:.0f}[/bold yellow]")
                            self.cooldown_until = self.cooldown_bars
                            self._last_theta_exit_at = datetime.datetime.now()
                        return

                    # ── [ThetaGate] Router must allow theta before entry ──
                    _router_allows_theta = True  # default: allow if no router data (bootstrap)
                    if hasattr(self, 'fm') and self.fm is not None and hasattr(self.fm, 'latest_router_decision'):
                        _rd = self.fm.latest_router_decision
                        if _rd is not None:
                            _router_allows_theta = _rd.theta_allowed
                            if not _router_allows_theta:
                                console.print(f"[yellow]⚠️ [ThetaGate] Router blocks theta: {_rd.theta_block_reason} — skip entry[/yellow]")
                        else:
                            console.print(f"[dim][ThetaGate] No router decision yet — allowing theta bootstrap[/dim]")
                    else:
                        console.print(f"[dim][ThetaGate] No futures monitor reference — allowing theta bootstrap[/dim]")
                    if not _router_allows_theta:
                        if use_theta and self.position == 0:
                            signal = None  # Force no-op for this tick

                    # Try ThetaGang entry
                    if use_theta and self.position == 0 and not (self._theta_gang.position and self._theta_gang.position.is_open):
                        spot = float(self.market_data["MTX"]["close"])
                        contract = self.active_contracts.get("C") or self.active_contracts.get("P")
                        dte_years = float(self._dte(getattr(contract, "delivery_date", None))) if contract else 0.03
                        iv = self.latest_iv or 0.25
                        entry_info = self._theta_gang.evaluate_entry(
                            spot,
                            iv,
                            dte_years,
                            squeeze_on,
                            score=signal.get("score") if isinstance(signal, dict) else None,
                        )
                        if entry_info:
                            # ── [ThetaGate] Edge gate: expected edge must cover friction × 2 ──
                            net_credit = entry_info.get("net_credit", 0)
                            friction = getattr(self, 'theta_friction', 68)  # default ~68 pts round-trip
                            min_edge = friction * getattr(self, 'theta_min_edge_multiple', 2.0)
                            if net_credit < min_edge:
                                console.print(f"[yellow]⚠️ [ThetaGate] Edge too small: credit={net_credit:.0f} < min_edge={min_edge:.0f} (friction={friction}) — skip entry[/yellow]")
                                if self.live_trading:
                                    self.exit_live_position("LIVE_EDGE_TOO_SMALL", f"credit={net_credit:.0f}<min={min_edge:.0f}")
                                return

                            # ── [ThetaGate] Min hold gate — skip if just exited (cooldown) ──
                            _now = datetime.datetime.now()
                            if hasattr(self, '_last_theta_exit_at') and self._last_theta_exit_at is not None:
                                _seconds_since_exit = (_now - self._last_theta_exit_at).total_seconds()
                                _min_hold = getattr(self, 'theta_min_hold_seconds', 1800)
                                if _seconds_since_exit < _min_hold:
                                    console.print(f"[yellow]⚠️ [ThetaGate] Cooldown: {_seconds_since_exit:.0f}s < {_min_hold}s since last exit — skip entry[/yellow]")
                                    return

                            if self.live_trading:
                                self._submit_live_theta_combo_entry(entry_info)
                                return
                            pos = self._theta_gang.open_position(entry_info)
                            self._theta_bars_held = 0
                            self._theta_release_confirm_count = 0
                            self._theta_release_last_bar_ts = None
                            # Create a readable string of position legs
                            legs_str = " | ".join(f"{leg.action} {leg.side}{leg.strike}" for leg in pos.legs)
                            # 記錄實際收取的權利金作為進場價
                            # GSD fix: 確保price參數正確傳遞
                            if pos and hasattr(pos, 'net_credit') and pos.net_credit is not None and pos.net_credit > 0:
                                entry_price = float(pos.net_credit)
                            else:
                                entry_price = 0.0
                                console.print(f"[yellow]⚠️ THETA_ENTRY: net_credit is None or 0, using price=0[/yellow]")
                            self.position = pos.quantity
                            self.active_side = "THETA"
                            self.entry_price = entry_price
                            self.entry_time = datetime.datetime.now()
                            self.stop_loss_price = entry_price * (1 + self.stop_loss_pct) if entry_price > 0 else 0.0
                            self.peak_premium = entry_price
                            self.has_tp1_hit = False
                            self.log_trade("THETA_ENTRY", "THETA", entry_price,
                                           f"credit={pos.net_credit:.0f} max_loss={pos.max_loss:.0f} strategy={pos.strategy} [{legs_str}]")
                            self._record_paper_order(
                                "THETA",
                                "BUY",
                                pos.quantity,
                                entry_price,
                                f"credit={pos.net_credit:.0f} max_loss={pos.max_loss:.0f} strategy={pos.strategy} [{legs_str}]",
                                strategy_override=pos.strategy,
                            )
                            console.print(f"[bold cyan]🔺 [ThetaGang] ENTRY {pos.strategy}: credit={pos.net_credit:.0f} [{legs_str}][/bold cyan]")
                            return

                if signal and sig_side:
                    if self.cooldown_until > 0:
                        # [GSD 2026-04-24] Dynamic cooldown: strong directional signal can exit cooldown early
                        if abs(sig_score) >= self.entry_score * 1.2:
                            console.print(f"[yellow][Cooldown] Strong signal score={sig_score:.1f} >= {self.entry_score*1.2:.1f} — breaking cooldown early[/yellow]")
                            self.cooldown_until = 0
                        else:
                            console.print(f"[dim]Signal {sig_side} ignored during cooldown ({self.cooldown_until} bars remaining)[/dim]")
                            self.replay_stats["blocked_entries"] += 1
                    elif self.live_trading:
                        console.print(f"[bold green]>>> ENTRY SIGNAL: {sig_side} score={sig_score:.1f}[/bold green]")
                        if self._enable_vertical_spread:
                            self.enter_spread_live_position(sig_side, signal)
                        else:
                            self.enter_live_position(sig_side, signal)
                    else:
                        # Track: did paper entry succeed or fail?
                        before_pos = self.position
                        if self._enable_vertical_spread:
                            self.enter_spread_paper_position(sig_side, signal)
                        else:
                            self.enter_paper_position(sig_side, signal)
                        if self.position == before_pos:
                            self.replay_stats["blocked_entries"] += 1

            # Periodic summary: every 60s print signal stats
            import time as _time
            now_ts = _time.time()
            if self.replay_stats["signals"] > 0 and (now_ts - self.replay_stats.get("last_summary_at", 0)) >= 60:
                self.replay_stats["last_summary_at"] = now_ts
                rs = self.replay_stats
                blocked = rs["blocked_entries"]
                total_dir = rs["directional_signals"]
            self.print_status_summary(signal)
        except Exception as exc:
            console.print(f"[red]Strategy loop error:[/red] {exc}")

    def run(self):
        self._running = True
        
        # [Phase A] Immediate Activation (Moved from __init__)
        # Extra guard: if api was already injected by OptionsMonitor wrapper, skip login.
        if not self.dry_run and self.api is None:
            from strategies.options.login import shioaji_login
            from options_engine.engine.broker_adapter import ShioajiBrokerAdapter, MockBrokerAdapter
            
            self.api = shioaji_login.login()
            if self.dry_run_live_orders:
                self.broker = MockBrokerAdapter(self.execution_cfg)
            else:
                self.broker = ShioajiBrokerAdapter(self.api, self.execution_cfg)
                
            if self.api is not None and hasattr(self.api, "set_order_callback"):
                self.api.set_order_callback(self.on_order_event)

            # Initialize OrderManager
            cfg = self.load_config()
            self._use_order_manager = cfg.get("monitoring", {}).get("use_order_manager", False)
            if self._use_order_manager:
                from core.order_management.order_manager import OrderManager
                _om_mode = "live" if self.live_trading else "paper"
                self.order_mgr = OrderManager(mode=_om_mode, broker_adapter=self.broker)
                
                # Prefer broker truth for live startup; fall back to ledger when broker recovery yields nothing.
                self._startup_recover_live_order_state()
                
                self._wire_order_callbacks()
                console.print(f"[green]📋 Options Order Lifecycle Manager enabled ({_om_mode} mode)[/green]")

        # Immediate Heartbeat & Status Summary
        if not self.active_contracts:
            found = self.find_best_contracts()
            if not found:
                console.print("[yellow]⏳ Options contracts not loaded yet — will retry in main loop[/yellow]")
                # Don't return here; let the main loop retry find_best_contracts
                # Contracts will be available once background fetch completes
            
        if not self.dry_run and self.api is not None:
            self._recover_position_from_api()

        # [Phase B] Async Backfill (4000+ Bars)
        import threading
        self._backfill_done = False
        def _bg_prefill():
            console.print(f"[cyan]⏳ [Phase B] Options background MTX backfill starting...[/cyan]")
            self.pre_fill_bars()
            self._backfill_done = True
            from core.shioaji_session import set_system_status, SystemReadiness
            set_system_status(SystemReadiness.TRADING)
            console.print(f"[bold green]✅ [Phase B] Options backfill complete. Indicators stabilizing...[/bold green]")
        
        threading.Thread(target=_bg_prefill, daemon=True).start()

        # Diagnostic Engine
        from core.diagnostic_engine import DiagnosticEngine
        self.diag_engine = DiagnosticEngine(str(self.ledger_path))
        self._diag_counter = 0

        console.print(f"[green]🚀 Options Monitor Running ({self.status_mode_label()}). Status: WARMING_UP[/green]")
        self.print_status_summary(force=True)

        try:
            while self._running:
                if os.path.exists(".restart"): break
                try:
                    self.run_strategy_logic()
                    self._diag_counter += 1
                    if self._diag_counter % 10 == 0:
                        results = self.diag_engine.check_health()
                        for r in results:
                            console.print(f"[bold red]🩺 DIAGNOSTIC ALERT (Options): {r.action}[/bold red]")
                except Exception as exc:
                    console.print(f"[red]Options strategy logic error: {exc}[/red]")
                    # [V-Model] Failsafe fallback entry
                    self._failsafe_fallback_entry(exc)
                time.sleep(self.loop_sleep_secs)
        except KeyboardInterrupt:
            pass
        finally:
            if self.api:
                self.api.logout()

def parse_args():
    parser = argparse.ArgumentParser(description="Run the live options squeeze monitor.")
    parser.add_argument("--dry-run", action="store_true", help="Skip broker login and run with mocked contracts and synthetic bars.")
    parser.add_argument("--dry-run-live-orders", action="store_true", help="In dry-run mode, exercise enter_live_position()/exit_live_position() via mock broker fills.")
    parser.add_argument("--once", action="store_true", help="Run a single strategy iteration and exit.")
    parser.add_argument("--replay", help="Replay an OHLCV CSV in dry-run mode. CSV must contain datetime/timestamp and Open, High, Low, Close, Volume columns.")
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    if args.replay and not args.dry_run:
        raise SystemExit("--replay requires --dry-run")
    if args.dry_run_live_orders and not args.dry_run:
        raise SystemExit("--dry-run-live-orders requires --dry-run")
    monitor = ShioajiOptionsSmartMonitor(
        dry_run=args.dry_run,
        run_once=args.once,
        replay_path=args.replay,
        dry_run_live_orders=args.dry_run_live_orders,
    )
    monitor.run()
