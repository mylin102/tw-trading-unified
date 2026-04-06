import os
import time
import pandas as pd
from datetime import datetime
from pathlib import Path
import yaml
from rich.console import Console

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
        self.watchlist = stk_cfg.get("watchlist", ["2330", "2454"])
        self.capital_limit = stk_cfg.get("capital_per_trade", 20000)
        self.strat_name = stk_cfg.get("strategy", "scout_strategy")
        
        self.positions = {}
        self.running = False
        
        self.date_str = datetime.now().strftime("%Y%m%d")
        self.ledger_path = TRADE_LOGS / f"STOCK_{self.date_str}_trades.csv"
        
        self.stock_account = None
        if not dry_run and self.api:
            accounts = self.api.list_accounts()
            self.stock_account = next((acc for acc in accounts if "Stock" in str(acc.account_type)), None)

    def calculate_odd_qty(self, price):
        """計算在資金上限內可買進的零股數"""
        return int(self.capital_limit // (price * 1.002))

    def run(self):
        """背景執行緒的主迴圈"""
        self.running = True
        console.print(f"[bold green]🍎 StockMonitor Started | Watchlist: {len(self.watchlist)} tickers[/bold green]")
        
        from strategies.stocks.entry_strategies import STOCK_STRATEGIES
        from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze
        
        if self.strat_name not in STOCK_STRATEGIES:
            console.print(f"[red]Error: Strategy '{self.strat_name}' not found.[/red]")
            return
            
        strat_fn = STOCK_STRATEGIES[self.strat_name]["func"]
        
        while self.running:
            now = datetime.now()
            # 台股交易時間：09:00 - 13:30
            if now.hour < 9 or (now.hour == 13 and now.minute > 30) or now.hour >= 14:
                time.sleep(60)
                continue
                
            for ticker in self.watchlist:
                if not self.running: break
                try:
                    contract = self.api.Contracts.Stocks[ticker]
                    
                    # 1. 抓取整股歷史 K 線 (Analysis)
                    start_date = (now - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
                    kbars = self.api.kbars(contract, start=start_date)
                    df = pd.DataFrame({**kbars})
                    
                    if df.empty: 
                        continue
                        
                    date_col = "ts" if "ts" in df.columns else "Date"
                    df[date_col] = pd.to_datetime(df[date_col])
                    df = df.set_index(date_col)
                    df.columns = [c.capitalize() if c.lower() in ["open", "high", "low", "close", "volume"] else c for c in df.columns]
                    
                    if len(df) < 20:
                        continue
                        
                    df = calculate_futures_squeeze(df)
                    
                    # 匯出即時指標給 8500 Dashboard 讀取
                    ind_path = MKT_LOGS / f"STOCK_{ticker}_{self.date_str}_indicators.csv"
                    df.tail(60).to_csv(ind_path)
                    
                    # 2. 策略評估
                    last_5m = df.iloc[-1]
                    state = {
                        "last_5m": last_5m,
                        "last_15m": last_5m, # 簡化
                        "df_5m": df,
                        "scout_stage": self.positions.get(ticker, {}).get("stage", "IDLE"),
                        "scout_entry_price": self.positions.get(ticker, {}).get("entry_price", 0.0)
                    }
                    
                    res = strat_fn(state, self.cfg)
                    
                    # 3. 取得即時零股快照 (Execution)
                    snapshot = self.api.snapshots([contract])[0]
                    curr_price = snapshot.close
                    
                    # 檢查止損與出場
                    self.check_stops(ticker, curr_price)
                    
                    # 執行進場/加碼
                    if res and res["action"] == "BUY":
                        reason = res.get("reason", "UNKNOWN")
                        qty_mode = res.get("qty_mode", "SCOUT")
                        self.execute_trade(ticker, "BUY", curr_price, qty_mode, reason)
                        
                except Exception as e:
                    console.print(f"[red]StockMonitor error on {ticker}: {e}[/red]")
            
            # 每分鐘輪詢一次 Watchlist
            time.sleep(60) 

    def check_stops(self, ticker, curr_price):
        """每日 13:20 強制出場，或跌破硬性止損比例"""
        if ticker not in self.positions: return
        pos = self.positions[ticker]
        now = datetime.now()
        
        # 13:20 強制平倉 (當沖防呆，確保不過夜)
        if now.hour == 13 and now.minute >= 20:
            self.execute_trade(ticker, "SELL", curr_price, "ALL", "TIME_EXIT_CLOSE")
            return
            
        # 硬性止損 (例如 3%)
        sl_pct = self.cfg.get("stocks", {}).get("stop_loss_pct", 0.03)
        if curr_price <= pos["entry_price"] * (1 - sl_pct):
            self.execute_trade(ticker, "SELL", curr_price, "ALL", "HARD_STOP_LOSS")
            
    def execute_trade(self, ticker, action, price, qty_mode, reason):
        """模擬下單紀錄，寫入 trades.csv 供 Dashboard 讀取"""
        qty = 10 if qty_mode == "SCOUT" else self.calculate_odd_qty(price)
        if qty <= 0 and action == "BUY": return
        
        pnl = 0.0
        if action == "SELL":
            qty = self.positions[ticker]["qty"]
            pnl = (price - self.positions[ticker]["entry_price"]) * qty
            del self.positions[ticker]
            color = "green" if pnl > 0 else "red"
            console.print(f"[{color}]🏁 SELL {ticker} | Price: {price} | PnL: {pnl:+.0f} | Reason: {reason}[/]")
        else:
            self.positions[ticker] = {
                "stage": qty_mode,
                "entry_price": price,
                "qty": qty
            }
            console.print(f"[cyan]🚀 BUY {ticker} ({qty_mode}) | Price: {price} | Qty: {qty} | Reason: {reason}[/cyan]")
            
        # 寫入 CSV
        trade_df = pd.DataFrame([{
            "timestamp": datetime.now(),
            "ticker": ticker,
            "action": action,
            "price": price,
            "qty": qty,
            "reason": reason,
            "pnl_cash": pnl
        }])
        hdr = not self.ledger_path.exists()
        trade_df.to_csv(self.ledger_path, mode='a', header=hdr, index=False)

    def stop(self):
        """停止監控器"""
        self.running = False
