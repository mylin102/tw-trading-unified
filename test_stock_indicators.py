#!/usr/bin/env python3
"""
測試股票指標保存功能
"""

import sys
import os
from pathlib import Path

# Add project root to path
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from strategies.stocks.data_storage import StockDataStorage
from datetime import datetime
import pandas as pd

def test_stock_data_storage():
    """測試股票數據存儲功能"""
    print("🧪 測試 StockDataStorage 類...")
    
    # 測試 1: 創建數據存儲器
    storage = StockDataStorage("2330")
    print(f"✅ 創建 StockDataStorage for 2330")
    print(f"   文件路徑: {storage.market_file}")
    print(f"   日期字串: {storage.date_str}")
    
    # 測試 2: 創建模擬指標數據
    test_data = {
        "timestamp": datetime.now(),
        "Open": 800.0,
        "High": 805.0,
        "Low": 795.0,
        "Close": 802.0,
        "Volume": 10000,
        "sqz_on": False,
        "momentum": 0.5,
        "bb_lower": 790.0,
        "bb_mid": 800.0,
        "bb_upper": 810.0,
        "ma20": 798.0,
        "ma60": 795.0,
        "name": "台積電"
    }
    
    # 測試 3: 保存指標
    try:
        storage.save_indicators(test_data["timestamp"], test_data)
        print("✅ 成功保存指標數據")
        
        # 檢查文件是否存在
        if storage.market_file.exists():
            print(f"✅ 文件已創建: {storage.market_file}")
            
            # 讀取文件內容
            df = pd.read_csv(storage.market_file)
            print(f"✅ 成功讀取 CSV 文件")
            print(f"   行數: {len(df)}")
            print(f"   欄位: {list(df.columns)}")
            
            if not df.empty:
                last_row = df.iloc[-1]
                print(f"   最新收盤價: {last_row.get('Close', 'N/A')}")
                print(f"   股票名稱: {last_row.get('name', 'N/A')}")
        else:
            print(f"❌ 文件未創建: {storage.market_file}")
            
    except Exception as e:
        print(f"❌ 保存指標時出錯: {e}")
        import traceback
        traceback.print_exc()
    
    # 測試 4: 檢查目錄結構
    print("\n📁 檢查目錄結構...")
    market_dir = Path("logs/market_data")
    if market_dir.exists():
        print(f"✅ market_data 目錄存在")
        files = list(market_dir.glob("STOCK_*.csv"))
        print(f"   找到 {len(files)} 個股票指標文件")
        for f in files[:5]:
            print(f"   - {f.name}")
    else:
        print(f"❌ market_data 目錄不存在")

if __name__ == "__main__":
    test_stock_data_storage()