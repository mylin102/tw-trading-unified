# 2026-06-26 Gemini CLI: One-off script to download stock data and calculate indicators for the dashboard
import os
import sys
import time
from pathlib import Path
from datetime import datetime
import pandas as pd
import yaml
import shioaji as sj
from rich.console import Console

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.shioaji_session import get_api, logout
from strategies.stocks.monitor import StockMonitor
from strategies.stocks.data_storage import StockDataStorage
from strategies.options.options_engine.engine.indicators import calculate_stock_squeeze
from strategies.stocks.multi_timeframe import analyze_market_condition

console = Console()

def run_one_off():
    console.print("[cyan]🌐 Logging into Shioaji...[/cyan]")
    api = get_api()
    config_path = ROOT / "config" / "stocks.yaml"
    
    monitor = StockMonitor(api, str(config_path))
    monitor.setup()
    
    # Run daily scan to populate pivot prices
    monitor._run_daily_scan()
    
    now = datetime.now()
    start_date = (now - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")
    
    for ticker in monitor.watchlist:
        console.print(f"\n[bold]📈 Processing {ticker}...[/bold]")
        try:
            contract = api.Contracts.Stocks[ticker]
            if not contract:
                console.print(f"[red]❌ Contract not found for {ticker}[/red]")
                continue
                
            kbars = api.kbars(contract, start=start_date, end=end_date)
            kbars_dict = {**kbars}
            
            if not kbars_dict or not any(len(v) > 0 for v in kbars_dict.values() if hasattr(v, '__len__')):
                console.print(f"[yellow]⚠️ No K-bar data returned for {ticker}[/yellow]")
                continue
                
            df = pd.DataFrame(kbars_dict)
            df["ts"] = pd.to_datetime(df["ts"])
            df = df.set_index("ts")
            
            # Resample to 5min
            resample_cols = {
                'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 
                'Volume': 'sum', 'Amount': 'sum'
            }
            # handle lowercase if needed
            if 'open' in df.columns:
                resample_cols = {k.lower(): v for k, v in resample_cols.items()}
            df = df.resample('5min').agg(resample_cols).dropna()
            df.columns = [c.capitalize() if c.lower() in ["open", "high", "low", "close", "volume"] else c for c in df.columns]
            
            # Calculate squeeze
            df = calculate_stock_squeeze(df)
            df['ma20'] = df['Close'].rolling(20).mean()
            df['ma60'] = df['Close'].rolling(60).mean()
            vol_avg = df['Volume'].rolling(20).mean()
            is_it_buy = (df['Volume'] > vol_avg * 1.5) & (df['Close'] > df['Open']) & (df['Close'] > df['ma20'])
            df['it_buy_rolling_count'] = is_it_buy.rolling(5).sum().fillna(0)
            
            scan_info = monitor.scan_results.get(ticker, {"pattern": "NONE", "pivot": 0.0, "atr_10": 0.0})
            tf_analysis = analyze_market_condition(df)
            
            if not df.empty:
                last_row = df.iloc[-1].to_dict()
                last_row["name"] = contract.name if hasattr(contract, "name") else ticker
                last_row["atr_10"] = scan_info.get("atr_10", 0.0)
                last_row["primary_trend"] = tf_analysis.get("market_state", {}).get("primary_trend", "UNKNOWN")
                last_row["market_regime"] = tf_analysis.get("market_state", {}).get("market_regime", "UNKNOWN")
                last_row["_data_source"] = "api"
                last_row["_trading_disabled"] = False
                
                storage = StockDataStorage(ticker)
                storage.save_indicators(df.index[-1], last_row)
                console.print(f"[green]✅ Indicators successfully saved to {storage.market_file.name}[/green]")
                
        except Exception as e:
            console.print(f"[red]❌ Error processing {ticker}: {e}[/red]")
            
    logout()
    console.print("\n[bold green]✨ All indicators synchronized successfully! Check your Dashboard now.[/bold green]")

if __name__ == "__main__":
    run_one_off()
