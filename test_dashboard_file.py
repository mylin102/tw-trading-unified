#!/usr/bin/env python3
import sys
sys.path.append('.')
from ui.dashboard import TRADE_DATE_STR
import os
import pandas as pd

print(f"TRADE_DATE_STR: {TRADE_DATE_STR}")
file_path = f"./exports/trades/TMF_{TRADE_DATE_STR}_trades.csv"
print(f"Dashboard尋找的文件: {file_path}")
print(f"文件存在: {os.path.exists(file_path)}")

if os.path.exists(file_path):
    df = pd.read_csv(file_path)
    print(f"找到 {len(df)} 筆交易記錄")
    print("前3筆交易:")
    print(df.head(3))
    
    # 檢查是否有今天的交易
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    today_trades = df[df['timestamp'].str.contains(today)]
    print(f"\n今天({today})的交易: {len(today_trades)}筆")
    if not today_trades.empty:
        print(today_trades)
