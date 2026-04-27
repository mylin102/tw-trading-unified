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
        """GSD: Recover overnight positions from previous day's trade ledger.
        
        Reads yesterday's trades CSV, calculates net positions (BUY - SELL),
        and restores them to self.positions so the monitor doesn't double-buy.
        Also writes BUY records to today's ledger so the dashboard can display them.
        """
        # GSD Fix: Skip if already recovered (today's ledger has OVERNIGHT_RECOVERY)
        if self.ledger_path.exists():
            try:
                existing = pd.read_csv(self.ledger_path)
                if not existing.empty and existing["reason"].str.contains("OVERNIGHT_RECOVERY").any():
                    # Re-load positions from recovery records but don't write again
                    recoveries = existing[existing["reason"].str.contains("OVERNIGHT_RECOVERY")]
                    for _, row in recoveries.drop_duplicates(subset=["ticker"]).iterrows():
                        self.positions[str(row["ticker"])] = {
                            "stage": "HOLD", "entry_price": row["entry_price"], "qty": int(row["qty"]),
                        }
                    console.print(f"[dim]♻️ Already recovered {len(self.positions)} positions from today's ledger[/dim]")
                    return
            except Exception:
                pass

        # Find the most recent ledger within the last 7 days
        ledger_file = None
        for i in range(1, 8):
            check_date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
            possible_file = TRADE_LOGS / f"STOCK_{check_date}_{self.mode_tag}_trades.csv"
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

            # Calculate net positions: BUY qty - SELL qty
            # We need to consider that the previous ledger itself might have OVERNIGHT_RECOVERY entries
            # which represent carry-over from even earlier days.
            buys = df[df["action"] == "BUY"].groupby("ticker").agg({"qty": "sum", "entry_price": "mean"})
            sells = df[df["action"] == "SELL"].groupby("ticker").agg({"qty": "sum"})

            # Merge and calculate net
            recovered_count = 0
            for ticker, buy_row in buys.iterrows():
                buy_qty = buy_row["qty"]
                sell_qty = sells.loc[ticker, "qty"] if ticker in sells.index else 0
                net_qty = buy_qty - sell_qty

                if net_qty > 0:
                    avg_price = buy_row["entry_price"]
                    self.positions[str(ticker)] = {
                        "stage": "HOLD",
                        "entry_price": avg_price,
                        "qty": int(net_qty),
                    }
                    recovered_count += 1
                    console.print(f"[green]♻️ Recovered position: {ticker} qty={int(net_qty)} @ {avg_price:.2f}[/green]")

                    # GSD Fix: Also write to today's ledger so dashboard can display it
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    recovery_record = {
                        "timestamp": now,
                        "ticker": ticker,
                        "strategy": self.strat_name,
                        "mode": self.mode_tag,
                        "action": "BUY",
                        "price": avg_price,
                        "entry_price": avg_price,
                        "qty": int(net_qty),
                        "reason": f"OVERNIGHT_RECOVERY_{last_ledger_date}",
                        "pnl_gross": 0.0,
                        "fees": 0.0,
                        "pnl_cash": 0.0,
                    }
                    rec_df = pd.DataFrame([recovery_record])
                    header = not self.ledger_path.exists()
                    rec_df.to_csv(self.ledger_path, mode='a', header=header, index=False)

            if recovered_count > 0:
                total_value = sum(p["qty"] * p["entry_price"] for p in self.positions.values())
                console.print(f"[bold green]✅ Recovered {recovered_count} positions (total value: ${total_value:,.0f}) — written to today's ledger for dashboard display[/bold green]")
            else:
                console.print(f"[dim]📂 No open positions from yesterday[/dim]")

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

    def setup(self):
        """Initialize stock monitor - compatible with main.py interface.
        
        V-Model fix: StockMonitor was missing setup() method that main.py calls,
        causing immediate crash on startup (AttributeError).
        """
        if self.dry_run:
            console.print("[yellow][StockMonitor] dry-run: skipping API setup[/yellow]")
            return True
        if self.api is None:
            console.print("[yellow][StockMonitor] no API provided, skipping setup[/yellow]")
            return True
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
                    console.print("[bold red]🛑 Cancelling ALL pending orders to prevent post-market execution[/bold red]")
                    self.cancel_all_pending_orders()
                    Path(ROOT / ".restart").touch()
                elif not self.live_trading and disk_live:
                    # PAPER → LIVE: promote to live mode, cancel any stale paper positions
                    console.print("[bold yellow]⚠️ Config changed to LIVE on disk! Switching to LIVE mode.[/bold yellow]")
                    self.live_trading = True
                    self.mode_tag = "LIVE"
                    console.print("[bold yellow]🛑 Cancelling ALL paper orders before going LIVE[/bold yellow]")
                    self.cancel_all_pending_orders()
                    Path(ROOT / ".restart").touch()
                return disk_live
        except Exception as e:
            console.print(f"[yellow]⚠️ Could not re-verify live flag: {e}[/yellow]")
            return self.live_trading

    def run_iteration(self):
        """Run a single iteration of the scanning and execution logic."""
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
                    "scout_stage": self.positions.get(ticker, {}).get("stage", "IDLE"),
                    "scout_entry_price": self.positions.get(ticker, {}).get("entry_price", 0.0),
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
                
                # --- A. Risk Check (Adaptive Exit Engine) ---
                if ticker in self.positions:
                    pos = self.positions[ticker]
                    # Update peak for trailing stop
                    pos["peak_price"] = max(pos.get("peak_price", pos["entry_price"]), snapshot.close)
                    # Track holding bars
                    pos["hold_bars"] = pos.get("hold_bars", 0) + 1
                    
                    from core.exit_engine import should_exit
                    trade_state = {
                        "entry_price": pos["entry_price"],
                        "side": "LONG",
                        "peak_price": pos["peak_price"],
                        "position_age_bars": 0 # TODO: Track bars held
                    }
                    # Context for edge calculation
                    context = {
                        "regime": regime,
                        "momentum": last_bar.get("momentum", 0),
                        "volatility": last_bar.get("atr", 0),
                        "volatility_norm": min(1.0, last_bar.get("atr", 0) / (snapshot.close * 0.05)),
                        "vwap_dist": abs(snapshot.close - last_bar.get("vwap", snapshot.close)),
                        "signal_score": abs(last_bar.get("score", 50))
                    }
                    
                    # Calculate time to close
                    close_time = now.replace(hour=13, minute=30, second=0)
                    time_to_close = max(0, (close_time - now).total_seconds() / 60)
                    market = {
                        "price": snapshot.close,
                        "atr": last_bar.get("atr", 0),
                        "time_to_close_mins": time_to_close
                    }
                    
                    exit_triggered, exit_reason = should_exit(trade_state, context, market)
                    if exit_triggered:
                        self.execute_trade(ticker, "SELL", snapshot.close, "ALL", exit_reason)
                    else:
                        # Fallback for Hard Stop (Circuit Breaker)
                        sl_pct = self.cfg.get("stocks", {}).get("stop_loss_pct", 0.05)
                        if snapshot.close <= pos["entry_price"] * (1 - sl_pct):
                            self.execute_trade(ticker, "SELL", snapshot.close, "ALL", "HARD_STOP_LOSS")

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
        if ticker not in self.positions: return
        pos = self.positions[ticker]
        now = datetime.now()
        peak = pos.get("peak_price", pos["entry_price"])
        profit_pct = (curr_price - pos["entry_price"]) / pos["entry_price"]
        
        # ── Layer 1: Adaptive Indicator-Driven Exits ──
        if context and self.cfg.get("stocks", {}).get("adaptive_exits", {}).get("enabled", False):
            a_cfg = self.cfg["stocks"]["adaptive_exits"]
            atr = context.get("atr", 0)
            score = context.get("score", 0)
            regime = context.get("regime", "NORMAL")
            
            # A. Chandelier ATR Trailing Stop (from Peak)
            sl_dist = atr * a_cfg.get("atr_sl_mult", 2.5)
            if sl_dist > 0 and curr_price <= (peak - sl_dist):
                self.execute_trade(ticker, "SELL", curr_price, "ALL", f"ADAPTIVE_ATR_STOP (peak={peak:.1f}, dist={sl_dist:.1f})")
                return
            
            # B. Momentum Rollover (Exhaustion)
            if score > a_cfg.get("rollover_score_threshold", 70) and context.get("mom_state") == 2:
                self.execute_trade(ticker, "SELL", curr_price, "ALL", f"MOMENTUM_ROLLOVER (score={score:.1f})")
                return
                
            # C. Regime-Aware Profit Targets
            target = a_cfg.get("profit_target_strong", 0.15) if regime == "STRONG" else a_cfg.get("profit_target_weak", 0.05)
            if profit_pct >= target:
                self.execute_trade(ticker, "SELL", curr_price, "ALL", f"REGIME_TARGET_HIT ({regime}: {target:.0%})")
                return

        # ── Layer 1.5: Fixed 2% Trailing Stop (unrealized-profit gated) ──
        # Only activates after position has shown at least 1% unrealized profit.
        # Prevents entry-stage noise from triggering premature exit.
        # Pure losses go to Hard Stop (Layer 3).
        profit_pct = (curr_price - pos["entry_price"]) / pos["entry_price"]
        peak_profit_pct = (peak - pos["entry_price"]) / pos["entry_price"] if peak > 0 else 0
        drawdown_from_peak = (peak - curr_price) / peak if peak > 0 else 0
        if peak_profit_pct > 0.01 and drawdown_from_peak >= 0.02:
            self.execute_trade(ticker, "SELL", curr_price, "ALL", f"TRAIL_2PCT (peak={peak:.1f}, dd={drawdown_from_peak:.1%})")
            return
        # ── Layer 1.6: Max Holding Time ──
        # Prevents dead positions from locking budget indefinitely.
        # hold_bars increments ~1/sec (run_iteration ticker loop), so 1800 ≈ 30 min.
        max_hold_seconds = self.cfg.get("stocks", {}).get("max_hold_seconds", 1800)
        if pos.get("hold_bars", 0) >= max_hold_seconds:
            self.execute_trade(ticker, "SELL", curr_price, "ALL", f"MAX_HOLD ({pos['hold_bars']}s/{max_hold_seconds}s)")
            return

        # ── Layer 2: EOD Smart Exit (Final Safety Window) ──
        # NOTE: 零股禁止當沖 — today's buy will be blocked by execute_trade's day-trade guard.
        # This layer only affects overnight positions held from prior days.
        if now.hour == 13 and now.minute >= 20:
            pnl_pct = (curr_price - pos["entry_price"]) / pos["entry_price"]
            if pnl_pct <= 0:
                self.execute_trade(ticker, "SELL", curr_price, "ALL", "TIME_EXIT_LOSER")
            elif now.minute >= 25:
                self.cancel_all_pending_orders()
                self.execute_trade(ticker, "SELL", curr_price, "ALL", "TIME_EXIT_FINAL")
            if ticker in self.positions:
                return  # only return if still holding (day-trade guard blocked sell)
            
        # ── Layer 3: Hard Stop Loss (Circuit Breaker) ──
        sl_pct = self.cfg.get("stocks", {}).get("stop_loss_pct", 0.05)
        if curr_price <= pos["entry_price"] * (1 - sl_pct):
            self.execute_trade(ticker, "SELL", curr_price, "ALL", "HARD_STOP_LOSS")

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

            self.positions[ticker] = {"stage": qty_mode, "entry_price": price, "qty": self.positions.get(ticker, {}).get("qty", 0) + qty}
            console.print(f"[cyan]🚀 [{self.mode_tag}] BUY {ticker} | Qty: {qty} | Reason: {reason}[/cyan]")
            self._log_trade(ticker, "BUY", price, qty, reason, 0.0, price)

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
            for ticker in self.watchlist:
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
