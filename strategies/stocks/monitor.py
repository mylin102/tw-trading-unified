import os
import time
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
import yaml
from rich.console import Console
import shioaji as sj
from strategies.stocks.scanner import StockScanner

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

        # Find yesterday's ledger
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        ledger_file = TRADE_LOGS / f"STOCK_{yesterday}_{self.mode_tag}_trades.csv"

        if not ledger_file.exists():
            console.print(f"[dim]📂 No previous ledger found: {ledger_file}[/dim]")
            return

        try:
            df = pd.read_csv(ledger_file)
            if df.empty:
                console.print(f"[dim]📂 Yesterday's ledger is empty[/dim]")
                return

            # Calculate net positions: BUY qty - SELL qty
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
                    self.positions[ticker] = {
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
                        "reason": f"OVERNIGHT_RECOVERY_{yesterday}",
                        "pnl_gross": 0.0,
                        "fees": 0.0,
                        "pnl_cash": 0.0,
                    }
                    rec_df = pd.DataFrame([recovery_record])
                    today_ledger = TRADE_LOGS / f"STOCK_{datetime.now().strftime('%Y%m%d')}_{self.mode_tag}_trades.csv"
                    header = not today_ledger.exists()
                    rec_df.to_csv(today_ledger, mode='a', header=header, index=False)

            if recovered_count > 0:
                total_value = sum(p["qty"] * p["entry_price"] for p in self.positions.values())
                console.print(f"[bold green]✅ Recovered {recovered_count} positions (total value: ${total_value:,.0f}) — written to today's ledger for dashboard display[/bold green]")
            else:
                console.print(f"[dim]📂 No open positions from yesterday[/dim]")

        except Exception as e:
            console.print(f"[yellow]⚠️ Position recovery failed: {e}[/yellow]")

    def clean_unfilled_orders(self):
        """撤銷超過 5 分鐘未成交的掛單"""
        if self.dry_run: return
        if not hasattr(self.api, 'trades'): return
        now = datetime.now()
        self.api.update_status()
        
        # 檢查 api.trades 中屬於我們這個模式的單
        for trade in self.api.trades:
            if trade.contract.code in self.watchlist:
                # 如果是掛單中 (Submitted) 且超過 5 分鐘
                order_time = datetime.fromtimestamp(trade.status.order_datetime)
                if trade.status.status == sj.constant.OrderState.Submitted and (now - order_time).total_seconds() > 300:
                    console.print(f"[yellow]⏳ Order Timeout: Cancelling {trade.contract.code}...[/yellow]")
                    self.api.cancel_order(trade)

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

    def run(self):
        self.running = True
        console.print(f"[bold green]🍎 StockMonitor [{self.mode_tag}] Started | Strategy: {self.strat_name} | Watchlist: {len(self.watchlist)}[/bold green]")

        # ── GSD: Recover positions from yesterday's ledger ────────────
        self._recover_positions_from_ledger()

        from strategies.stocks.entry_strategies import STOCK_STRATEGIES
        from strategies.options.options_engine.engine.indicators import calculate_stock_squeeze
        from strategies.stocks.multi_timeframe import analyze_market_condition, should_trade_based_on_tf
        
        # 💡 GSD: Run initial scan before entering the loop
        self._run_daily_scan()
        
        strat_fn = STOCK_STRATEGIES[self.strat_name]["func"]
        last_regime_check = 0
        last_daily_scan = time.time()
        active_watchlist = None  # 第二招：每日動態篩選
        
        while self.running:
          try:
            now = datetime.now()
            if now.hour < 9 or (now.hour == 13 and now.minute > 30) or now.hour >= 14:
                time.sleep(60); continue
            
            # 每 30 分鐘更新大盤多空判斷
            if time.time() - last_regime_check > 1800:
                self._update_market_regime()
                last_regime_check = time.time()
            
            # 💡 GSD: Refresh daily scan every 4 hours
            if time.time() - last_daily_scan > 14400:
                self._run_daily_scan()
                last_daily_scan = time.time()
            
            # 第二招：開盤後篩選「成交量前 N + 開盤強勢」
            if active_watchlist is None and now.hour == 9 and now.minute >= 5:
                active_watchlist = self._filter_watchlist_by_strength()
                console.print(f"[cyan]📋 Active watchlist: {len(active_watchlist)} / {len(self.watchlist)} tickers[/cyan]")
            
            # 定期清理掛不到的單
            self.clean_unfilled_orders()
            
            scan_list = active_watchlist if active_watchlist else self.watchlist
                
            for ticker in scan_list:
                if not self.running: break
                try:
                    contract = self.api.Contracts.Stocks[ticker]
                    # Skip suspended/warning stocks if notice attribute exists
                    notice = getattr(contract, 'notice', None)
                    if notice is not None and str(notice) != "Normal": continue
                    
                    # 1. 指標分析
                    start_date = (now - pd.Timedelta(days=14)).strftime("%Y-%m-%d")
                    kbars = self.api.kbars(contract, start=start_date)
                    if not kbars: continue
                    df = pd.DataFrame({**kbars})
                    if df.empty: continue
                    df["ts"] = pd.to_datetime(df["ts"]); df = df.set_index("ts")
                    df.columns = [c.capitalize() if c.lower() in ["open", "high", "low", "close", "volume"] else c for c in df.columns]
                    df = calculate_stock_squeeze(df)
                    
                    # --- 優化：投信作帳指標 (實裝) ---
                    # 改用更適配 5 分K 的均線窗口 (MA20, MA60)
                    df['ma20'] = df['Close'].rolling(20).mean()
                    df['ma60'] = df['Close'].rolling(60).mean()
                    
                    # 代理指標邏輯：成交量 > 均量 1.5倍 且 收紅 且 價格 > 均線
                    vol_avg = df['Volume'].rolling(20).mean()
                    is_it_buy = (df['Volume'] > vol_avg * 1.5) & (df['Close'] > df['Open']) & (df['Close'] > df['ma20'])
                    # 計算過去 5 根中有幾根符合
                    df['it_buy_rolling_count'] = is_it_buy.rolling(5).sum().fillna(0)
                    
                    df["name"] = contract.name
                    df.tail(60).to_csv(MKT_LOGS / f"STOCK_{ticker}_{self.date_str}_indicators.csv")
                    
                    # 2. 策略
                    # 💡 GSD: Include pattern info in state
                    scan_info = self.scan_results.get(ticker, {"pattern": "NONE", "pivot": 0.0})
                    
                    # P2優化：多時間框架確認
                    try:
                        tf_analysis = analyze_market_condition(df)
                        should_trade, tf_details = should_trade_based_on_tf(df)
                        
                        # 如果多時間框架不允許交易，跳過策略執行
                        if not should_trade:
                            console.print(f"[yellow]⏸️ {ticker} 多時間框架過濾: {tf_details.get('trading_recommendation', {}).get('reason', 'TF_FILTER')}[/yellow]")
                            continue
                            
                        state = {
                            "last_5m": df.iloc[-1], "df_5m": df,
                            "scout_stage": self.positions.get(ticker, {}).get("stage", "IDLE"),
                            "scout_entry_price": self.positions.get(ticker, {}).get("entry_price", 0.0),
                            "market_trend": "BEAR" if self.is_bear_market else "BULL",
                            "is_bear_market": self.is_bear_market,
                            "pattern": scan_info["pattern"],
                            "pivot": scan_info["pivot"],
                            "multi_timeframe": tf_analysis,
                            "market_state": tf_analysis.get('market_state', {}),
                            "tf_recommendation": tf_analysis.get('trading_recommendation', {}),
                            "should_trade_tf": should_trade,
                            "tf_details": tf_details
                        }
                    except Exception as e:
                        console.print(f"[yellow]⚠️ {ticker} 多時間框架分析錯誤: {e}[/yellow]")
                        # 錯誤時使用原始state
                        state = {
                            "last_5m": df.iloc[-1], "df_5m": df,
                            "scout_stage": self.positions.get(ticker, {}).get("stage", "IDLE"),
                            "scout_entry_price": self.positions.get(ticker, {}).get("entry_price", 0.0),
                            "market_trend": "BEAR" if self.is_bear_market else "BULL",
                            "is_bear_market": self.is_bear_market,
                            "pattern": scan_info["pattern"],
                            "pivot": scan_info["pivot"],
                        }
                    
                    res = strat_fn(state, self.cfg)
                    
                    # 3. 執行
                    snapshot = self.api.snapshots([contract])[0]
                    self.check_risk(ticker, snapshot.close)
                    
                    if res and res["action"] == "BUY" and not self._check_bear_defense():
                        self.execute_trade(ticker, "BUY", snapshot.close, res.get("qty_mode", "SCOUT"), res.get("reason", "SIGNAL"))
                        
                except Exception as e:
                    console.print(f"[red]Error {ticker}: {e}[/red]")
            
            time.sleep(60)
          except Exception as e:
            # Thread-level safety net: log and continue instead of dying
            console.print(f"[bold red]🍎 StockMonitor loop error (recovering): {e}[/bold red]")
            time.sleep(30)

    def check_risk(self, ticker, curr_price):
        if ticker not in self.positions: return
        pos = self.positions[ticker]
        now = datetime.now()
        
        # 第三招：13:20 只撤退虧損倉，獲利倉抱到 13:25 trailing stop
        if now.hour == 13 and now.minute >= 20:
            pnl_pct = (curr_price - pos["entry_price"]) / pos["entry_price"]
            if pnl_pct <= 0:
                self.execute_trade(ticker, "SELL", curr_price, "ALL", "TIME_EXIT_LOSER")
            elif now.minute >= 25:
                self.execute_trade(ticker, "SELL", curr_price, "ALL", "TIME_EXIT_FINAL")
            return
            
        sl_pct = self.cfg.get("stocks", {}).get("stop_loss_pct", 0.03)
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
                order = self.api.Order(
                    price=price, quantity=qty, action=sj.constant.Action.Buy,
                    price_type=sj.constant.StockPriceType.LMT,
                    order_type=sj.constant.OrderType.ROD,
                    order_lot=sj.constant.StockOrderLot.Odd
                )
                trade = self.api.place_order(contract, order)
                # 等待委託回報確認 (最多 10 秒)
                self.api.update_status()
                if trade.status.status != sj.constant.OrderState.Submitted and trade.status.status != sj.constant.OrderState.Filled:
                    console.print(f"[red]❌ BUY {ticker} order rejected: {trade.status.status}[/red]")
                    return

            self.positions[ticker] = {"stage": qty_mode, "entry_price": price, "qty": self.positions.get(ticker, {}).get("qty", 0) + qty}
            console.print(f"[cyan]🚀 [{self.mode_tag}] BUY {ticker} | Qty: {qty} | Reason: {reason}[/cyan]")
            self._log_trade(ticker, "BUY", price, qty, reason, 0.0, price)

        elif action == "SELL":
            if ticker not in self.positions: return
            pos = self.positions[ticker]
            
            # --- LIVE: 下單後等確認，才更新 position ---
            if self.mode_tag == "LIVE":
                contract = self.api.Contracts.Stocks[ticker]
                order = self.api.Order(
                    price=price, quantity=pos["qty"], action=sj.constant.Action.Sell,
                    price_type=sj.constant.StockPriceType.LMT,
                    order_type=sj.constant.OrderType.ROD,
                    order_lot=sj.constant.StockOrderLot.Odd
                )
                trade = self.api.place_order(contract, order)
                self.api.update_status()
                if trade.status.status != sj.constant.OrderState.Submitted and trade.status.status != sj.constant.OrderState.Filled:
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

    def stop(self): self.running = False
