#!/usr/bin/env python3
"""測試期貨K棒資料獲取"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time
from datetime import datetime, timedelta
import pandas as pd

try:
    # 嘗試導入期貨監控系統
    from strategies.futures.monitor import FuturesMonitor
    
    print("✅ 成功導入FuturesMonitor")
    
    # 創建一個模擬的API對象
    class MockAPI:
        class Contracts:
            class Futures:
                class TMF:
                    pass
        
        def kbars(self, contract, start, end):
            print(f"📊 模擬kbars調用: contract={contract.code if hasattr(contract, 'code') else contract}, start={start}, end={end}")
            # 返回模擬數據
            return {
                "ts": [datetime.now() - timedelta(minutes=i) for i in range(10, 0, -1)],
                "Open": [35500 + i for i in range(10)],
                "High": [35510 + i for i in range(10)],
                "Low": [35490 + i for i in range(10)],
                "Close": [35505 + i for i in range(10)],
                "Volume": [1000 + i*100 for i in range(10)]
            }
    
    # 創建模擬對象
    mock_api = MockAPI()
    
    # 配置文件路徑
    config_path = "config/futures.yaml"
    
    # 創建期貨監控實例（dry_run模式）
    monitor = FuturesMonitor(api=mock_api, config_path=config_path, dry_run=True)
    
    # 手動設置contract
    class MockContract:
        code = "TXFR1"
        delivery_date = "2026-04-16"
    
    monitor.contract = MockContract()
    monitor.api = mock_api
    
    print(f"📝 測試配置: contract={monitor.contract.code}, dry_run={monitor.dry_run}")
    
    # 測試_fetch_today_kbars方法
    print("\n🔍 測試_fetch_today_kbars方法...")
    result = monitor._fetch_today_kbars()
    
    if result is not None:
        print(f"✅ 成功獲取K棒資料: {len(result)} 根")
        print(f"資料範例:\n{result.head()}")
    else:
        print("❌ 無法獲取K棒資料")
        print("可能原因:")
        print("1. dry_run模式被啟用")
        print("2. contract未設置")
        print("3. api未設置")
        print(f"   dry_run={monitor.dry_run}")
        print(f"   contract={monitor.contract}")
        print(f"   api={monitor.api}")
        
except Exception as e:
    print(f"❌ 測試失敗: {e}")
    import traceback
    traceback.print_exc()

print("\n測試完成！")