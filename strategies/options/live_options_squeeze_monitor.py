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
from pathlib import Path
from types import SimpleNamespace
from rich.console import Console

# 依序匯入策略所需組件
from options_engine.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment
from core.date_utils import get_session_date_str
from options_engine.engine.broker_adapter import ShioajiBrokerAdapter
from options_engine.engine.backtest_engine import should_exit_position, should_take_partial_profit, should_exit_by_time_constraints
from options_engine.engine.backtest_engine import resolve_option_strike
from options_engine.engine.options_strategy import get_mode_profile, get_score_floor, get_stop_loss_pct, get_strategy_weights, infer_mid_trend, resolve_entry_side

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")

import login.shioaji_login as shioaji_login

try:
    from strategies.futures.squeeze_futures.report.notifier import send_email_notification as _notify
except ImportError:
    _notify = None

console = Console()

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
        self.max_holding_days = self.full_cfg.get("risk_mgmt", {}).get("max_holding_days")
        self.min_dte_to_exit = self.full_cfg.get("risk_mgmt", {}).get("min_dte_to_exit")
        self.execution_cfg = self.full_cfg.get("execution", {})
        self.pricing_cfg = self.full_cfg.get("pricing", {})
        self.max_spread_pct = self.execution_cfg.get("max_spread_pct", 0.05)
        self.paper_lots = self.full_cfg.get("risk_mgmt", {}).get("lots_per_trade", 2)
        self.max_positions = self.full_cfg.get("risk_mgmt", {}).get("max_positions", self.paper_lots)
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
        
        # Parameterized fees (GSD enhancement)
        self.broker_fee_per_side = float(self.execution_cfg.get("broker_fee_per_side", 20.0))
        self.exchange_fee_per_side = float(self.execution_cfg.get("exchange_fee_per_side", 5.0))
        self.shutdown_grace_mins = int(self.exit_opt.get("shutdown_grace_mins", 1))
        self.api = None if self.dry_run else shioaji_login.login()
        if self.dry_run_live_orders:
            self.broker = MockBrokerAdapter(self.execution_cfg)
        else:
            self.broker = None if self.dry_run else ShioajiBrokerAdapter(self.api, self.execution_cfg)
        
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
        self.trailing_stop_pct = float(self.m_cfg.get("trailing_stop_pct", 0))
        self.last_signal = None

        # ThetaGang (sell premium) integration
        self._theta_gang = None
        self._theta_cfg = self.full_cfg.get("theta_gang", {})
        if self._theta_cfg.get("enabled", False):
            try:
                from theta_gang import ThetaGangManager
                self._theta_gang = ThetaGangManager(self.full_cfg, self._bs, self.strike_rounding)
                console.print(f"[bold cyan][ThetaGang] {self._theta_gang.strategy} enabled (auto_regime={self._theta_cfg.get('auto_regime', True)})[/bold cyan]")
            except Exception as e:
                console.print(f"[yellow][ThetaGang] init failed: {e}[/yellow]")
        self.last_status_print_at = None
        self.last_kbars_fetch_at = 0.0
        self.latest_score = 0.0
        self.latest_iv = 0.25
        self.latest_mid_trend = ""
        self.loop_sleep_secs = 60
        self.status_print_secs = 300
        self.pending_entry = None
        self.pending_exit_qty = 0
        self.pending_exit_reason = None
        self.pending_exit_trade = None
        self.order_timeout_secs = int(self.execution_cfg.get("order_timeout_secs", 30))
        self.max_order_retries = int(self.execution_cfg.get("max_order_retries", 1))
        self.replay_bars = self._load_replay_bars(self.replay_path) if self.replay_path else None
        self.replay_cursor = max(0, self.strategy_cfg.get("length", 20) + 5) if self.replay_bars is not None else None
        self.replay_stats = {"signals": 0, "directional_signals": 0, "entries": 0, "exits": 0, "tp1_hits": 0, "blocked_entries": 0, "last_summary_at": 0}
        self._seen_fill_ordnos = set()  # Dedup for live FDeal callbacks

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

    def load_config(self):
        path = Path(__file__).parent / "config" / "options_strategy.yaml"
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
        all_contracts = [c for c in self.api.Contracts.Options[symbol]]
        
        # 2. 取得今日日期
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        
        # 3. 過濾掉已過期合約，並按到期日排序
        valid_contracts = sorted(
            [c for c in all_contracts if c.delivery_date >= today],
            key=lambda x: x.delivery_date
        )
        
        if not valid_contracts:
            return None, []
        
        # 4. 取出最接近的到期日
        nearest_date = valid_contracts[0].delivery_date
        nearest_list = [c for c in valid_contracts if c.delivery_date == nearest_date]
        
        return nearest_date, nearest_list
    
    def get_atm_contracts(self, contracts, spot_price, range_pts=200):
        """根據台指現價，過濾出正負 range_pts 點內的 ATM 合約"""
        atm_list = [
            c for c in contracts
            if abs(c.strike_price - spot_price) <= range_pts
        ]
        return atm_list


    def find_best_contracts(self):
        if self.dry_run:
            return self._setup_dry_run_contracts()
        
        try:
            # 1. 確保合約資訊是最新的 (特別是夜盤剛開始時)
            if self.api:
                # 使用更強健的列表轉換
                try:
                    all_txo = list(self.api.Contracts.Options["TXO"])
                except Exception:
                    # Fallback to empty list on error
                    all_txo = []
                    
                if not all_txo:
                    console.print("[yellow]🔍 正在重新下載合約資訊...[/yellow]")
                    self.api.fetch_contracts()
            
            # 2. 自動尋找最快到期的選擇權合約 (支援週選/月選)
            nearest_date, contracts = self.get_nearest_options("TXO")
            if not contracts:
                console.print("[red]❌ 錯誤：找不到任何有效的 TXO 選擇權合約。[/red]")
                return False
            
            console.print(f"[green]✅ 找到 {len(contracts)} 筆 {nearest_date} 到期合約[/green]")
            
            # 3. 獲取標的期貨並取得現價
            # Shioaji 中 小台指可能是 MTX 或 MXF，優先嘗試 MTX
            mtx_group = []
            for symbol in ["MTX", "MXF"]:
                try:
                    mtx_group = list(self.api.Contracts.Futures[symbol])
                    if mtx_group: 
                        console.print(f"[dim]💡 成功在 {symbol} 分類下找到期貨合約[/dim]")
                        break
                except Exception:
                    # Ignore and try next symbol
                    continue
            
            if not mtx_group:
                console.print("[red]❌ 錯誤：找不到任何有效的台指期貨合約 (MTX/MXF)。[/red]")
                return False
            
            # 過濾標準合約並按到期日排序
            mtx_cons = sorted([c for c in mtx_group if len(c.code) in [5, 6, 7]], key=lambda x: x.delivery_date)
            if not mtx_cons:
                console.print("[red]❌ 錯誤：找不到任何有效的 MTX 期貨合約 (格式篩選後)。[/red]")
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
            cons_at_strike = [c for c in contracts if c.strike_price == atm_strike]
            
            if not cons_at_strike:
                console.print(f"[yellow]⚠️  找不到履約價 {atm_strike} 的 ATM 合約，嘗試最近的合約。[/yellow]")
                # 如果剛好沒這個履約價，就改用 get_atm_contracts 找最近的
                atm_contracts = self.get_atm_contracts(contracts, S, range_pts=200)
                if not atm_contracts:
                    atm_contracts = contracts # 真的找不到就全用
                cons_at_strike = atm_contracts

            # 5. 鎖定監控合約 (MTX, Call, Put)
            calls = [c for c in cons_at_strike if c.option_right == "Call" or str(c.option_right).endswith("Call")]
            puts = [c for c in cons_at_strike if c.option_right == "Put" or str(c.option_right).endswith("Put")]
            
            if not calls or not puts:
                console.print(f"[red]❌ 錯誤：無法配對 Call/Put 合約 (Strike: {atm_strike})[/red]")
                return False
                
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
            
            # [Phase 1 Fix] Validate contracts haven't expired
            today = datetime.date.today()
            for side, contract in [("C", self.active_contracts["C"]), ("P", self.active_contracts["P"])]:
                try:
                    if not hasattr(contract, 'delivery_date') or not contract.delivery_date:
                        continue
                    dd = contract.delivery_date
                    # Shioaji may return str in various formats — normalize
                    if isinstance(dd, str):
                        # Try multiple formats Shioaji might use
                        parsed = None
                        for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]:
                            try:
                                parsed = datetime.datetime.strptime(dd, fmt).date()
                                break
                            except ValueError:
                                continue
                        if parsed is None:
                            console.print(f"[dim]⚠️ Could not parse delivery date '{dd}' for {contract.code}, skipping expiry check[/dim]")
                            continue
                        dd = parsed
                    elif hasattr(dd, 'date'):
                        dd = dd.date()
                    if dd <= today:
                        console.print(f"[red]🚫 Contract {contract.code} expires today or earlier! Rejecting.[/red]")
                        return False
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
        
        # Check if current contracts have expired
        today_str = datetime.date.today().strftime("%Y/%m/%d")
        needs_refresh = False

        for side, contract in [("C", self.active_contracts.get("C")), ("P", self.active_contracts.get("P"))]:
            if not contract:
                needs_refresh = True
                break
            # GSD: Compare standardized YYYY/MM/DD strings
            dd = getattr(contract, 'delivery_date', None)
            if dd and isinstance(dd, str):
                # Clean Shioaji delivery_date format if needed
                dd_clean = dd.replace("-", "/")
                if dd_clean <= today_str:
                    console.print(f"[yellow]⚠️ {side} contract {contract.code} expired (delivery: {dd})[/yellow]")
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
            elif m_contract and (code == getattr(m_contract, "code", None) or code.startswith("MXF")):
                key = "MTX"
            if key:
                self.market_data[key]["close"] = float(tick.close)
                self.market_data[key]["bid"] = float(getattr(tick, 'bid_price', tick.close))
                self.market_data[key]["ask"] = float(getattr(tick, 'ask_price', tick.close))

            # Build 5m bars from MTX ticks
            if key == "MTX":
                price = float(tick.close)
                vol = int(getattr(tick, "volume", 1))
                
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
                else:
                    return

    def on_bidask(self, exchange, bidask):
        """Update bid/ask from BidAsk callback — more frequent than Tick in off-hours."""
        self.last_tick_at = time.time()  # Sentinel: track data freshness
        with self.lock:
            code = bidask.code
            bid = bidask.bid_price[0] if hasattr(bidask.bid_price, '__getitem__') else float(bidask.bid_price)
            ask = bidask.ask_price[0] if hasattr(bidask.ask_price, '__getitem__') else float(bidask.ask_price)
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
                console.print(f"[dim]bidask unmatched: {code} vs C={getattr(c_contract,'code',None)} P={getattr(p_contract,'code',None)} MTX={getattr(m_contract,'code',None)}[/dim]")
                return
            if bid > 0 and ask > 0:
                self.market_data[key]["bid"] = float(bid)
                self.market_data[key]["ask"] = float(ask)
                mid = (bid + ask) / 2
                if self.market_data[key]["close"] <= 0 or key == "MTX":
                    self.market_data[key]["close"] = mid

    def log_trade(self, action, side, price, note="", quantity=None):
        pnl = 0
        point_value = self.pricing_cfg.get("point_value", 50)
        qty = quantity or self.position or 1

        # GSD fix: Explicit whitelist of exit actions that require PnL calculation
        # (instead of fragile substring matching)
        exit_keywords = ["EXIT", "TP1", "TRAIL", "TIME", "REVERSAL", "TRAP", "EOD", "FILL"]
        is_exit_action = any(kw in action for kw in exit_keywords) and self.entry_price > 0

        # Skip non-trade entries (cancelled orders, retries, etc.)
        if any(kw in action for kw in ["CLEARED", "RETRY", "SUBMITTED"]):
            is_exit_action = False

        if is_exit_action:
            gross_pnl = (price - self.entry_price) * point_value * qty
            # 扣除交易成本 (RULES.md Rule 4: PnL Must Include All Costs)
            # 期權手續費 (GSD parameterized)
            broker_fee = self.broker_fee_per_side * 2 * qty  # 進出各一次
            exchange_fee = self.exchange_fee_per_side * 2 * qty
            # 交易稅: 期權約 0.1% 權利金
            tax_rate = self.pricing_cfg.get("tax_rate", 0.001)
            tax = (self.entry_price + price) * point_value * tax_rate * qty
            pnl = round(gross_pnl - broker_fee - exchange_fee - tax, 0)

            # GSD validation: warn if exit PnL is 0 (indicates missing action keyword)
            if pnl == 0 and "ENTRY" not in action:
                console.print(f"[yellow]⚠️ Exit PnL=0 for {action} {side} @ {price} — check action keyword[/yellow]")
        # 從既有 ledger 累計 balance
        balance = 0
        if self.ledger_path.exists():
            try:
                prev = pd.read_csv(self.ledger_path)
                balance = pd.to_numeric(prev["PnL"], errors="coerce").fillna(0).sum()
            except Exception as e:
                console.print(f"[yellow]⚠️ Ledger read error: {e} — balance reset to 0[/yellow]")
        balance += pnl
        data = {
            "Timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Mode": self.mode, "Action": action, "Side": side,
            "Price": price, "Quantity": qty,
            "PnL": pnl, "Balance": balance, "Note": str(note).replace(",", ";"),
        }
        pd.DataFrame([data]).to_csv(self.ledger_path, mode='a', index=False, header=not self.ledger_path.exists())

    def on_order_event(self, stat, msg):
        # set_order_callback receives OrderState enum:
        #   OrderState.FuturesDeal / OrderState.StockDeal — actual fills
        #   OrderState.FuturesOrder / OrderState.StockOrder — order status changes
        # Only process deal events (actual fills), ignore order status changes
        is_deal = stat in (sj.constant.OrderState.FuturesDeal, sj.constant.OrderState.StockDeal)
        if not (self.dry_run_live_orders and stat == "MOCK_FILL") and not is_deal:
            return
        action = str(msg.get("action", ""))
        price = float(msg.get("price", 0.0) or 0.0)
        quantity = int(msg.get("quantity", 0) or 0)
        code = msg.get("code")
        side = self.active_side
        if self.pending_entry and code == self.pending_entry["contract_code"] and action == "Buy":
            side = self.pending_entry["side"]
            ordno = msg.get("ordno", "")
            if ordno and ordno in self._seen_fill_ordnos:
                console.print(f"[yellow]⚠️ Duplicate fill ignored: ordno={ordno}[/yellow]")
                return
            if ordno:
                self._seen_fill_ordnos.add(ordno)
            self.position += quantity
            self.active_side = side
            self.entry_price = price
            self.entry_mtx_price = self.pending_entry["entry_mtx_price"]
            self.entry_time = self.pending_entry.get("signal_time") or self._current_strategy_time()
            self.has_tp1_hit = False
            self.stop_loss_price = price * (1 - self.stop_loss_pct)
            self.peak_premium = price
            self.replay_stats["entries"] += 1
            self.log_trade("LIVE_ENTRY_FILLED", side, price, f"qty={quantity}")
            if _notify:
                _notify(f"[TXO] ENTRY {side} @ {price:.1f}", f"🟢 ENTRY {side} qty={quantity} @ {price:.1f}")
            if self.position >= self.paper_lots:
                self.pending_entry = None
            return
        if self.active_side and action == "Sell":
            self.position = max(0, self.position - quantity)
            self.log_trade("LIVE_EXIT_FILLED", self.active_side, price, f"qty={quantity} reason={self.pending_exit_reason or ''}".strip())
            if _notify:
                _notify(f"[TXO] EXIT {self.active_side} @ {price:.1f}", f"🔴 EXIT {self.active_side} qty={quantity} @ {price:.1f} reason={self.pending_exit_reason or ''}")
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
            signal_desc = f"score={signal['score']:.1f} side={signal['side'] or '-'} trend={signal['mid_trend'] or '-'}"
        elif self.last_signal:
            signal_desc = f"score={self.last_signal['score']:.1f} side={self.last_signal['side'] or '-'} trend={self.last_signal['mid_trend'] or '-'}"
        console.print(
            f"[cyan][{self.status_mode_label()}][/cyan] mode={self.mode} position={position_desc} "
            f"mtx={self.market_data['MTX']['close']:.1f} signal={signal_desc}"
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

    def spread_is_tradeable(self, side):
        quote = self.current_option_quote(side)
        if quote["mid"] <= 0:
            return False
        spread_pct = max(0.0, quote["ask"] - quote["bid"]) / quote["mid"]
        return spread_pct <= self.max_spread_pct

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
        score = float(signal["score"]) if signal else self.latest_score
        mid_trend = (signal["mid_trend"] or "") if signal else self.latest_mid_trend
        side_label = (signal["side"] or "") if signal else ""

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

        from core.date_utils import get_session
        now = datetime.datetime.now()
        
        # GSD: Base on signal data to ensure all indicators are preserved
        row = signal.copy() if signal else {}
        
        # Standardize and add Greeks
        row.update({
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "session": get_session(now),
            "score": score,  # Use the freshly calculated score
            "side": side_label,
            "price_mtx": price_mtx,
            "strike": strike,
            "dte": round(dte_years * 365, 2),
            "mid_trend": mid_trend,
            "iv": round(iv, 4),
            "delta": round(delta_val, 4),
            "gamma": round(gamma_val, 6),
            "vega": round(vega_val, 4),
            # Backwards compatibility/aliases for dashboard
            "vwap": price_mtx,
            "sqz_on": row.get("sqz_on", row.get("squeeze_on", False)),
            "fired": row.get("fired", False),
        })
        
        if iv > 0:
            self.latest_iv = iv
            
        # GSD Fix: Support dynamic column expansion
        df_row = pd.DataFrame([row])
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
        # Night session data (15:00-05:00) is stored under the calendar date when it started.
        # e.g., 20:00 on 4/13 → server key = 2026-04-13, not 2026-04-14.
        today = datetime.datetime.now()
        if today.hour < 5:
            today = today - datetime.timedelta(days=1)
        date_str = today.strftime("%Y-%m-%d")
        try:
            bars = self.api.kbars(self.active_contracts["MTX"], start=date_str, end=date_str)
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
        
        # [DATA GAP FIX] 驗證並填充資料缺口
        frame = self._validate_and_fill_kbar_gaps(frame)
        
        return frame

    def _validate_kbar_data(self, df):
        """驗證kbar資料完整性
        
        Args:
            df: pandas DataFrame with datetime index
            
        Returns:
            tuple: (is_valid, message)
        """
        if df is None or df.empty:
            return False, "資料為空"

        # BUG FIX 2026-04-13: Lower bar requirement for night sessions.
        # Night session may have as few as 10 fresh 5-min bars.
        from core.date_utils import is_night_session as _is_night
        is_night = _is_night(datetime.datetime.now())
        min_bars_required = 10 if is_night else 30
        if len(df) < min_bars_required:
            return False, f"資料不足: {len(df)}根 < {min_bars_required}根"
        
        # 檢查時間間隔連續性
        if len(df) > 1:
            time_diffs = df.index.to_series().diff().dt.total_seconds() / 60  # 轉換為分鐘

            # 跳過第一個NaN值
            if len(time_diffs) > 1:
                valid_diffs = time_diffs.iloc[1:].dropna()
                if len(valid_diffs) > 0:
                    max_gap = valid_diffs.max()
                    min_gap = valid_diffs.min()

                    # BUG FIX 2026-04-13: Night session has natural 375-min gap
                    # (day close 13:45 → night open 15:00 = 75 min, but server
                    # may return combined data with 300+ min gap).
                    # Only check max_gap during continuous trading hours.
                    import datetime as _dt
                    is_night = _dt.datetime.now().hour >= 15 or _dt.datetime.now().hour < 5
                    max_allowed_gap = 380 if is_night else 30  # 380 = covers 13:45→15:00 gap

                    # 檢查是否有異常間隔
                    if max_gap > max_allowed_gap:
                        return False, f"資料缺口過大: {max_gap:.0f}分鐘"
                    
                    # 檢查間隔是否一致（應為5分鐘）
                    # BUG FIX 2026-04-13: Shioaji kbars() returns 1-min bars at night.
                    # Don't reject 1-min data during night sessions.
                    import datetime as _dt2
                    is_night2 = _dt2.datetime.now().hour >= 15 or _dt2.datetime.now().hour < 5
                    if not is_night2 and (abs(min_gap - 5) > 1 or abs(max_gap - 5) > 1):
                        console.print(f"[dim]⚠️ Kbar間隔異常: min={min_gap:.1f}min, max={max_gap:.1f}min[/dim]")
        
        # 檢查價格有效性
        required_columns = ["Open", "High", "Low", "Close", "Volume"]
        for col in required_columns:
            if col not in df.columns:
                return False, f"缺少必要欄位: {col}"

            # 檢查是否有NaN值
            nan_count = df[col].isna().sum()
            if nan_count > 0:
                return False, f"欄位 {col} 有 {nan_count} 個NaN值"

            # 檢查價格合理性
            if col in ["Open", "High", "Low", "Close"]:
                if (df[col] <= 0).any():
                    return False, f"欄位 {col} 有非正數值"

        return True, "資料完整"

    def _fill_small_kbar_gaps(self, df, max_gap_minutes=15):
        """填充小間隔的kbar資料缺口
        
        Args:
            df: pandas DataFrame with datetime index
            max_gap_minutes: 最大允許填充的缺口大小（分鐘）
            
        Returns:
            pandas DataFrame: 填充後的資料
        """
        if df is None or df.empty or len(df) < 2:
            return df
        
        try:
            # 確保索引是DatetimeIndex且已排序
            df = df.sort_index()
            
            # 計算時間間隔
            time_diffs = df.index.to_series().diff().dt.total_seconds() / 60
            
            if len(time_diffs) > 1:
                # 找到需要填充的缺口
                gaps = []
                for i in range(1, len(time_diffs)):
                    gap = time_diffs.iloc[i]
                    if gap > 5 and gap <= max_gap_minutes:  # 5分鐘是正常間隔
                        prev_time = df.index[i-1]
                        curr_time = df.index[i]
                        gaps.append((prev_time, curr_time, gap))
                
                if gaps:
                    console.print(f"[dim]🔧 發現 {len(gaps)} 個小資料缺口，進行填充...[/dim]")
                    
                    # 重新採樣到5分鐘頻率
                    start_time = df.index[0]
                    end_time = df.index[-1]
                    
                    # 創建完整的5分鐘時間索引
                    full_index = pd.date_range(
                        start=start_time.floor('5min'),
                        end=end_time.ceil('5min'),
                        freq='5min'
                    )
                    
                    # 重新索引並填充
                    df_reindexed = df.reindex(full_index)
                    
                    # 前向填充（最多填充3根kbar）
                    fill_limit = min(3, max_gap_minutes // 5)
                    df_filled = df_reindexed.ffill(limit=fill_limit)
                    
                    # 移除完全為NaN的行（無法填充的大缺口）
                    df_filled = df_filled.dropna(how='all')
                    
                    console.print(f"[green]✓ 資料填充完成: {len(df)} -> {len(df_filled)} 根kbar[/green]")
                    return df_filled
            
            return df
            
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
                    df_warm = df_hist.tail(100)
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
            if bars is not None and len(bars) >= 30:
                # [Wave 2 optimization] Convert pre-filled bars to deque format
                for _, row in bars[["Open", "High", "Low", "Close", "Volume"]].iterrows():
                    bar_dict = {
                        "open": row["Open"],
                        "high": row["High"],
                        "low": row["Low"],
                        "close": row["Close"],
                        "volume": row["Volume"],
                        "ts": row.name,  # DataFrame index is timestamp
                    }
                    self._mtx_tick_bars_deque.append(bar_dict)
                self._mtx_tick_bars_cache = bars[["Open", "High", "Low", "Close", "Volume"]].copy()
                console.print(f"[green]Pre-filled {len(self._mtx_tick_bars_deque)} MTX bars from kbars[/green]")
        except Exception:
            pass

    def _get_tick_bars_fallback(self):
        """Fallback: use tick-built bars when kbars API is unavailable."""
        df = self._get_mtx_tick_bars_df()
        if len(df) >= 30:
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
        # [DATA GAP FIX] 獲取並驗證kbar資料
        df5_raw = self._fetch_today_futures_bars()
        
        # 如果API資料無效，嘗試備用資料源
        if df5_raw is None:
            console.print("[yellow]⚠️ API資料獲取失敗，嘗試備用資料源[/yellow]")
            df5_raw = self._get_tick_bars_fallback()
        
        # 驗證資料完整性
        if df5_raw is not None:
            is_valid, msg = self._validate_kbar_data(df5_raw)
            if not is_valid:
                console.print(f"[yellow]⚠️ Kbar資料驗證失敗: {msg}[/yellow]")
                
                # 嘗試填充小缺口
                df5_filled = self._fill_small_kbar_gaps(df5_raw)
                is_valid_filled, msg_filled = self._validate_kbar_data(df5_filled)
                
                if is_valid_filled:
                    console.print("[green]✓ 資料填充後驗證通過，使用填充後資料[/green]")
                    df5_raw = df5_filled
                else:
                    console.print(f"[red]✗ 資料無法修復: {msg_filled}[/red]")
                    df5_raw = None
        
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

        # BUG FIX 2026-04-13: Lower bar requirement for night sessions.
        # At session start (15:00), only a few bars exist. 30 is too strict.
        # Use 10 for night session (15:00-05:00), 30 for day session.
        from core.date_utils import is_night_session
        is_night = is_night_session(datetime.datetime.now())
        min_bars = 10 if is_night else max(30, self.strategy_cfg.get("length", 20) + 5)
        if len(df5_raw) < min_bars:
            console.print(f"[yellow]⚠️ Not enough bars for indicators: {len(df5_raw)} < {min_bars} (night={is_night})[/yellow]")
            self.record_signal_snapshot(None)
            return None

        try:
            p5 = calculate_futures_squeeze(df5_raw, self.strategy_cfg.get("length", 20))
            # Resample correctly and ensure columns are present
            def safe_resample(df, rule):
                res = df.resample(rule).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
                if len(res) < 2:
                    # Not enough data to calculate squeeze
                    return res
                return calculate_futures_squeeze(res, self.strategy_cfg.get("length", 20))

            p15 = safe_resample(df5_raw, "15min")
            p1h = safe_resample(df5_raw, "1h")

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
                "squeeze_on": safe_bool(row.get("sqz_on")),
                "fired": safe_bool(row.get("fired")),
                "bullish_align": safe_bool(row.get("bullish_align")),
                "bearish_align": safe_bool(row.get("bearish_align")),
            }
            # GSD: Include all raw indicators for dashboard visibility
            for k, v in row_data.items():
                if k not in signal:
                    signal[k] = v

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

            if not signal["side"]:
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

    def enter_paper_position(self, side, signal):
        if self.position >= self.max_positions:
            console.print(f"[red]🚫 enter_paper_position blocked: max positions ({self.max_positions}) reached (currently {self.position})[/red]")
            return
        if not self._dte_allows_entry(side, now=signal.get("timestamp")):
            return
        if not self.spread_is_tradeable(side):
            console.print(f"[yellow]⚠️ enter_paper_position blocked: spread too wide for {side}[/yellow]")
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
            
        if not self._paper_margin_check(entry_price):
            return
        self.position = self.paper_lots
        self.active_side = side
        self.entry_price = entry_price
        self.entry_mtx_price = signal["price_mtx"]
        self.entry_time = signal.get("timestamp") or self._current_strategy_time()
        self.has_tp1_hit = False
        self.stop_loss_price = entry_price * (1 - self.stop_loss_pct)
        self.peak_premium = entry_price
        self.replay_stats["entries"] += 1
        self.log_trade("PAPER_ENTRY", side, entry_price, f"score={signal['score']:.1f}")

    def enter_live_position(self, side, signal):
        self.submit_live_entry(side, signal, retries=0)

    def submit_live_entry(self, side, signal, retries):
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
        trade = self.broker.place_entry_order(contract, self.paper_lots)
        if trade is None:
            console.print("[red]❌ 下單未執行（可能保證金不足）[/red]")
            self._audit_signal("LIVE_ENTRY_SUBMITTED", side, signal, "place_order_returned_none")
            return
        self.pending_entry = {
            "side": side,
            "contract_code": contract.code,
            "entry_mtx_price": signal["price_mtx"],
            "signal_time": signal.get("timestamp"),
            "submitted_at": datetime.datetime.now(),
            "trade": trade,
            "retries": retries,
        }
        self._audit_signal("LIVE_ENTRY_SUBMITTED", side, signal, "")
        self.log_trade("LIVE_ENTRY_SUBMITTED", side, getattr(contract, "ask_price", 0.0), f"score={signal['score']:.1f} trade={self.broker.describe_trade(trade)}")
        if self.dry_run_live_orders:
            self._simulate_dry_run_fill(trade, contract, "Buy", self.paper_lots)

    def exit_paper_position(self, action, price, note=""):
        if self.position <= 0 or not self.active_side:
            return
        exit_qty = self.position
        self.position = 0
        self.log_trade(action, self.active_side, price, note, quantity=exit_qty)
        self.active_side = None
        self.entry_price = 0.0
        self.entry_mtx_price = 0.0
        self.entry_time = None
        self.has_tp1_hit = False
        self.stop_loss_price = 0.0
        self.peak_premium = 0.0
        self.cooldown_until = self.cooldown_bars
        self.replay_stats["exits"] += 1

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
        trade = self.broker.place_exit_order(contract, exit_quantity, bid_price=bid)
        if trade is None:
            console.print("[red]❌ 出場下單未執行[/red]")
            self._audit_signal("LIVE_EXIT_SUBMITTED", self.active_side, {"action": action, "note": note}, "place_order_returned_none")
            return
        self.pending_exit_qty = exit_quantity
        self.pending_exit_reason = action
        self.pending_exit_trade = {
            "submitted_at": datetime.datetime.now(),
            "trade": trade,
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
        if self.pending_entry and self._order_age_secs(self.pending_entry) >= self.order_timeout_secs:
            pending = dict(self.pending_entry)
            trade = pending.get("trade")
            self.broker.cancel_order(trade)
            self._clear_stale_entry("entry timeout cancelled")
            if pending.get("retries", 0) < self.max_order_retries:
                retry_signal = {
                    "score": self.last_signal["score"] if self.last_signal else 0,
                    "price_mtx": pending["entry_mtx_price"],
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

    def _margin_sufficient(self):
        """Check account margin before live entry."""
        if not self.api:
            return True
        try:
            margin = self.api.margin(self.api.futopt_account)
            equity = margin.equity
            reserve_pct = 0.20
            available = equity * (1 - reserve_pct)
            # 選擇權買方需要權利金，用 order_margin_premium 或 fallback 估算
            required = margin.order_margin_premium if margin.order_margin_premium > 0 else 10000
            if available < required:
                console.print(f"[red]Margin check: equity={equity:.0f} available={available:.0f} < required={required:.0f}[/red]")
                return False
            return True
        except Exception as e:
            console.print(f"[yellow]Margin check failed: {e} — allowing order[/yellow]")
            return True

    def _paper_margin_check(self, entry_price):
        """
        Paper mode margin check — enforce capital limits.
        Buyer: need premium × 50 × lots
        Seller (spread): need wing_width × 50 × lots
        """
        risk_cfg = self.full_cfg.get("risk_mgmt", {})
        initial_capital = float(risk_cfg.get("initial_capital", 40000))
        reserve_pct = 0.20
        available = initial_capital * (1 - reserve_pct)

        # Adjust by realized PnL from ledger
        if self.ledger_path.exists():
            try:
                prev = pd.read_csv(self.ledger_path)
                realized_pnl = pd.to_numeric(prev["PnL"], errors="coerce").fillna(0).sum()
                available = (initial_capital + realized_pnl) * (1 - reserve_pct)
            except Exception:
                pass

        lots = self.paper_lots
        required = entry_price * 50 * lots if entry_price > 0 else 10000 * lots

        if available < required:
            console.print(f"[red]⛔ Paper margin: available={available:.0f} < required={required:.0f} (capital={initial_capital:.0f})[/red]")
            return False
        console.print(f"[dim]Paper margin OK: available={available:.0f} >= required={required:.0f}[/dim]")
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
                        for i, r in enumerate(rows):
                            action = r.get("Action", "")
                            qty = int(r.get("Quantity", 0) or 0)
                            if "ENTRY" in action:
                                current_qty = qty  # 進場設為該口數
                                last_side = r.get("Side")
                                last_entry_price = float(r.get("Price", 0))
                            elif "TP1" in action:
                                # TP1 減碼: 從持倉減去該口數
                                current_qty = max(0, current_qty - qty)
                            elif "EXIT" in action or "PANIC" in action or "TRAIL" in action or "TIME" in action or "REVERSAL" in action:
                                current_qty = 0  # 完全出場
                                last_side = None

                        if current_qty > 0 and last_side:
                            self.position = current_qty
                            self.active_side = last_side
                            self.entry_price = last_entry_price
                            self.stop_loss_price = self.entry_price * (1 - self.stop_loss_pct)
                            self.peak_premium = self.entry_price
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

    def manage_open_position(self, signal):
        if self.position <= 0 or not self.active_side:
            return False
        
        now = signal.get("timestamp") if signal else self._current_strategy_time()
        quote = self.current_option_quote(self.active_side)
        exit_price = quote["bid"]
        contract = self.active_contracts.get(self.active_side)
        
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
        
        signal_score = signal["score"] if signal else (self.last_signal["score"] if self.last_signal else 999)

        # 3. Score reversal exit: 趨勢翻轉才出場
        #    修正: 出場門檻比進場寬 (60% of entry_score)，防止 whipsaw
        #    買 P 時 score 很負，翻正超過 reversal_threshold → 趨勢反轉
        #    買 C 時 score 很正，翻負超過 reversal_threshold → 趨勢反轉
        reversal_threshold = self.entry_score * 0.67  # 60 → 40
        
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
                self.position = 1
                self.has_tp1_hit = True
                self.replay_stats["tp1_hits"] += 1
                self.log_trade(f"{self.status_mode_label()}_TP1", self.active_side, exit_price, f"score={signal_score:.1f}")
        if should_exit_position(
            exit_price,
            self.entry_price,
            self.stop_loss_pct,
            signal_score,
            self.has_tp1_hit,
            score_floor=self.score_floor,
        ):
            if self.live_trading:
                self.exit_live_position("LIVE_EXIT_SUBMITTED", f"score={signal_score:.1f}")
            else:
                self.exit_paper_position("PAPER_EXIT", exit_price, f"score={signal_score:.1f}")
            return True
        return False

    def run_strategy_logic(self):
        try:
            # GSD: Update log paths dynamically to handle session rollovers (e.g. 15:00)
            self._update_log_paths()
            
            now = self._current_strategy_time()
            eod_state = self._eod_state(now)
            self.refresh_live_orders()
            signal = self.fetch_live_signal()
            
            if self.position > 0:
                cur_p = self.market_data[self.active_side]["close"]
                cur_bid = self.market_data[self.active_side]["bid"]
                cur_ask = self.market_data[self.active_side]["ask"]
                if self.manage_open_position(signal):
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
                
                # ThetaGang: sell premium when squeeze is on (ranging market)
                if self._theta_gang and signal and self.cooldown_until <= 0:
                    squeeze_on = signal.get("squeeze_on", False) if isinstance(signal, dict) else False
                    auto_regime = self._theta_cfg.get("auto_regime", True)
                    use_theta = (auto_regime and squeeze_on) or (not auto_regime)

                    # Manage existing ThetaGang position
                    if self._theta_gang.position and self._theta_gang.position.is_open:
                        spot = float(self.market_data["MTX"]["close"])
                        contract = self.active_contracts.get("C") or self.active_contracts.get("P")
                        dte_years = float(self._dte(getattr(contract, "delivery_date", None))) if contract else 0.03
                        iv = self.latest_iv or 0.25
                        exit_info = self._theta_gang.evaluate_exit(spot, iv, dte_years, squeeze_on)
                        if exit_info:
                            pos = self._theta_gang.close_position()
                            # GSD fix: ThetaGang has pre-computed PnL, don't recalculate through log_trade
                            theta_pnl = round(exit_info["pnl"], 0)
                            theta_row = {
                                "Timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "Mode": self.mode, "Action": "THETA_EXIT", "Side": pos.strategy,
                                "Price": 0, "Quantity": pos.quantity,
                                "PnL": theta_pnl, "Balance": theta_pnl,
                                "Note": f"{exit_info['reason']} credit={pos.net_credit:.0f} pnl={exit_info['pnl']:.0f}",
                            }
                            pd.DataFrame([theta_row]).to_csv(self.ledger_path, mode='a', index=False, header=not self.ledger_path.exists())
                            console.print(f"[bold yellow]🔻 [ThetaGang] EXIT {pos.strategy}: {exit_info['reason']} PnL={exit_info['pnl']:.0f}[/bold yellow]")
                            self.cooldown_until = self.cooldown_bars
                        return

                    # Try ThetaGang entry
                    if use_theta and not (self._theta_gang.position and self._theta_gang.position.is_open):
                        spot = float(self.market_data["MTX"]["close"])
                        contract = self.active_contracts.get("C") or self.active_contracts.get("P")
                        dte_years = float(self._dte(getattr(contract, "delivery_date", None))) if contract else 0.03
                        iv = self.latest_iv or 0.25
                        entry_info = self._theta_gang.evaluate_entry(spot, iv, dte_years, squeeze_on)
                        if entry_info:
                            pos = self._theta_gang.open_position(entry_info)
                            # Create a readable string of position legs
                            legs_str = " | ".join(f"{leg.action} {leg.side}{leg.strike}" for leg in pos.legs)
                            self.log_trade("THETA_ENTRY", "THETA", 0,
                                           f"credit={pos.net_credit:.0f} max_loss={pos.max_loss:.0f} strategy={pos.strategy} [{legs_str}]")
                            console.print(f"[bold cyan]🔺 [ThetaGang] ENTRY {pos.strategy}: credit={pos.net_credit:.0f} [{legs_str}][/bold cyan]")
                            return

                if signal and signal["side"]:
                    if self.cooldown_until > 0:
                        console.print(f"[dim]Signal {signal['side']} ignored during cooldown[/dim]")
                        self.replay_stats["blocked_entries"] += 1
                    elif self.live_trading:
                        console.print(f"[bold green]>>> ENTRY SIGNAL: {signal['side']} score={signal['score']:.1f}[/bold green]")
                        self.enter_live_position(signal["side"], signal)
                    else:
                        # Track: did paper entry succeed or fail?
                        before_pos = self.position
                        self.enter_paper_position(signal["side"], signal)
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
                executed = rs["entries"]
                if total_dir > 0:
                    rate = executed / total_dir * 100
                    console.print(f"[dim]📊 60s summary: {rs['signals']} signals, {total_dir} directional, {executed} entries, {blocked} blocked ({rate:.0f}% exec rate)[/dim]")

            self.print_status_summary(signal)
        except Exception as exc:
            console.print(f"[red]Strategy loop error:[/red] {exc}")

    def run(self):
        # find_best_contracts may already be called from main.py
        if not self.active_contracts:
            if not self.find_best_contracts():
                return
            self.pre_fill_bars()
        if not self.dry_run and self.api is not None:
            # Recover position from API on startup to prevent duplicate entries
            self._recover_position_from_api()
            # Don't override callback or subscribe — main.py handles it
        console.print(f">>> [{'DRY RUN' if self.dry_run and not self.dry_run_live_orders else self.status_mode_label() + ' MODE'}] [EXIT-SNIPER ENABLED] Monitor Running <<<")
        if self.dry_run_live_orders:
            console.print("[green]Dry live-orders mode active. Mock broker fills will exercise live entry/exit callbacks without real broker login.[/green]")
        elif self.dry_run:
            console.print("[green]Dry run active. Broker login, quote subscription, and order placement are skipped.[/green]")
        elif self.live_trading:
            console.print("[yellow]Live mode active. Orders will be submitted through the broker adapter and local state will follow fill callbacks.[/yellow]")
        else:
            console.print("[green]Paper mode active. Entries and exits will be simulated in the trade ledger.[/green]")
        self.print_status_summary(force=True)
        try:
            self._running = True
            while self._running:
                # [Wave 1 Fix] Check for restart flag from dashboard
                if os.path.exists(".restart"):
                    console.print("[bold yellow]🔄 Restart flag detected. Exiting Options Monitor for supervisor...[/bold yellow]")
                    break

                if self.dry_run and self.run_once:
                    self.run_strategy_logic()
                    console.print("[bold green]Dry run completed one strategy iteration.[/bold green]")
                    break
                if self.dry_run and self.replay_bars is not None and self.replay_cursor is not None and self.replay_cursor > len(self.replay_bars):
                    console.print("[bold green]Dry run replay completed.[/bold green]")
                    break
                
                now = self._current_strategy_time()
                
                # 檢查市場是否開盤 (支援日盤 + 夜盤)
                market_open, session = self._is_market_open(now)
                if not market_open:
                    console.print(f"[dim]Market closed ({session}). Waiting for next session...[/dim]")
                    time.sleep(60)
                    continue

                # 夜盤時後的特殊處理
                if session == "night":
                    console.print(f"[dim]🌙 夜盤時段 ({now.strftime('%H:%M')})[/dim]")
                
                # [Phase 1 Fix] Check options data freshness
                self._check_options_contract_staleness()
                
                # [GSD Fix] Save options kbar data to CSV
                self._save_options_bar()

                self.run_strategy_logic()
                if not (self.dry_run and self.replay_bars is not None):
                    time.sleep(self.loop_sleep_secs)
        except KeyboardInterrupt:
            pass
        finally:
            shioaji_login.logout()


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
