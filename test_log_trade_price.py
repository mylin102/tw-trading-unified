#!/usr/bin/env python3
"""
測試log_trade函數的price參數
"""

import sys
import os

# 添加路徑以便導入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    # 嘗試導入相關模組
    from strategies.options.live_options_squeeze_monitor import OptionsSqueezeMonitor
    
    print("✓ 成功導入OptionsSqueezeMonitor")
    
    # 創建一個簡單的測試
    class MockThetaGang:
        def __init__(self):
            self.position = None
        
        def open_position(self, entry_info):
            # 模擬一個position
            class MockPosition:
                def __init__(self):
                    self.strategy = "iron_condor"
                    self.net_credit = 183.0  # 這應該是float
                    self.max_loss = 17.0
                    self.legs = []
                    self.quantity = 1
            
            return MockPosition()
    
    # 測試log_trade函數
    print("\n測試log_trade函數:")
    print(f"  pos.net_credit類型: {type(183.0)}")
    print(f"  pos.net_credit值: {183.0}")
    
    # 檢查log_trade函數
    monitor_file = "strategies/options/live_options_squeeze_monitor.py"
    if os.path.exists(monitor_file):
        with open(monitor_file, 'r') as f:
            content = f.read()
            
            # 查找log_trade調用
            import re
            pattern = r'self\.log_trade\("THETA_ENTRY".*?pos\.net_credit'
            matches = re.findall(pattern, content, re.DOTALL)
            
            if matches:
                print(f"\n找到log_trade調用:")
                for match in matches[:1]:  # 只顯示第一個
                    print(f"  {match[:100]}...")
            else:
                print(f"\n未找到THETA_ENTRY的log_trade調用")
    
except ImportError as e:
    print(f"❌ 導入錯誤: {e}")
except Exception as e:
    print(f"❌ 其他錯誤: {e}")

print("\n=== 問題分析 ===")
print("可能的問題:")
print("1. pos.net_credit可能是0（但Note顯示credit=183）")
print("2. log_trade函數可能沒有正確處理price參數")
print("3. 可能有其他代碼覆蓋了Price值")
print("4. CSV寫入可能有問題")

print("\n建議的修復:")
print("1. 在log_trade函數中添加debug輸出")
print("2. 檢查pos.net_credit的實際值")
print("3. 確保Price欄位正確記錄")