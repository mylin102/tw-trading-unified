import time
import datetime
import argparse
import pandas as pd
import yaml
import shioaji as sj
from shioaji import TickFOPv1, Exchange
import sys
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from py_vollib.black_scholes.greeks.numerical import delta, gamma, vega
from py_vollib.black_scholes.implied_volatility import implied_volatility
from rich.console import Console

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

# 依序匯入策略所需組件
from options_engine.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment
from options_engine.engine.broker_adapter import ShioajiBrokerAdapter
from options_engine.engine.backtest_engine import should_exit_position, should_take_partial_profit, should_exit_by_time_constraints
from options_engine.engine.backtest_engine import resolve_option_strike
from options_engine.engine.options_strategy import get_mode_profile, get_score_floor, get_stop_loss_pct, get_strategy_weights, infer_mid_trend, resolve_entry_side

try:
    from options_engine.engine.greeks import black_scholes, calculate_dte, find_implied_volatility
except ImportError:
    # 備援路徑處理
    sys.path.append(os.path.join(os.path.dirname(__file__), "src"))
    from options_engine.engine.greeks import black_scholes, calculate_dte, find_implied_volatility

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
        self.fallback_underlying_price = float(self.strategy_cfg.get("fallback_underlying_price", 23000))
        self.monthly_delivery_min_days = int(self.strategy_cfg.get("monthly_delivery_min_days", 7))
        self.strike_rounding = int(self.pricing_cfg.get("strike_rounding", 100))
        self.risk_free_rate = float(self.pricing_cfg.get("risk_free_rate", 0.02))
        self.eod_panic_time = self._parse_hhmm(self.exit_opt.get("eod_panic_time", "13:30"))
        self.eod_passive_window_mins = int(self.exit_opt.get("eod_passive_window_mins", 20))
        self.shutdown_grace_mins = int(self.exit_opt.get("shutdown_grace_mins", 1))
        self.api = None if self.dry_run else shioaji_login.login()
        if self.dry_run_live_orders:
            self.broker = MockBrokerAdapter(self.execution_cfg)
        else:
            self.broker = None if self.dry_run else ShioajiBrokerAdapter(self.api, self.execution_cfg)
        
        self.market_data = {"MTX": {"close": 0.0, "bid": 0.0, "ask": 0.0}, "C": {"close": 0.0, "bid": 0.0, "ask": 0.0}, "P": {"close": 0.0, "bid": 0.0, "ask": 0.0}}
        self.active_contracts = {}
        self.lock = threading.Lock()
        self.position, self.active_side, self.entry_price, self.has_tp1_hit, self.stop_loss_price = 0, None, 0.0, False, 0.0
        self.entry_mtx_price = 0.0
        self.entry_time = None
        self.last_signal = None
        self.last_status_print_at = None
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
        self.replay_stats = {"signals": 0, "directional_signals": 0, "entries": 0, "exits": 0, "tp1_hits": 0}
        
        # 設定日誌路徑
        log_sub_dir = "live_trading" if self.live_trading else "paper_trading"
        log_base = Path(__file__).parent / "logs" / log_sub_dir
        log_base.mkdir(parents=True, exist_ok=True)
        
        self.indicator_log_path = log_base / f"OPTIONS_{datetime.datetime.now().strftime('%Y%m%d')}_indicators.csv"
        self.ledger_path = log_base / "options_trade_ledger.csv"
        if self.api is not None and hasattr(self.api, "set_order_callback"):
            self.api.set_order_callback(self.on_order_event)

    def load_config(self):
        path = Path(__file__).parent / "config" / "options_strategy.yaml"
        with open(path, 'r') as f: return yaml.safe_load(f)

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
        """檢查市場是否開盤 (支援日盤 + 夜盤)"""
        now_minutes = self._minutes_since_midnight(current_time)
        
        # 日盤：08:45 - 13:45
        day_open = 8 * 60 + 45    # 08:45
        day_close = 13 * 60 + 45  # 13:45
        
        # 夜盤：15:00 - 05:00 (隔天)
        night_open = 15 * 60      # 15:00
        night_close = 5 * 60      # 05:00
        
        # 檢查是否為日盤
        if day_open <= now_minutes <= day_close:
            return True, "day"
        
        # 檢查是否為夜盤 (跨日)
        if now_minutes >= night_open or now_minutes <= night_close:
            return True, "night"
        
        return False, "closed"
    
    def _eod_state(self, current_time):
        panic_minutes = (self.eod_panic_time[0] * 60) + self.eod_panic_time[1]
        now_minutes = self._minutes_since_midnight(current_time)
        passive_start = panic_minutes - self.eod_passive_window_mins
        return {
            "is_passive": passive_start <= now_minutes < panic_minutes,
            "is_panic": now_minutes >= panic_minutes,
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
                except:
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
                except:
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
            snap = self.api.snapshots([target_mtx])[0]
            S = snap.close if snap.close > 0 else self.fallback_underlying_price
            
            # 初始化標的行情
            self.market_data["MTX"]["close"] = float(S)
            self.market_data["MTX"]["bid"] = float(getattr(snap, 'buy_price', S) or S)
            self.market_data["MTX"]["ask"] = float(getattr(snap, 'sell_price', S) or S)
            
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
            return True
            
        except Exception as e:
            console.print(f"[red]❌ find_best_contracts 發生異常：[/red] {e}")
            import traceback
            console.print(traceback.format_exc())
            return False

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
                self.market_data[key]["close"] = float(tick.close)
                self.market_data[key]["bid"] = float(getattr(tick, 'bid_price', tick.close))
                self.market_data[key]["ask"] = float(getattr(tick, 'ask_price', tick.close))

    def log_trade(self, action, side, price, note=""):
        data = {"Timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "Mode": self.mode, "Action": action, "Side": side, "Price": price, "Note": note}
        pd.DataFrame([data]).to_csv(self.ledger_path, mode='a', index=False, header=not self.ledger_path.exists())

    def on_order_event(self, stat, msg):
        if not (self.dry_run_live_orders and stat == "MOCK_FILL") and stat != sj.constant.OrderState.FDeal:
            return
        action = str(msg.get("action", ""))
        price = float(msg.get("price", 0.0) or 0.0)
        quantity = int(msg.get("quantity", 0) or 0)
        code = msg.get("code")
        side = self.active_side
        if self.pending_entry and code == self.pending_entry["contract_code"] and action == "Buy":
            side = self.pending_entry["side"]
            self.position += quantity
            self.active_side = side
            self.entry_price = price
            self.entry_mtx_price = self.pending_entry["entry_mtx_price"]
            self.entry_time = self.pending_entry.get("signal_time") or self._current_strategy_time()
            self.has_tp1_hit = False
            self.stop_loss_price = price * (1 - self.stop_loss_pct)
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
        # 即使沒有訊號也要記錄數據 (用於 Streamlit 顯示)
        if not signal:
            row = {
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "score": 0,
                "side": "",
                "price_mtx": self.market_data["MTX"]["close"],
                "mid_trend": "",
                "iv": 0.0,
                "delta": 0.0,
                "gamma": 0.0,
                "vega": 0.0
            }
            pd.DataFrame([row]).to_csv(self.indicator_log_path, mode="a", index=False, header=not self.indicator_log_path.exists())
            return

        # 使用 py_vollib 計算 IV 與 Greeks
        iv, delta_val, gamma_val, vega_val = 0.0, 0.0, 0.0, 0.0
        try:
            side = signal["side"]
            if side in ["C", "P"]:
                quote = self.current_option_quote(side)
                contract = self.active_contracts.get(side)
                
                # 使用合理的預設值進行計算
                strike = getattr(contract, "strike_price", resolve_option_strike(signal["price_mtx"], self.strike_rounding))
                delivery_date = getattr(contract, "delivery_date", None)
                
                # 計算 DTE（天數）
                if delivery_date:
                    dte_years = calculate_dte(delivery_date, now=signal.get("timestamp") or self._current_strategy_time())
                else:
                    dte_years = 3.0 / 365.0
                
                # 使用市場報價
                option_price = quote["mid"]
                
                if option_price > 0 and strike > 0:
                    # 使用 py_vollib 計算 IV
                    option_type = 'c' if side == 'C' else 'p'
                    try:
                        iv = implied_volatility(
                            option_price,
                            signal["price_mtx"],
                            strike,
                            dte_years,
                            self.risk_free_rate,
                            option_type,
                        )
                        
                        # 使用 py_vollib 計算 Greeks
                        delta_val = delta(option_type, signal["price_mtx"], strike, dte_years, self.risk_free_rate, iv)
                        gamma_val = gamma(option_type, signal["price_mtx"], strike, dte_years, self.risk_free_rate, iv)
                        vega_val = vega(option_type, signal["price_mtx"], strike, dte_years, self.risk_free_rate, iv)
                    except Exception as e:
                        # 計算失敗時使用預設值
                        iv = 0.25
                        delta_val = 0.5 if side == 'C' else -0.5
                        gamma_val = 0.02
                        vega_val = 20.0
        except Exception as e:
            console.print(f"[red]Greeks calculation error:[/red] {e}")

        row = {
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "score": signal["score"],
            "side": signal["side"] or "",
            "price_mtx": signal["price_mtx"],
            "mid_trend": signal["mid_trend"] or "",
            "iv": round(iv, 4),
            "delta": round(delta_val, 4),
            "gamma": round(gamma_val, 6),
            "vega": round(vega_val, 4)
        }
        pd.DataFrame([row]).to_csv(self.indicator_log_path, mode="a", index=False, header=not self.indicator_log_path.exists())

    def _fetch_today_futures_bars(self):
        if self.dry_run:
            return self._build_dry_run_bars()
        if not hasattr(self.api, "kbars") or "MTX" not in self.active_contracts:
            return None
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        try:
            bars = self.api.kbars(self.active_contracts["MTX"], start=today, end=today)
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
        return frame

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
        df5_raw = self._fetch_today_futures_bars()
        if df5_raw is not None:
            console.print(f"[debug] Raw bars: {len(df5_raw)} rows, from {df5_raw.index[0]} to {df5_raw.index[-1]}")
        if df5_raw is None or len(df5_raw) < max(30, self.strategy_cfg.get("length", 20) + 5):
            return None
        try:
            p5 = calculate_futures_squeeze(df5_raw, self.strategy_cfg.get("length", 20))
            # Resample correctly and ensure columns are present
            def safe_resample(df, rule):
                res = df.resample(rule).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
                if len(res) < 2: return res
                return calculate_futures_squeeze(res, self.strategy_cfg.get("length", 20))

            p15 = safe_resample(df5_raw, "15min")
            p1h = safe_resample(df5_raw, "1h")

            row = p5.iloc[-1]
            timestamp = row.name
            m15 = p15[p15.index <= timestamp]
            h1 = p1h[p1h.index <= timestamp]

            if m15.empty or h1.empty:
                console.print(f"[yellow]Insufficient MTF data: 15m={len(m15)}, 1h={len(h1)}[/yellow]")
                return None

            # Check for momentum column availability
            has_momentum_15m = "momentum" in m15.columns and not m15["momentum"].empty
            has_momentum_1h = "momentum" in h1.columns and not h1["momentum"].empty
            
            if not has_momentum_15m:
                console.print(f"[yellow]15m momentum not available yet, waiting for more data...[/yellow]")
                return None
            
            # Use available timeframes for alignment score
            available_data = {"5m": p5, "15m": m15}
            if has_momentum_1h:
                available_data["1h"] = h1
            else:
                console.print(f"[yellow]1h momentum not available (insufficient data), using 5m+15m only[/yellow]")
                # Adjust weights for available timeframes
                if len(available_data) == 2:
                    available_data["1h"] = m15  # Use 15m as proxy for 1h

            score = calculate_mtf_alignment(available_data, weights=self.weights)["score"]
            mid_trend = infer_mid_trend(m15)
            side = resolve_entry_side(row, score, row["Close"], self.entry_score, mid_trend=mid_trend, require_mid_trend=True)
            signal = {"score": score, "side": side, "price_mtx": row["Close"], "mid_trend": mid_trend, "timestamp": timestamp}
            self.last_signal = signal
            self.replay_stats["signals"] += 1
            if side:
                self.replay_stats["directional_signals"] += 1
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
        if not self._dte_allows_entry(side, now=signal.get("timestamp")):
            return
        if not self.spread_is_tradeable(side):
            return
        quote = self.current_option_quote(side)
        entry_price = quote["ask"]
        if entry_price <= 0:
            return
        self.position = self.paper_lots
        self.active_side = side
        self.entry_price = entry_price
        self.entry_mtx_price = signal["price_mtx"]
        self.entry_time = signal.get("timestamp") or self._current_strategy_time()
        self.has_tp1_hit = False
        self.stop_loss_price = entry_price * (1 - self.stop_loss_pct)
        self.replay_stats["entries"] += 1
        self.log_trade("PAPER_ENTRY", side, entry_price, f"score={signal['score']:.1f}")

    def enter_live_position(self, side, signal):
        self.submit_live_entry(side, signal, retries=0)

    def submit_live_entry(self, side, signal, retries):
        if self.pending_entry is not None:
            return
        if not self._dte_allows_entry(side, now=signal.get("timestamp")):
            return
        if not self.spread_is_tradeable(side):
            return
        self.sync_contract_quotes()
        contract = self.active_contracts.get(side)
        if contract is None:
            return
        trade = self.broker.place_entry_order(contract, self.paper_lots)
        if trade is None:
            console.print("[red]❌ 下單未執行（可能保證金不足）[/red]")
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
        self.log_trade("LIVE_ENTRY_SUBMITTED", side, getattr(contract, "ask_price", 0.0), f"score={signal['score']:.1f} trade={self.broker.describe_trade(trade)}")
        if self.dry_run_live_orders:
            self._simulate_dry_run_fill(trade, contract, "Buy", self.paper_lots)

    def exit_paper_position(self, action, price, note=""):
        if self.position <= 0 or not self.active_side:
            return
        self.log_trade(action, self.active_side, price, note)
        self.position = 0
        self.active_side = None
        self.entry_price = 0.0
        self.entry_mtx_price = 0.0
        self.entry_time = None
        self.has_tp1_hit = False
        self.stop_loss_price = 0.0
        self.replay_stats["exits"] += 1

    def exit_live_position(self, action, note="", quantity=None):
        self.submit_live_exit(action, note=note, quantity=quantity, retries=0)

    def submit_live_exit(self, action, note="", quantity=None, retries=0):
        if not self.active_side or self.position <= 0 or self.pending_exit_qty > 0:
            return
        self.sync_contract_quotes()
        contract = self.active_contracts.get(self.active_side)
        if contract is None:
            return
        exit_quantity = min(self.position, quantity or self.position)
        bid = self.current_option_quote(self.active_side)["bid"]
        trade = self.broker.place_exit_order(contract, exit_quantity, bid_price=bid)
        if trade is None:
            console.print("[red]❌ 出場下單未執行[/red]")
            return
        self.pending_exit_qty = exit_quantity
        self.pending_exit_reason = action
        self.pending_exit_trade = {
            "submitted_at": datetime.datetime.now(),
            "trade": trade,
            "quantity": exit_quantity,
            "retries": retries,
        }
        self.log_trade(action, self.active_side, self.current_option_quote(self.active_side)["bid"], f"{note} trade={self.broker.describe_trade(trade)}".strip())
        if self.dry_run_live_orders:
            self._simulate_dry_run_fill(trade, contract, "Sell", exit_quantity)

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
            return False
        
        signal_score = signal["score"] if signal else (self.last_signal["score"] if self.last_signal else 999)
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

            if self.position == 0 and signal and signal["side"]:
                if self.live_trading:
                    self.enter_live_position(signal["side"], signal)
                else:
                    self.enter_paper_position(signal["side"], signal)
            self.print_status_summary(signal)
        except Exception as exc:
            console.print(f"[red]Strategy loop error:[/red] {exc}")

    def run(self):
        if not self.find_best_contracts(): return
        if not self.dry_run and self.api is not None:
            self.api.quote.set_on_tick_fop_v1_callback(self.on_tick)
            for con in self.active_contracts.values():
                self.api.quote.subscribe(con, quote_type='tick')
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
            while True:
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
                    console.print(f"[bold green]Market closed ({session}). Exiting monitor for daily settlement...[/bold green]")
                    break
                
                # 夜盤時的特殊處理
                if session == "night":
                    console.print(f"[dim]🌙 夜盤時段 ({now.strftime('%H:%M')})[/dim]")
                
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
