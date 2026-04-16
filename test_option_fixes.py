#!/usr/bin/env python3
"""
驗證期權交易紀錄修復
"""
import pandas as pd
import sys
import os

def test_price_recording():
    """測試Price記錄是否正確"""
    print("=== 測試Price記錄 ===")
    
    csv_path = "./strategies/options/logs/paper_trading/options_trade_ledger.csv"
    if not os.path.exists(csv_path):
        print(f"❌ CSV文件不存在: {csv_path}")
        return False
    
    df = pd.read_csv(csv_path)
    print(f"總記錄數: {len(df)}")
    
    # 檢查THETA_ENTRY的Price
    theta_entries = df[df["Action"].str.contains("THETA_ENTRY")]
    print(f"THETA_ENTRY筆數: {len(theta_entries)}")
    
    price_zero = theta_entries[theta_entries["Price"] == 0]
    print(f"Price為0的THETA_ENTRY: {len(price_zero)}")
    
    if len(price_zero) > 0:
        print("❌ 仍有THETA_ENTRY的Price為0")
        return False
    else:
        print("✅ 所有THETA_ENTRY的Price都不為0")
        return True

def test_theta_exit_recognition():
    """測試THETA_EXIT是否被正確識別"""
    print("\n=== 測試THETA_EXIT識別 ===")
    
    # 檢查log_trade函數
    monitor_path = "./strategies/options/live_options_squeeze_monitor.py"
    with open(monitor_path, 'r') as f:
        content = f.read()
    
    if '"THETA_EXIT"' in content:
        print("✅ THETA_EXIT在exit_keywords列表中")
        return True
    else:
        print("❌ THETA_EXIT不在exit_keywords列表中")
        return False

def test_net_credit_validation():
    """測試net_credit驗證"""
    print("\n=== 測試net_credit驗證 ===")
    
    theta_gang_path = "./strategies/options/theta_gang.py"
    with open(theta_gang_path, 'r') as f:
        content = f.read()
    
    if 'net_credit <= 0' in content:
        print("✅ net_credit <= 0的驗證已添加")
        return True
    else:
        print("❌ net_credit <= 0的驗證未添加")
        return False

def test_dashboard_logic():
    """測試dashboard顯示邏輯"""
    print("\n=== 測試dashboard顯示邏輯 ===")
    
    dashboard_path = "./ui/dashboard.py"
    with open(dashboard_path, 'r') as f:
        content = f.read()
    
    if 'THETA_EXIT' in content and 'format_options_trades' in content:
        print("✅ dashboard包含THETA_EXIT處理邏輯")
        return True
    else:
        print("❌ dashboard缺少THETA_EXIT處理邏輯")
        return False

def main():
    print("期權交易紀錄修復驗證測試")
    print("=" * 50)
    
    tests = [
        test_price_recording,
        test_theta_exit_recognition,
        test_net_credit_validation,
        test_dashboard_logic,
    ]
    
    results = []
    for test in tests:
        try:
            result = test()
            results.append(result)
        except Exception as e:
            print(f"❌ 測試失敗: {e}")
            results.append(False)
    
    passed = sum(results)
    total = len(results)
    
    print("\n" + "=" * 50)
    print(f"測試結果: {passed}/{total} 通過")
    
    if passed == total:
        print("✅ 所有測試通過!")
        return 0
    else:
        print("❌ 有測試失敗，需要進一步修復")
        return 1

if __name__ == "__main__":
    sys.exit(main())
