#!/usr/bin/env python3
"""
測試Dashboard修復驗證
驗證期權交易紀錄不一致問題的修復
"""

import os
import sys
import pandas as pd

def test_dashboard_fixes():
    print("=== Dashboard修復驗證測試 ===\n")
    
    # 1. 檢查CSV數據
    csv_path = "./strategies/options/logs/paper_trading/options_trade_ledger.csv"
    if not os.path.exists(csv_path):
        print(f"❌ CSV檔案不存在: {csv_path}")
        return False
    
    df = pd.read_csv(csv_path)
    print(f"1. CSV數據檢查:")
    print(f"   - 總行數: {len(df)}")
    print(f"   - 標題行: {', '.join(df.columns.tolist())}")
    
    # 統計THETA交易
    theta_entries = df[df['Action'] == 'THETA_ENTRY']
    theta_exits = df[df['Action'] == 'THETA_EXIT']
    
    print(f"   - THETA_ENTRY數量: {len(theta_entries)}")
    print(f"   - THETA_EXIT數量: {len(theta_exits)}")
    
    if len(theta_entries) == len(theta_exits):
        print(f"   ✓ THETA交易配對正確: {len(theta_entries)} 筆round-trip交易")
    else:
        print(f"   ❌ THETA交易未配對: ENTRY={len(theta_entries)}, EXIT={len(theta_exits)}")
    
    # 2. 模擬dashboard的overview計算
    print(f"\n2. Overview頁面計算:")
    entries = df[df['Action'].str.contains('ENTRY', na=False) & ~df['Action'].str.contains('EXIT', na=False)]
    print(f"   - 應顯示進場次數: {len(entries)} 筆")
    
    if len(entries) == len(theta_entries):
        print(f"   ✓ Overview計算正確")
    else:
        print(f"   ❌ Overview計算錯誤: 應為{len(theta_entries)}筆")
    
    # 3. 檢查THETA交易的PnL計算
    print(f"\n3. THETA交易PnL檢查:")
    
    # 檢查是否有Price=0的問題
    zero_price_entries = theta_entries[theta_entries['Price'] == 0]
    zero_price_exits = theta_exits[theta_exits['Price'] == 0]
    
    print(f"   - Price=0的THETA_ENTRY: {len(zero_price_entries)} 筆")
    print(f"   - Price=0的THETA_EXIT: {len(zero_price_exits)} 筆")
    
    if len(zero_price_entries) > 0:
        print(f"   ⚠ THETA_ENTRY的Price應為收取的權利金，不應為0")
        print(f"     需要修復live_options_squeeze_monitor.py中的記錄邏輯")
    
    # 4. 檢查Balance計算
    print(f"\n4. Balance累計檢查:")
    
    # 模擬正確的Balance計算
    balance = 0
    for idx, row in df.iterrows():
        if row['Action'] == 'THETA_ENTRY':
            # 進場時Balance不變
            pass
        elif row['Action'] == 'THETA_EXIT':
            # 出場時更新Balance
            pnl = row['PnL']
            if pd.notna(pnl):
                balance += pnl
    
    print(f"   - 最後一筆的Balance: {df.iloc[-1]['Balance'] if 'Balance' in df.columns else 'N/A'}")
    print(f"   - 模擬累計Balance: {balance:.1f}")
    
    # 5. 檢查修復狀態
    print(f"\n5. 修復狀態檢查:")
    
    # 檢查dashboard.py修復
    dashboard_path = "./ui/dashboard.py"
    if os.path.exists(dashboard_path):
        with open(dashboard_path, 'r') as f:
            content = f.read()
            
            if "THETA_EXIT" in content and "any(kw in action for kw in" in content:
                print(f"   ✓ dashboard.py已修復THETA_EXIT檢測")
            else:
                print(f"   ❌ dashboard.py未修復THETA_EXIT檢測")
            
            if "entries = today_l[today_l[\"Action\"].str.contains(\"ENTRY\", na=False) & ~today_l[\"Action\"].str.contains(\"EXIT\", na=False)]" in content:
                print(f"   ✓ dashboard.py的overview計算邏輯正確")
            else:
                print(f"   ❌ dashboard.py的overview計算邏輯可能有問題")
    
    # 檢查期權監控修復
    monitor_path = "./strategies/options/live_options_squeeze_monitor.py"
    if os.path.exists(monitor_path):
        print(f"   ✓ live_options_squeeze_monitor.py存在")
        # 實際修復需要檢查檔案內容
    else:
        print(f"   ⚠ live_options_squeeze_monitor.py不存在")
    
    print(f"\n=== 總結 ===")
    print(f"原始問題:")
    print(f"  1. Dashboard顯示4筆option交易")
    print(f"  2. CSV有12筆記錄 (6 THETA_ENTRY + 6 THETA_EXIT)")
    print(f"  3. Overview顯示進場12筆")
    print(f"  4. 邏輯不一致")
    
    print(f"\n修復後預期:")
    print(f"  1. Dashboard應顯示: {len(theta_entries)} 筆交易")
    print(f"  2. Overview應顯示: {len(entries)} 筆進場")
    print(f"  3. 所有顯示應一致")
    
    print(f"\n驗證結果:")
    if len(theta_entries) == len(theta_exits) and len(entries) == len(theta_entries):
        print(f"  ✓ 數據一致性驗證通過")
        return True
    else:
        print(f"  ❌ 數據一致性驗證失敗")
        return False

if __name__ == "__main__":
    success = test_dashboard_fixes()
    sys.exit(0 if success else 1)