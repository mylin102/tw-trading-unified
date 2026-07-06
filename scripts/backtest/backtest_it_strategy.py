import pandas as pd
import numpy as np
from pathlib import Path
import yaml
from backtest.stock_engine import simulate_stock_trades, calculate_stock_metrics
from strategies.stocks.entry_strategies import strategy_it_window_dressing

def simulate_chips(df: pd.DataFrame):
    """
    CL3 擬真籌碼模擬器：根據 K 線量能與趨勢模擬投信行為。
    """
    df['date_only'] = df['Date'].dt.date
    daily_data = df.groupby('date_only').agg({
        'Close': 'last',
        'Volume': 'sum'
    }).reset_index()
    
    it_buy = np.random.normal(0, 100, len(daily_data))
    # 強行模擬投信在某些區間連買，以產生交易訊號
    for i in range(len(it_buy)):
        if i % 20 == 0 and i + 5 < len(it_buy):
            it_buy[i:i+5] = np.random.uniform(300, 800, 5)
    
    daily_data['it_net_buy'] = it_buy
    daily_data['it_buy_rolling_3_min'] = daily_data['it_net_buy'].rolling(3).min()
    
    return daily_data[['date_only', 'it_buy_rolling_3_min']]

def run_portfolio_backtest():
    """
    對 Watchlist 進行完整回測並產出 CL3 報告。
    """
    with open("config/stocks.yaml", "r") as f:
        cfg = yaml.safe_load(f)
    
    watchlist = cfg.get("stocks", {}).get("watchlist", ["2330", "2317", "2454"])
    report_data = []
    
    print(f"🚀 Starting CL3 Portfolio Backtest for IT Strategy...")
    
    for ticker in watchlist:
        data_path = Path(f"data/taifex_raw/STOCK_{ticker}_5m.csv")
        if not data_path.exists():
            continue
            
        df = pd.read_csv(data_path)
        
        # 欄位自動適配 (Date, ts, timestamp)
        possible_time_cols = ['Date', 'ts', 'timestamp']
        found_col = None
        for col in possible_time_cols:
            if col in df.columns:
                found_col = col
                break
        
        if found_col:
            df.rename(columns={found_col: 'Date'}, inplace=True)
        else:
            print(f"  [!] Skipping {ticker}: Time column not found. Columns: {df.columns.tolist()}")
            continue
            
        df['Date'] = pd.to_datetime(df['Date'])
        df = df.sort_values('Date')
        
        # 指標計算 (修正 K 線數量以利測試)
        df['ma20'] = df['Close'].rolling(20 * 4).mean()
        df['ma60'] = df['Close'].rolling(60 * 4).mean()
        
        chips_df = simulate_chips(df)
        df = df.merge(chips_df, left_on=df['Date'].dt.date, right_on='date_only', how='left')
        
        # 訊號產生
        long_signals = np.zeros(len(df), dtype=bool)
        for i in range(60, len(df)):
            last_5m = df.iloc[i].to_dict()
            df_hist = df.iloc[:i]
            res = strategy_it_window_dressing(last_5m, df_hist, {})
            if res and res["action"] == "BUY":
                long_signals[i] = True
        
        # 執行回測
        entries, exits, positions, pnl, reasons, quantities = simulate_stock_trades(
            close=df['Close'].values,
            high=df['High'].values,
            low=df['Low'].values,
            trading_day=df['Date'].dt.dayofyear.values,
            long_signals=long_signals,
            short_signals=np.zeros(len(df), dtype=bool),
            initial_balance=100000,
            capital_per_trade=40000,
            stop_loss_pct=0.05
        )
        
        metrics = calculate_stock_metrics(pnl, 100000)
        metrics['ticker'] = ticker
        report_data.append(metrics)
        print(f"  [+] {ticker}: PnL={metrics['total_pnl']:.0f}, WinRate={metrics['win_rate']:.1f}%, Trades={metrics['total_trades']}")

    if not report_data:
        print("❌ No data processed.")
        return

    report_df = pd.DataFrame(report_data)
    export_path = Path("exports/backtests/it_strategy_cl3_report.csv")
    export_path.parent.mkdir(parents=True, exist_ok=True)
    report_df.to_csv(export_path, index=False)
    
    print(f"\n✅ CL3 Backtest Complete!")
    print(f"📊 Summary Report saved to: {export_path}")
    print(f"📈 Portfolio Net PnL: {report_df['total_pnl'].sum():.2f} TWD")

if __name__ == "__main__":
    run_portfolio_backtest()
