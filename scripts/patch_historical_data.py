#!/usr/bin/env python3
"""
補資料腳本 (Data Patcher)
用途：從 Shioaji API 抓取 3/27 至今的完整歷史數據，計算指標並更新日誌。
"""
import sys
import os
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

# 加入專案根目錄
sys.path.append(str(Path(__file__).parent.parent))

from core.shioaji_session import get_api
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze

def patch_data(ticker="TMF", start_date="2026-03-27"):
    api = get_api()
    print(f"✅ API Logged in. Fetching {ticker} history from {start_date}...")

    # 1. 尋找合約
    category = "TMF" if ticker == "TMF" else "MXF"
    contracts_list = [c for c in api.Contracts.Futures[category]]
    # 找最近月合約 (R1 或目前的 D6)
    target = next((c for c in contracts_list if "R1" in c.code or "D6" in c.code), contracts_list[0])
    print(f"Using contract: {target.code} ({target.name})")

    # 2. 抓取 K 棒
    end_date = datetime.now().strftime("%Y-%m-%d")
    print(f"Fetching kbars from {start_date} to {end_date}...")
    kbars = api.kbars(target, start=start_date, end=end_date)
    df = pd.DataFrame({**kbars})
    
    if df.empty:
        print("❌ No data returned from API.")
        return

    df["ts"] = pd.to_datetime(df["ts"])
    df.set_index("ts", inplace=True)
    # 確保列名符合 calculate_futures_squeeze 預期
    df = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"
    })

    # 3. 計算指標
    print("Calculating indicators...")
    res = calculate_futures_squeeze(df)

    # 4. 儲存檔案
    # 我們將資料按交易日拆分並儲存 (模仿 monitor.py 邏輯)
    log_dir = Path("logs/market_data")
    log_dir.mkdir(parents=True, exist_ok=True)

    # 根據 trading_day 分組存檔
    for day, group in res.groupby("trading_day"):
        date_str = day.strftime("%Y%m%d")
        # 我們存為 _PAPER 以利儀表板優先讀取，或者您可以改名
        path = log_dir / f"{ticker}_{date_str}_PAPER_indicators.csv"
        
        # 如果是今天的檔案，我們可能想保留即時產出的部分（或直接覆蓋更準確的歷史資料）
        group_to_save = group.drop(columns=["trading_day"]) # 移除計算用中間欄位
        if "mom_prev" in group_to_save.columns: 
            group_to_save = group_to_save.drop(columns=["mom_prev"])
        
        group_to_save.index.name = "timestamp"
        group_to_save.to_csv(path, index=True)
        print(f"💾 Saved {len(group)} bars to {path.name}")

    print("\n✅ Patching complete!")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default="TMF", help="TMF or MTX")
    parser.add_argument("--start", default="2026-03-27", help="Start date YYYY-MM-DD")
    args = parser.parse_args()
    
    patch_data(ticker=args.ticker, start_date=args.start)
