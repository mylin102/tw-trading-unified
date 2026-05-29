import os
import time
import pandas as pd
import random
import json
from datetime import datetime, timedelta
from pathlib import Path
import yaml
from rich.console import Console
import shioaji as sj
from core.dashboard_data import build_stock_orders_from_trades
from strategies.stocks.scanner import StockScanner
from strategies.stocks.data_storage import StockDataStorage
from strategies.stocks.position_state import (
    position_state_path,
    load_position_state,
    save_position_state,
    merge_recovery,
)

ROOT = Path(__file__).parent.parent.parent
MKT_LOGS = ROOT / "logs" / "market_data"
TRADE_LOGS = ROOT / "exports" / "trades"
MKT_LOGS.mkdir(parents=True, exist_ok=True)
TRADE_LOGS.mkdir(parents=True, exist_ok=True)

console = Console()

class StockMonitor:
    def __init__(self, api, config_path, dry_run=False):
        self.api = api
        self.dry_run = dry_run
        
        with open(config_path, "r", encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)
            
        stk_cfg = self.cfg.get("stocks", {})
        self.watchlist = stk_cfg.get("watchlist", ["2330"])
        self.total_budget = stk_cfg.get("total_portfolio_budget", 100000)
        self.capital_per_trade = stk_cfg.get("capital_per_trade", 20000)
        self.strat_name = stk_cfg.get("strategy", "scout_strategy")
        self.live_trading = self.cfg.get("live_trading", False)
        
        # 狀態標籤
        self.mode_tag = "LIVE" if (self.live_trading and not dry_run) else "PAPER"
        self.date_str = datetime.now().strftime("%Y%m%d")
        self.ledger_path = TRADE_LOGS / f"STOCK_{self.date_str}_{self.mode_tag}_trades.csv"
        self.orders_path = TRADE_LOGS / f"STOCK_{self.date_str}_{self.mode_tag}_orders.json"
        
        self.positions = {} 
        self.pending_orders = {} # {ticker: {"order_id": str, "time": datetime}}
        self.running = False
        
        # 空頭防禦
        bear_cfg = stk_cfg.get("bear_defense", {})
        self.bear_defense = bear_cfg.get("enabled", False)
        self.market_ema_length = bear_cfg.get("market_ema_length", 60)
        self.max_daily_loss = bear_cfg.get("max_daily_loss", 3000)
        self.max_consecutive_losses = bear_cfg.get("max_consecutive_losses", 3)
        self.bear_max_positions = bear_cfg.get("bear_max_positions", 1)
        self.normal_max_positions = bear_cfg.get("normal_max_positions", 3)
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.is_bear_market = False
        
        # 💡 GSD: Integrated Scanner for Pattern Recognition (CANSLIM)
        self.scanner = StockScanner(self.api)
        self.scan_results = {} # {ticker: {"pattern": str, "pivot": float}}
        
        # 數據儲存管理器
        self.data_storages = {}  # {ticker: StockDataStorage}

    def _run_daily_scan(self):
        """執行日線級別型態掃描，更新 Pivot 點"""
        console.print("[cyan]🔍 Running daily pattern scan for CANSLIM...[/cyan]")
        df_scan = self.scanner.scan_squeeze(self.watchlist, self.cfg)
        if not df_scan.empty:
            for _, row in df_scan.iterrows():
                self.scan_results[row["ticker"]] = {
                    "pattern": row["pattern"],
                    "pivot": row["pivot"]
                }
            console.print(f"[green]✅ Pattern scan complete. Tagged {len(self.scan_results)} tickers.[/green]")

    def _run_daily_scan_try(self):
        """Skip CANSLIM scan on startup to avoid VPN hang. 4h periodic retry handles it."""
        pass

    def get_current_exposure(self):
        return sum([p["qty"] * p["entry_price"] for p in self.positions.values()])

    def _recover_positions_from_ledger(self):
        """Recover overnight positions from previous day's trade ledger.

        Restores self.positions and writes to position_state.json.
        Does NOT write to today's trade ledger — the ledger is an immutable
        event log and must not contain synthetic entries.

        Dashboard reads position_state.json for open positions instead
        of scanning the ledger for OVERNIGHT_RECOVERY entries.
        """
        # GSD Fix: Skip if already recovered (position_state exists for today)
        state_file = position_state_path(TRADE_LOGS, self.date_str, self.mode_tag)
        today_state = load_position_state(state_file)
        if today_state:
            for ticker, pos in today_state.items():
                self.positions[str(ticker)] = {
                    "stage": "HOLD",
                    "entry_price": pos.get("avg_cost", 0.0),
                    "qty": int(pos.get("qty", 0)),
                }
            if self.positions:
                console.print(f"[dim]♻️ Already recovered {len(self.positions)} positions from today's state file[/dim]")
            return

        # Find the most recent ledger starting from today back to 7 days
        ledger_file = None
        last_ledger_date = None
        for i in range(0, 8):
            check_date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
            possible_file = TRADE_LOGS / f"STOCK_{check_date}_{self.mode_tag}_trades.csv"
            console.print(f"[dim]🔍 Checking recovery ledger: {possible_file} (exists: {possible_file.exists()})[/dim]")
            if possible_file.exists():
                ledger_file = possible_file
                last_ledger_date = check_date
                break

        if not ledger_file:
            console.print(f"[dim]📂 No previous stock ledger found in the last 7 days[/dim]")
            return

        try:
            df = pd.read_csv(ledger_file)
            if df.empty:
                console.print(f"[dim]📂 Previous ledger ({ledger_file.name}) is empty[/dim]")
                return

            # Find earliest BUY entries per ticker (original entry, not recovery)
            buys = df[df["action"] == "BUY"].copy()
            if buys.empty:
                console.print(f"[dim]📂 No BUY entries in previous ledger[/dim]")
                return

            sells = df[df["action"] == "SELL"].groupby("ticker").agg({"qty": "sum"})

            now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            recovered_count = 0

            # Process each ticker: restore original entry info, not recovery-write
            for ticker, buy_group in buys.groupby("ticker"):
                buy_qty = buy_group["qty"].sum()
                sell_qty = sells.loc[ticker, "qty"] if ticker in sells.index else 0
                net_qty = int(buy_qty - sell_qty)

                if net_qty <= 0:
                    continue

                # Pick the earliest BUY row as the canonical entry
                first_buy = buy_group.sort_values("timestamp").iloc[0]
                avg_price = float(first_buy.get("entry_price", first_buy["price"]))
                original_ts = str(first_buy.get("timestamp", ""))
                original_strategy = str(first_buy.get("strategy", self.strat_name))

                # Restore in-memory position
                self.positions[str(ticker)] = {
                    "stage": "HOLD",
                    "entry_price": avg_price,
                    "qty": net_qty,
                    "entry_ts": original_ts,
                    "peak_price": avg_price,
                }
                recovered_count += 1
                
                # Calculate hard stop for logging
                sl_pct = self.cfg.get("stocks", {}).get("stop_loss_pct", 0.02)
                hard_stop = avg_price * (1 - sl_pct)
                console.print(f"[green]♻️ Recovered position: {ticker} qty={net_qty} @ {avg_price:.2f} (original: {original_ts})[/green]")
                console.print(f"[dim]🛡️ Risk Level: {ticker} Cost={avg_price:.2f}, HardStop={hard_stop:.2f} (-{sl_pct*100:.1f}%)[/dim]")

                # Write to position_state.json only (NOT to ledger)
                merge_recovery(
                    today_state,
                    ticker=ticker,
                    original_entry_ts=original_ts,
                    strategy=original_strategy,
                    mode=self.mode_tag,
                    avg_cost=avg_price,
                    qty=net_qty,
                    realized_pnl=0.0,
                    recovered_from=last_ledger_date,
                    recovery_ts=now_ts,
                )

            if recovered_count > 0:
                save_position_state(state_file, today_state)
                total_value = sum(p["qty"] * p["entry_price"] for p in self.positions.values())
                console.print(f"[bold green]✅ Recovered {recovered_count} positions → position_state.json (total value: ${total_value:,.0f})[/bold green]")
            else:
                console.print(f"[dim]📂 No open positions from previous ledger[/dim]")

        except Exception as e:
            console.print(f"[yellow]⚠️ Position recovery failed: {e}[/yellow]")

    def clean_unfilled_orders(self):
        """撤銷超過 5 分鐘未成交的掛單 (only in LIVE mode)"""
        if self.dry_run: return
        if self.mode_tag != "LIVE": return  # PAPER mode: no real orders to cancel
        now = datetime.now()
        self.api.update_status()

        # 檢查 api.list_trades() 中屬於我們這個模式的單
        try:
            trades = self.api.list_trades()
        except Exception:
            return

        for trade in trades:
            if trade.contract.code in self.watchlist:
                # 如果是掛單中 (Submitted) 且超過 5 分鐘
                order_dt = trade.status.order_datetime
                # 處理不同類型的時間戳記
                if isinstance(order_dt, datetime):
                    order_time = order_dt  # Already a datetime
                elif isinstance(order_dt, (int, float)):
                    order_time = datetime.fromtimestamp(order_dt)
                else:
                    # 嘗試轉換為datetime
                    try:
                        if hasattr(order_dt, 'timestamp'):
                            order_time = datetime.fromtimestamp(order_dt.timestamp())
                        else:
                            # 如果無法處理，使用當前時間
                            order_time = now
                            console.print(f"[yellow]⚠️ 無法解析訂單時間: {order_dt}, 使用當前時間[/yellow]")
                    except Exception as e:
                        order_time = now
                        console.print(f"[yellow]⚠️ 訂單時間解析錯誤: {e}, 使用當前時間[/yellow]")
                if trade.status.status == sj.constant.Status.Submitted and (now - order_time).total_seconds() > 300:
                    console.print(f"[yellow]⏳ Order Timeout: Cancelling {trade.contract.code}...[/yellow]")
                    self.api.cancel_order(trade)

    def cancel_all_pending_orders(self):
        """BUG FIX 2026-04-13: Cancel ALL pending orders immediately.

        Called at:
        - 13:25 (end-of-day, before market close at 13:30)
        - When switching LIVE → PAPER mode
        Prevents odd-lot / ROD orders from filling after market hours (14:30).

        Only runs in LIVE mode — PAPER mode has no real orders to cancel.
        """
        if self.dry_run:
            console.print("[yellow]🚨 [DRY-RUN] Would cancel all pending orders[/yellow]")
            return
        if self.mode_tag != "LIVE": return  # PAPER mode: no real orders

        self.api.update_status()
        try:
            trades = self.api.list_trades()
        except Exception:
            return

        cancelled = 0
        for trade in trades:
            if trade.contract.code in self.watchlist:
                if trade.status.status == sj.constant.Status.Submitted:
                    console.print(f"[yellow]🚨 EOD Cancel: {trade.contract.code} (status={trade.status.status})[/yellow]")
                    try:
                        self.api.cancel_order(trade)
                        cancelled += 1
                    except Exception as e:
                        console.print(f"[red]❌ Failed to cancel {trade.contract.code}: {e}[/red]")

        if cancelled > 0:
            console.print(f"[bold red]🛑 Cancelled {cancelled} pending order(s) — preventing post-market fill[/bold red]")
        
        # 保存更新後的訂單狀態
        self._save_orders_file()

    def _check_bear_defense(self):
        """大盤空頭防禦：EMA 濾網 + 單日虧損上限 + 連虧暫停"""
        if not self.bear_defense:
            return False  # not blocked
        # 單日虧損上限
        if self.daily_pnl <= -self.max_daily_loss:
            console.print(f"[red]🛡️ Bear Defense: 單日虧損 {self.daily_pnl:+,.0f} 超過上限 -{self.max_daily_loss}, 停止開倉[/red]")
            return True
        # 連虧暫停
        if self.consecutive_losses >= self.max_consecutive_losses:
            console.print(f"[red]🛡️ Bear Defense: 連虧 {self.consecutive_losses} 次, 暫停開倉[/red]")
            return True
        # 持倉上限
        max_pos = self.bear_max_positions if self.is_bear_market else self.normal_max_positions
        if len(self.positions) >= max_pos:
            return True
        return False

    def _update_market_regime(self):
        """用加權指數 EMA 判斷大盤多空"""
        if not self.bear_defense:
            return
        try:
            taiex = self.api.Contracts.Indexs.TSE["001"]
            start = (datetime.now() - pd.Timedelta(days=90)).strftime("%Y-%m-%d")
            kbars = self.api.kbars(taiex, start=start)
            df = pd.DataFrame({**kbars})
            if len(df) < self.market_ema_length:
                return
            df["ema"] = df["Close"].ewm(span=self.market_ema_length, adjust=False).mean()
            self.is_bear_market = df["Close"].iloc[-1] < df["ema"].iloc[-1]
            tag = "🐻 空頭" if self.is_bear_market else "🐂 多頭"
            console.print(f"[dim]📊 大盤 regime: {tag} (Close={df['Close'].iloc[-1]:.0f}, EMA{self.market_ema_length}={df['ema'].iloc[-1]:.0f})[/dim]")
        except Exception as e:
            console.print(f"[yellow]⚠️ Market regime check failed: {e}[/yellow]")

    def _filter_watchlist_by_strength(self):
        """第二招：只留成交量大 + 開盤強勢的標的"""
        if self.api is None:
            return self.watchlist
        scored = []
        for ticker in self.watchlist:
            try:
                contract = self.api.Contracts.Stocks[ticker]
                snap = self.api.snapshots([contract])[0]
                if snap.total_amount > 0 and snap.close > snap.open:
                    scored.append((ticker, snap.total_amount))
            except Exception:
                pass
        # 按成交金額排序，取前半
        scored.sort(key=lambda x: -x[1])
        max_tickers = max(3, len(self.watchlist) // 2)
        result = [t for t, _ in scored[:max_tickers]]
        return result if result else self.watchlist

    def _check_date_reset(self):
        """Check if date has changed and reset daily stats."""
        current_date = datetime.now().strftime("%Y%m%d")
        if current_date != self.date_str:
            console.print(f"[bold yellow]📅 DATE_RESET: {self.date_str} -> {current_date}. Resetting daily_pnl and consecutive_losses.[/bold yellow]")
            self.date_str = current_date
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            
            # Update paths
            self.ledger_path = TRADE_LOGS / f"STOCK_{self.date_str}_{self.mode_tag}_trades.csv"
            self.orders_path = TRADE_LOGS / f"STOCK_{self.date_str}_{self.mode_tag}_orders.json"
            
            # Clear scan results for a fresh start
            self.scan_results = {}
            if hasattr(self, '_loop_state'):
                self._loop_state["active_watchlist"] = None
            
            # Re-recover positions (in case we switched days and need to roll over holdings)
            self._recover_positions_from_ledger()
            self._save_orders_file()
            self._save_position_state_to_disk()

    def _save_position_state_to_disk(self):
        """Save current in-memory positions to position_state.json for dashboard and recovery."""
        state_file = position_state_path(TRADE_LOGS, self.date_str, self.mode_tag)
        current_state = {}
        for ticker, pos in self.positions.items():
            current_state[ticker] = {
                "entry_ts": pos.get("entry_ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                "strategy": self.strat_name,
                "mode": self.mode_tag,
                "avg_cost": pos.get("entry_price", 0.0),
                "qty": pos.get("qty", 0),
                "realized_pnl": 0.0, # Handled per trade in ledger
                "recovered_from": self.date_str,
                "last_recovered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        save_position_state(state_file, current_state)
        console.print(f"[dim]💾 Saved position state for {len(current_state)} tickers to {state_file.name}[/dim]")

    def setup(self):
        """Initialize stock monitor - compatible with main.py interface.
        
        V-Model fix: StockMonitor was missing setup() method that main.py calls,
        causing immediate crash on startup (AttributeError).
        """
        # [GSD Fix] Ensure paths are correct for current day
        self._check_date_reset()
        
        if self.dry_run:
            console.print("[yellow][StockMonitor] dry-run: skipping API setup[/yellow]")
            return True
        if self.api is None:
            console.print("[yellow][StockMonitor] no API provided, skipping setup[/yellow]")
            return True
            
        # Initial recovery and save
        self._recover_positions_from_ledger()
        self._save_position_state_to_disk()
        
        return True

    def _reload_live_flag(self):
        """CRITICAL: Re-read the config file from disk to ensure live_trading hasn't been disabled."""
        try:
            with open(os.path.join(ROOT, "config", "stocks.yaml"), "r", encoding="utf-8") as f:
                current_cfg = yaml.safe_load(f)
                disk_live = current_cfg.get("live_trading", False)
                if self.live_trading and not disk_live:
                    # LIVE → PAPER: block trading, cancel orders, trigger restart
                    console.print("[bold red]🚨 SAFETY OVERRIDE: Config changed to PAPER on disk! Blocking LIVE trade.[/bold red]")
                    self.live_trading = False
                    self.mode_tag = "PAPER"
                    # Update paths
                    self.ledger_path = TRADE_LOGS / f"STOCK_{self.date_str}_{self.mode_tag}_trades.csv"
                    self.orders_path = TRADE_LOGS / f"STOCK_{self.date_str}_{self.mode_tag}_orders.json"
                    
                    console.print("[bold red]🛑 Cancelling ALL pending orders to prevent post-market execution[/bold red]")
                    self.cancel_all_pending_orders()
                    Path(ROOT / ".restart").touch()
                elif not self.live_trading and disk_live:
                    # PAPER → LIVE: promote to live mode, cancel any stale paper positions
                    console.print("[bold yellow]⚠️ Config changed to LIVE on disk! Switching to LIVE mode.[/bold yellow]")
                    self.live_trading = True
                    self.mode_tag = "LIVE"
                    # Update paths
                    self.ledger_path = TRADE_LOGS / f"STOCK_{self.date_str}_{self.mode_tag}_trades.csv"
                    self.orders_path = TRADE_LOGS / f"STOCK_{self.date_str}_{self.mode_tag}_orders.json"
                    
                    console.print("[bold yellow]🛑 Cancelling ALL paper orders before going LIVE[/bold yellow]")
                    self.cancel_all_pending_orders()
                    Path(ROOT / ".restart").touch()
                return disk_live
        except Exception as e:
            console.print(f"[yellow]⚠️ Could not re-verify live flag: {e}[/yellow]")
            return self.live_trading

    def run_iteration(self):
        """Run a single iteration of the scanning and execution logic."""
        # [GSD Fix] Daily reset for long-running process
        self._check_date_reset()
        
        if not hasattr(self, '_loop_state'):
            from strategies.stocks.entry_strategies import STOCK_STRATEGIES
            from strategies.options.options_engine.engine.indicators import calculate_stock_squeeze
            from strategies.stocks.multi_timeframe import analyze_market_condition, should_trade_based_on_tf
            
            self._loop_state = {
                "last_regime_check": 0,
                "last_daily_scan": time.time(),
                "active_watchlist": None,
                "calculate_stock_squeeze": calculate_stock_squeeze,
                "STOCK_STRATEGIES": STOCK_STRATEGIES,
                "analyze_market_condition": analyze_market_condition,
                "should_trade_based_on_tf": should_trade_based_on_tf
            }
            self._run_daily_scan_try()  # non-blocking attempt
            console.print(f"[bold green]🍎 StockMonitor [{self.mode_tag}] Started | Strategy: {self.strat_name}[/bold green]")

        now = datetime.now()
        console.print(f"[dim]🕒 Run iteration at {now.strftime('%H:%M:%S')}[/dim]")
        # BUG FIX 2026-04-13: Cancel pending orders when trading window closes
        if now.hour < 9 or (now.hour == 13 and now.minute > 30) or now.hour >= 14:
            console.print(f"[dim]⏸️ Outside trading hours, skipping iteration[/dim]")
            # Only cancel once per session end (track with flag)
            if not getattr(self, '_eod_cancelled_today', False):
                self.cancel_all_pending_orders()
                self._eod_cancelled_today = True
            return
        else:
            # Reset flag when a new trading day starts
            self._eod_cancelled_today = False

        # Periodically refresh regime and scan
        if time.time() - self._loop_state["last_regime_check"] > 1800:
            self._update_market_regime()
            self._loop_state["last_regime_check"] = time.time()
        
        if time.time() - self._loop_state["last_daily_scan"] > 14400:
            self._run_daily_scan_try()
            self._loop_state["last_daily_scan"] = time.time()

        if self._loop_state["active_watchlist"] is None and now.hour == 9 and now.minute >= 5:
            self._loop_state["active_watchlist"] = self._filter_watchlist_by_strength()
            console.print(f"[cyan]📋 Active watchlist: {len(self._loop_state['active_watchlist'])} / {len(self.watchlist)} tickers[/cyan]")

        self.clean_unfilled_orders()
        scan_list = self._loop_state["active_watchlist"] if self._loop_state["active_watchlist"] else self.watchlist
        # Also scan open positions (may include overnight holdings not in watchlist)
        position_tickers = [t for t in self.positions.keys() if t not in scan_list]
        if position_tickers:
            scan_list = list(scan_list) + position_tickers
            console.print(f"[dim]📋 Added {len(position_tickers)} open positions to scan: {position_tickers}[/dim]")
        console.print(f"[dim]🔍 Scanning {len(scan_list)} tickers: {scan_list[:5]}{'...' if len(scan_list) > 5 else ''}[/dim]")

        for ticker in scan_list:
            try:
                contract = self.api.Contracts.Stocks[ticker]
                console.print(f"[dim]📋 Contract for {ticker}: {contract}[/dim]")
                notice = getattr(contract, 'notice', None)
                if notice is not None and str(notice) != "Normal": 
                    console.print(f"[yellow]⚠️ Skipping {ticker} due to notice: {notice}[/yellow]")
                    continue
                
                # 1. Indicator Analysis (回溯 4 天以確保有足夠的歷史資料算指標，特別是週一)
                start_date = (now - pd.Timedelta(days=4)).strftime("%Y-%m-%d")
                end_date = now.strftime("%Y-%m-%d")
                console.print(f"[dim]📊 Fetching data for {ticker} from {start_date} to {end_date}[/dim]")
                
                df = None
                data_source = "unknown"
                
                # 嘗試 1: 從 API 獲獲取 kbars
                import socket as _sk1
                _orig_to1 = _sk1.getdefaulttimeout()
                _sk1.setdefaulttimeout(15)
                try:
                    kbars = self.api.kbars(contract, start=start_date, end=end_date)
                    kbars_dict = {**kbars}
                    
                    if kbars_dict and any(len(v) > 0 for v in kbars_dict.values() if hasattr(v, '__len__')):
                        df = pd.DataFrame(kbars_dict)
                        data_source = "api_kbars"
                        console.print(f"[green]✅ Got {len(df)} rows from API kbars for {ticker}[/green]")
                    else:
                        console.print(f"[yellow]⚠️ API returned empty data for {ticker}[/yellow]")
                except Exception as e:
                    console.print(f"[dim]📋 API kbars failed: {e}[/dim]")
                finally:
                    _sk1.setdefaulttimeout(_orig_to1)
                
                # 嘗試 2: 如果 API 數據為空，從本地緩存讀取
                if df is None or df.empty:
                    console.print(f"[dim]📊 Trying local cache for {ticker}[/dim]")
                    try:
                        # 查找最近的緩存數據文件
                        cache_pattern = f"STOCK_{ticker}_*_indicators.csv"
                        cache_files = list(MKT_LOGS.glob(cache_pattern))
                        
                        if cache_files:
                            # 按修改時間排序，獲取最新的文件
                            cache_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                            latest_cache = cache_files[0]
                            
                            # 讀取緩存數據
                            cache_df = pd.read_csv(latest_cache)
                            if not cache_df.empty:
                                # 使用最後一行作為參考數據（僅用於顯示，不用於交易決策）
                                last_row = cache_df.iloc[-1].to_dict()
                                current_time = pd.Timestamp.now()
                                
                                # 創建一個 DataFrame，但標記為緩存數據
                                df = pd.DataFrame({
                                    'ts': [current_time],
                                    'Open': [last_row.get('open', last_row.get('Close', 0))],
                                    'High': [last_row.get('high', last_row.get('Close', 0))],
                                    'Low': [last_row.get('low', last_row.get('Close', 0))],
                                    'Close': [last_row.get('close', last_row.get('Close', 0))],
                                    'Volume': [last_row.get('volume', 0)],
                                    'Amount': [last_row.get('amount', 0)],
                                    '_data_source': ['cache']  # 標記數據來源
                                })
                                data_source = "cache"
                                console.print(f"[yellow]⚠️ Using cached reference data for {ticker} from {latest_cache.name} (Close: {df['Close'].iloc[0]}) - NOT FOR TRADING[/yellow]")
                    except Exception as e:
                        console.print(f"[dim]📋 Cache read failed: {e}[/dim]")
                
                # 如果沒有可用的數據，跳過此股票
                if df is None or df.empty:
                    console.print(f"[red]❌ No data available for {ticker}, skipping[/red]")
                    # 但仍然創建一個空的數據存儲，以便dashboard知道此股票存在但無數據
                    if ticker not in self.data_storages:
                        self.data_storages[ticker] = StockDataStorage(ticker)
                        console.print(f"[dim]📊 Created empty data storage for {ticker}[/dim]")
                    continue
                if df.empty: 
                    console.print(f"[yellow]⚠️ Empty DataFrame for {ticker}[/yellow]")
                    continue
                
                df["ts"] = pd.to_datetime(df["ts"])
                df = df.set_index("ts")
                
                # GSD Fix: Resample to 5 minutes if it's from API (which is 1-min by default)
                # This ensures EMA20/60 reflect a more meaningful timeframe (1.5h/5h)
                if data_source == "api_kbars":
                    # Backup original names if they are already capitalized
                    resample_cols = {
                        'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 
                        'Volume': 'sum', 'Amount': 'sum'
                    }
                    # Handle lowercase if needed
                    if 'open' in df.columns:
                        resample_cols = {k.lower(): v for k, v in resample_cols.items()}
                    
                    df = df.resample('5min').agg(resample_cols).dropna()
                    console.print(f"[dim]📊 Resampled {ticker} to 5-minute bars ({len(df)} rows)[/dim]")

                df.columns = [c.capitalize() if c.lower() in ["open", "high", "low", "close", "volume"] else c for c in df.columns]
                
                # 調試：檢查傳遞給 calculate_stock_squeeze 的 DataFrame
                console.print(f"[dim]📊 DEBUG: Before calculate_stock_squeeze for {ticker}[/dim]")
                
                df = self._loop_state["calculate_stock_squeeze"](df)
                
                # Indicator logic
                df['ma20'] = df['Close'].rolling(20).mean()
                df['ma60'] = df['Close'].rolling(60).mean()
                vol_avg = df['Volume'].rolling(20).mean()
                is_it_buy = (df['Volume'] > vol_avg * 1.5) & (df['Close'] > df['Open']) & (df['Close'] > df['ma20'])
                df['it_buy_rolling_count'] = is_it_buy.rolling(5).sum().fillna(0)
                
                scan_info = self.scan_results.get(ticker, {"pattern": "NONE", "pivot": 0.0})
                
                # Multi-TF analysis
                tf_analysis = self._loop_state["analyze_market_condition"](df)
                
                # 保存指標數據供 Dashboard 顯示
                if not df.empty:
                    last_row = df.iloc[-1].to_dict()
                    # 添加股票名稱
                    last_row["name"] = contract.name if hasattr(contract, "name") else ticker
                    
                    # GSD: Include Multi-TF results for more robust Dashboard trend label
                    last_row["primary_trend"] = tf_analysis.get("market_state", {}).get("primary_trend", "UNKNOWN")
                    last_row["market_regime"] = tf_analysis.get("market_state", {}).get("market_regime", "UNKNOWN")
                    
                    # 標記數據來源
                    if data_source == "cache":
                        last_row["_data_source"] = "cache"
                        last_row["_trading_disabled"] = True
                        console.print(f"[yellow]⚠️ Indicators for {ticker} marked as CACHE DATA - TRADING DISABLED[/yellow]")
                    elif data_source == "api_kbars":
                        last_row["_data_source"] = "api"
                        last_row["_trading_disabled"] = False
                    else:
                        last_row["_data_source"] = "unknown"
                        last_row["_trading_disabled"] = True
                    
                    # 初始化數據存儲器
                    if ticker not in self.data_storages:
                        self.data_storages[ticker] = StockDataStorage(ticker)
                        console.print(f"[dim]📊 Created data storage for {ticker}[/dim]")
                    
                    # 保存最新指標
                    try:
                        self.data_storages[ticker].save_indicators(df.index[-1], last_row)
                        console.print(f"[green]✅ Saved indicators for {ticker} (Trend: {last_row['primary_trend']})[/green]")
                    except Exception as e:
                        console.print(f"[yellow]⚠️ Failed to save indicators for {ticker}: {e}[/yellow]")

                # 檢查數據來源，如果是緩存數據則跳過交易
                if data_source == "cache":
                    console.print(f"[yellow]⚠️ Skipping trade for {ticker} - using cached data (trading disabled)[/yellow]")
                    continue
                
                should_trade, tf_details = self._loop_state["should_trade_based_on_tf"](df)
                
                if not should_trade:
                    continue
                    
                state = {
                    "last_5m": df.iloc[-1], "df_5m": df,
                    "ticker": ticker,
                    "scout_stage": self.positions.get(ticker, {}).get("stage", "IDLE"),
                    "scout_entry_price": self.positions.get(ticker, {}).get("entry_price", 0.0),
                    "hold_bars": self.positions.get(ticker, {}).get("hold_bars", 0),
                    "market_trend": "BEAR" if self.is_bear_market else "BULL",
                    "pattern": scan_info["pattern"],
                    "pivot": scan_info["pivot"],
                }
                
                res = self._loop_state["STOCK_STRATEGIES"][self.strat_name]["func"](state, self.cfg)
                
                # 3. Execution & Risk with Decision Intelligence
                snapshot = self.api.snapshots([contract])[0]
                last_bar = df.iloc[-1].to_dict()
                regime = tf_analysis.get("regime", "NORMAL")
                last_bar["regime"] = regime
                
                # --- A. Risk Check (Consolidated Exit Engine) ---
                if ticker in self.positions:
                    self.check_risk(ticker, snapshot.close, context={
                        "regime": regime,
                        "last_bar": last_bar,
                        "now": now
                    })

                # --- B. Entry Filter (Market Gate + Edge Model) ---
                if res and res["action"] == "BUY" and not self._check_bear_defense():
                    from strategies.stocks.market_gate import get_gate, strategy_allowed
                    gate = get_gate()
                    if gate == "BLOCK_LONG":
                        console.print(f"[dim]🚫 Market gate BLOCK_LONG — skip {ticker}[/dim]")
                        continue
                    if not strategy_allowed(self.strat_name):
                        console.print(f"[dim]🚫 Strategy {self.strat_name} not allowed in current regime — skip {ticker}[/dim]")
                        continue
                    
                    from core.edge_model import edge_model
                    context = {
                        "regime": regime,
                        "momentum": last_bar.get("momentum", 0),
                        "volatility": last_bar.get("atr", 0),
                        "vwap_dist": abs(snapshot.close - last_bar.get("vwap", snapshot.close))
                    }
                    edge_res = edge_model.evaluate(abs(last_bar.get("score", 50)), context, self.strat_name)
                    
                    if edge_res["has_edge"]:
                        # [Fix] Cooldown: same ticker max 1 entry per 10 min
                        _last_entry = getattr(self, f'_last_entry_{ticker}', 0)
                        if time.time() - _last_entry < 600:
                            console.print(f"[dim]⏳ Cooldown active for {ticker} — skip[/dim]")
                            continue
                        self._reload_live_flag() 
                        self.execute_trade(ticker, "BUY", snapshot.close, res.get("qty_mode", "SCOUT"), f"{res.get('reason', 'SIGNAL')} (Edge={edge_res['edge_score']:.2f})")
                        setattr(self, f'_last_entry_{ticker}', time.time())
                    else:
                        if random.random() < 0.1: # Reduce log noise
                            console.print(f"[dim]🛡️ Entry blocked: {ticker} {edge_res['reason']}[/dim]")
                        
            except Exception as e:
                console.print(f"[red]Error {ticker}: {e}[/red]")

    def run(self):
        """Legacy run method for backward compatibility."""
        self.running = True
        # GSD Fix: Ensure recovery is called and then save to JSON for dashboard
        self._recover_positions_from_ledger()
        self._save_orders_file()
        
        while self.running:
            self.run_iteration()
            time.sleep(60)

    def check_risk(self, ticker, curr_price, context=None):
        """Consolidated Risk Engine v1.2
        Handles: Adaptive Exit, Trailing Stop, Max Hold Time, EOD Exit, and Hard Stop.
        """
        if ticker not in self.positions: return
        pos = self.positions[ticker]
        now = datetime.now()
        
        # 1. Update State
        pos["peak_price"] = max(pos.get("peak_price", pos["entry_price"]), curr_price)
        pos["hold_bars"] = pos.get("hold_bars", 0) + 1
        peak = pos["peak_price"]
        
        # 2. Extract context for adaptive logic
        ctx = context or {}
        last_bar = ctx.get("last_bar", {})
        regime = ctx.get("regime", "NORMAL")
        
        # ── Layer 1: Adaptive Exit Engine (Edge-based) ──
        from core.exit_engine import should_exit
        trade_state = {
            "entry_price": pos["entry_price"],
            "side": "LONG",
            "peak_price": peak,
            "position_age_bars": pos["hold_bars"]
        }
        # Context for edge calculation
        exit_context = {
            "regime": regime,
            "momentum": last_bar.get("momentum", 0),
            "volatility": last_bar.get("atr", 0),
            "volatility_norm": min(1.0, last_bar.get("atr", 0) / (curr_price * 0.05)),
            "vwap_dist": abs(curr_price - last_bar.get("vwap", curr_price)),
            "signal_score": abs(last_bar.get("score", 50))
        }
        # Calculate time to close
        close_time = now.replace(hour=13, minute=30, second=0)
        time_to_close = max(0, (close_time - now).total_seconds() / 60)
        market_state = {
            "price": curr_price,
            "atr": last_bar.get("atr", 0),
            "time_to_close_mins": time_to_close
        }
        
        exit_triggered, exit_reason = should_exit(trade_state, exit_context, market_state)
        if exit_triggered:
            self.execute_trade(ticker, "SELL", curr_price, "ALL", exit_reason)
            return

        # ── Layer 1.5: Configurable Trailing Stop ──
        stk_cfg = self.cfg.get("stocks", {})
        activation_pct = stk_cfg.get("trailing_activation_pct", 0.012)
        drawdown_pct = stk_cfg.get("trailing_drawdown_pct", 0.01)
        
        peak_profit_pct = (peak - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] > 0 else 0
        drawdown_from_peak = (peak - curr_price) / peak if peak > 0 else 0
        
        if peak_profit_pct >= activation_pct and drawdown_from_peak >= drawdown_pct:
            self.execute_trade(ticker, "SELL", curr_price, "ALL", f"TRAIL_PULLBACK (peak={peak:.1f}, dd={drawdown_from_peak:.1%})")
            return

        # ── Layer 1.6: Max Holding Time ──
        max_hold_seconds = stk_cfg.get("max_hold_seconds", 1800)
        if pos.get("hold_bars", 0) >= max_hold_seconds:
            self.execute_trade(ticker, "SELL", curr_price, "ALL", f"MAX_HOLD ({pos['hold_bars']}s/{max_hold_seconds}s)")
            return

        # ── Layer 2: EOD Smart Exit (Final Safety Window) ──
        if now.hour == 13 and now.minute >= 20:
            pnl_pct = (curr_price - pos["entry_price"]) / pos["entry_price"]
            if pnl_pct <= 0:
                self.execute_trade(ticker, "SELL", curr_price, "ALL", "TIME_EXIT_LOSER")
            elif now.minute >= 25:
                self.cancel_all_pending_orders()
                self.execute_trade(ticker, "SELL", curr_price, "ALL", "TIME_EXIT_FINAL")
            if ticker in self.positions:
                return
            
        # ── Layer 3: Hard Stop Loss (Last Defense) ──
        sl_pct = stk_cfg.get("stop_loss_pct", 0.02)
        hard_stop_price = pos["entry_price"] * (1 - sl_pct)
        if curr_price <= hard_stop_price:
            self.execute_trade(ticker, "SELL", curr_price, "ALL", f"HARD_STOP_LOSS (price={curr_price:.2f} <= {hard_stop_price:.2f})")
            return

    def execute_trade(self, ticker, action, price, qty_mode, reason):
        if action == "BUY":
            # GSD Fix: Prevent duplicate entry for same ticker
            if ticker in self.positions and qty_mode == "SCOUT":
                return

            current_exposure = self.get_current_exposure()
            remaining = self.total_budget - current_exposure
            if remaining <= 5000: return

            if qty_mode == "SCOUT":
                scout_cap = min(remaining, max(5000, self.capital_per_trade * 0.1))
                qty = int(scout_cap // (price * 1.002))
            else:
                # P0 fix: guard against missing position (race condition with check_risk)
                if ticker not in self.positions:
                    return
                pos_qty = self.positions[ticker]["qty"]
                pos_cost = pos_qty * self.positions[ticker]["entry_price"]
                total_target = min(self.total_budget - (current_exposure - pos_cost), self.capital_per_trade)
                qty = int(total_target // (price * 1.002)) - pos_qty
            
            if qty <= 0: return

            # --- LIVE: 下單後等確認，才更新 position ---
            if self.mode_tag == "LIVE":
                contract = self.api.Contracts.Stocks[ticker]
                # POLICY 2026-04-13: Use IntradayOdd — trades during market hours (09:00-13:30),
                # auto-expires at 13:30 if unfilled. NO post-market 14:30 fill risk.
                order = self.api.Order(
                    price=price, quantity=qty, action=sj.constant.Action.Buy,
                    price_type=sj.constant.StockPriceType.LMT,
                    order_type=sj.constant.OrderType.ROD,
                    order_lot=sj.constant.StockOrderLot.IntradayOdd
                )
                trade = self.api.place_order(contract, order)
                # 等待委託回報確認 (最多 10 秒)
                self.api.update_status()
                if trade.status.status != sj.constant.Status.Submitted and trade.status.status != sj.constant.Status.Filled:
                    console.print(f"[red]❌ BUY {ticker} order rejected: {trade.status.status}[/red]")
                    return

            self.positions[ticker] = {"stage": qty_mode, "entry_price": price, "qty": self.positions.get(ticker, {}).get("qty", 0) + qty, "entry_ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            # [SCOUT Lifecycle] MAIN = promoted from SCOUT; lifecycle marks position maturity
            if qty_mode == "MAIN":
                self.positions[ticker]["lifecycle"] = "PROMOTED"
            console.print(f"[cyan]🚀 [{self.mode_tag}] BUY {ticker} | Qty: {qty} | Reason: {reason}[/cyan]")
            self._log_trade(ticker, "BUY", price, qty, reason, 0.0, price)
            self._save_position_state_to_disk()

        elif action == "SELL":
            if ticker not in self.positions: return
            pos = self.positions[ticker]
            
            # GSD Fix: 零股禁止當沖檢查
            # 台灣股市規則：零股（<1000股）禁止當天賣出
            qty = pos.get("qty", 0)
            if qty < 1000:
                # 檢查是否為當天買入的零股
                is_today_buy = False
                try:
                    # 從交易紀錄中查找這個ticker的最近買入時間
                    if self.ledger_path.exists():
                        trades_df = pd.read_csv(self.ledger_path)
                        # 將ticker轉換為整數進行比較（交易紀錄中的ticker是整數）
                        ticker_int = int(ticker)
                        ticker_trades = trades_df[trades_df["ticker"] == ticker_int]
                        buy_trades = ticker_trades[ticker_trades["action"] == "BUY"]
                        if not buy_trades.empty:
                            # 獲取最近一筆買入交易的時間
                            latest_buy = buy_trades.iloc[-1]
                            buy_timestamp = pd.to_datetime(latest_buy["timestamp"])
                            buy_date = buy_timestamp.date()
                            today = datetime.now().date()
                            is_today_buy = (buy_date == today)
                except Exception as e:
                    console.print(f"[yellow]⚠️ Failed to check odd-lot day-trade: {e}[/yellow]")
                
                # 如果零股是今天買入的，則禁止賣出（當沖）
                if is_today_buy:
                    console.print(f"[yellow]⚠️ 零股禁止當沖: {ticker} ({qty}股) 今天買入，禁止當天賣出[/yellow]")
                    return
                else:
                    # 零股但不是今天買入的（可能是隔夜持倉），允許賣出
                    console.print(f"[cyan]ℹ️ 零股賣出允許: {ticker} ({qty}股) 非今天買入[/cyan]")
            
            # --- LIVE: 下單後等確認，才更新 position ---
            if self.mode_tag == "LIVE":
                contract = self.api.Contracts.Stocks[ticker]
                # POLICY 2026-04-13: Use IntradayOdd — auto-expires at 13:30 if unfilled.
                order = self.api.Order(
                    price=price, quantity=pos["qty"], action=sj.constant.Action.Sell,
                    price_type=sj.constant.StockPriceType.LMT,
                    order_type=sj.constant.OrderType.ROD,
                    order_lot=sj.constant.StockOrderLot.IntradayOdd
                )
                trade = self.api.place_order(contract, order)
                self.api.update_status()
                if trade.status.status != sj.constant.Status.Submitted and trade.status.status != sj.constant.Status.Filled:
                    console.print(f"[red]❌ SELL {ticker} order rejected: {trade.status.status}[/red]")
                    return

            pnl = (price - pos["entry_price"]) * pos["qty"]
            qty = pos["qty"]
            del self.positions[ticker]
            # 更新空頭防禦計數
            self.daily_pnl += pnl
            if pnl < 0:
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0
            # 計算手續費
            buy_amt = pos["entry_price"] * qty
            sell_amt = price * qty
            buy_fee = max(20.0, buy_amt * 0.0005)
            sell_fee = max(20.0, sell_amt * 0.0005)
            tax = sell_amt * 0.003
            fees = buy_fee + sell_fee + tax
            net_pnl = pnl - fees
            console.print(f"[green]🏁 [{self.mode_tag}] SELL {ticker} | PnL: {net_pnl:+.0f} (gross {pnl:+.0f} - fees {fees:.0f}) | DayPnL: {self.daily_pnl:+.0f}[/green]")
            self._log_trade(ticker, "SELL", price, qty, reason, pnl, pos["entry_price"], fees, net_pnl)
            self._save_position_state_to_disk()

    def _log_trade(self, ticker, action, price, qty, reason, pnl, entry_price=0.0, fees=0.0, net_pnl=0.0):
        row = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ticker": ticker,
            "strategy": self.strat_name,
            "mode": self.mode_tag,
            "action": action,
            "price": round(price, 2),
            "entry_price": round(entry_price, 2),
            "qty": qty,
            "reason": reason,
            "pnl_gross": round(pnl, 0),
            "fees": round(fees, 0),
            "pnl_cash": round(net_pnl if action == "SELL" else 0, 0),
        }
        trade_df = pd.DataFrame([row])
        trade_df.to_csv(self.ledger_path, mode='a', header=not self.ledger_path.exists(), index=False)
        
        # 保存訂單狀態
        self._save_orders_file()

    def _save_orders_file(self):
        """Export all stock orders to JSON for dashboard consumption."""
        try:
            # 獲取當前市場價格用於計算未實現損益
            current_prices = {}
            price_tickers = list(self.watchlist)
            for t in self.positions:
                if t not in price_tickers:
                    price_tickers.append(t)
            for ticker in price_tickers:
                try:
                    contract = self.api.Contracts.Stocks[ticker]
                    snapshot = self.api.snapshots([contract])[0]
                    current_prices[ticker] = snapshot.close
                except Exception:
                    current_prices[ticker] = 0
            
            # 從API獲取訂單狀態
            orders_data = []
            if self.mode_tag == "LIVE":
                try:
                    trades = self.api.list_trades()
                    for trade in trades:
                        if trade.contract.code in self.watchlist:
                            # 映射狀態到dashboard期望的格式
                            status_map = {
                                "Submitted": "OPEN",
                                "Filled": "FILLED", 
                                "PartialFilled": "PARTIAL",
                                "Cancelled": "CANCELLED",
                                "Rejected": "REJECTED"
                            }
                            status = trade.status.status.name
                            status_str = status_map.get(status, status)
                            
                        order_dict = {
                            "order_id": trade.status.id,
                            "ticker": trade.contract.code,
                            "side": "BUY" if trade.order.action.name == "Buy" else "SELL",
                            "order_type": trade.order.price_type.name,  # LMT, MKT, etc.
                            "qty": trade.order.quantity,
                            "filled_qty": trade.status.deal_quantity,
                            "price": trade.order.price,
                            "filled_price": trade.status.avg_price if trade.status.avg_price > 0 else trade.order.price,
                            "status": status_str,
                            "timestamp": trade.status.order_datetime.strftime("%Y-%m-%d %H:%M:%S") if trade.status.order_datetime else datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "strategy": self.strat_name,
                            "mode": self.mode_tag
                        }
                        orders_data.append(order_dict)
                except Exception as e:
                    console.print(f"[yellow]⚠️ Failed to fetch live orders: {e}[/yellow]")

                if not orders_data and self.ledger_path.exists():
                    try:
                        trades_df = pd.read_csv(self.ledger_path)
                        orders_data = build_stock_orders_from_trades(
                            trades_df,
                            default_strategy=self.strat_name,
                            mode=self.mode_tag,
                        )
                    except Exception as e:
                        console.print(f"[yellow]⚠️ Failed to recover live orders from ledger: {e}[/yellow]")
            
            # 對於PAPER模式，從交易紀錄和pending_orders構建模擬訂單
            else:
                try:
                    if self.ledger_path.exists():
                        trades_df = pd.read_csv(self.ledger_path)
                        orders_data = build_stock_orders_from_trades(
                            trades_df,
                            default_strategy=self.strat_name,
                            mode=self.mode_tag,
                        )
                except Exception as e:
                    console.print(f"[yellow]⚠️ Failed to read trades for orders: {e}[/yellow]")
                    # 回退到從positions創建（為了向後兼容）
                    for ticker, pos in self.positions.items():
                        if pos.get("qty", 0) > 0:
                            order_dict = {
                                "order_id": f"PAPER-{ticker}-{int(time.time())}",
                                "ticker": ticker,
                                "side": "BUY",
                                "order_type": "LMT",
                                "qty": pos["qty"],
                                "filled_qty": pos["qty"],
                                "price": pos["entry_price"],
                                "filled_price": pos["entry_price"],
                                "status": "FILLED",
                                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "strategy": self.strat_name,
                                "mode": self.mode_tag
                            }
                            orders_data.append(order_dict)
                
                # 從pending_orders創建待處理訂單
                for ticker, order_info in self.pending_orders.items():
                    order_dict = {
                        "order_id": order_info.get("order_id", f"PENDING-{ticker}"),
                        "ticker": ticker,
                        "side": "BUY",  # 假設都是買單
                        "order_type": "LMT",  # Paper mode pending orders are limit orders
                        "qty": 1000,  # 默認數量
                        "filled_qty": 0,
                        "price": 0,
                        "filled_price": 0,
                        "status": "OPEN",
                        "timestamp": order_info.get("time", datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
                        "strategy": self.strat_name,
                        "mode": self.mode_tag
                    }
                    orders_data.append(order_dict)
            
            # 保存到文件
            self.orders_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.orders_path, "w", encoding="utf-8") as f:
                json.dump(orders_data, f, ensure_ascii=False, indent=2)
                
            console.print(f"[dim]💾 Saved {len(orders_data)} stock orders to {self.orders_path.name}[/dim]")
            
        except Exception as e:
            console.print(f"[yellow]⚠️ Failed to save orders file: {e}[/yellow]")

    def stop(self): self.running = False
