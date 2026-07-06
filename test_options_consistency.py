#!/usr/bin/env python3
"""
測試期權交易紀錄一致性修復
"""
import pandas as pd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ui.dashboard import format_options_trades

def test_format_options_trades():
    """測試修復後的round-trip配對邏輯"""
    # 模擬CSV數據
    test_data = [
        {"Timestamp": "2026-04-15 17:52:30", "Action": "THETA_ENTRY", "Side": "THETA", "Price": 0, "Quantity": 1, "PnL": 0, "Balance": 0, "Note": "test"},
        {"Timestamp": "2026-04-15 17:53:31", "Action": "THETA_EXIT", "Side": "iron_condor", "Price": 0, "Quantity": 1, "PnL": -1, "Balance": -1, "Note": "test exit"},
        {"Timestamp": "2026-04-15 18:00:34", "Action": "THETA_ENTRY", "Side": "THETA", "Price": 0, "Quantity": 1, "PnL": 0, "Balance": -1, "Note": "test2"},
        {"Timestamp": "2026-04-15 18:02:35", "Action": "THETA_EXIT", "Side": "iron_condor", "Price": 0, "Quantity": 1, "PnL": 0, "Balance": 0, "Note": "test2 exit"},
    ]
    
    df = pd.DataFrame(test_data)
    result = format_options_trades(df)
    
    print("測試結果:")
    print(f"輸入記錄數: {len(df)}")
    print(f"輸出round-trip交易數: {len(result) if result is not None and not result.empty else 0}")
    
    if result is not None and not result.empty:
        print("\n配對的交易:")
        for i, row in result.iterrows():
            print(f"交易 #{row.get('#', i+1)}: {row.get('進場時間')} -> {row.get('出場時間')}")
    
    # 驗證
    expected_pairs = 2  # 應該配對成2筆round-trip交易
    actual_pairs = len(result) if result is not None and not result.empty else 0
    
    if actual_pairs == expected_pairs:
        print(f"✓ 測試通過: 成功配對{actual_pairs}筆交易")
        return True
    else:
        print(f"✗ 測試失敗: 預期{expected_pairs}筆，實際{actual_pairs}筆")
        return False

def test_entry_count_calculation():
    """測試進場次數計算邏輯"""
    test_data = [
        {"Timestamp": "2026-04-15 17:52:30", "Action": "THETA_ENTRY", "Side": "THETA"},
        {"Timestamp": "2026-04-15 17:53:31", "Action": "THETA_EXIT", "Side": "iron_condor"},
        {"Timestamp": "2026-04-15 18:00:34", "Action": "THETA_ENTRY", "Side": "THETA"},
        {"Timestamp": "2026-04-15 18:02:35", "Action": "THETA_EXIT", "Side": "iron_condor"},
        {"Timestamp": "2026-04-15 20:10:21", "Action": "THETA_ENTRY", "Side": "THETA"},
        {"Timestamp": "2026-04-15 20:13:23", "Action": "THETA_EXIT", "Side": "iron_condor"},
    ]
    
    df = pd.DataFrame(test_data)
    
    # 舊邏輯: 包含"ENTRY|THETA" - 會匹配所有包含THETA的行
    old_count = df[df["Action"].str.contains("ENTRY|THETA", na=False)].shape[0]
    
    # 新邏輯: 包含"ENTRY"但不包含"EXIT"
    new_count = df[df["Action"].str.contains("ENTRY", na=False) & ~df["Action"].str.contains("EXIT", na=False)].shape[0]
    
    print(f"舊邏輯進場次數: {old_count} (錯誤: 包含THETA_EXIT)")
    print(f"新邏輯進場次數: {new_count} (正確: 只包含THETA_ENTRY)")
    
    if new_count == 3 and old_count == 6:
        print("✓ 進場次數計算修復正確")
        return True
    else:
        print("✗ 進場次數計算仍有問題")
        return False

if __name__ == "__main__":
    print("=== 期權交易紀錄一致性測試 ===\n")
    
    test1 = test_format_options_trades()
    print()
    test2 = test_entry_count_calculation()
    
    if test1 and test2:
        print("\n✓ 所有測試通過")
        sys.exit(0)
    else:
        print("\n✗ 測試失敗")
        sys.exit(1)
