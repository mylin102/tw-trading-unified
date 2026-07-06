#!/usr/bin/env python3
"""
測試THETA交易Price記錄問題
"""

import pandas as pd
import os

def test_theta_price_recording():
    print("=== 測試THETA交易Price記錄問題 ===\n")
    
    csv_path = "./strategies/options/logs/paper_trading/options_trade_ledger.csv"
    if not os.path.exists(csv_path):
        print(f"❌ CSV檔案不存在: {csv_path}")
        return
    
    df = pd.read_csv(csv_path)
    
    print("1. 分析THETA交易記錄:")
    
    theta_entries = df[df['Action'] == 'THETA_ENTRY']
    theta_exits = df[df['Action'] == 'THETA_EXIT']
    
    print(f"   THETA_ENTRY記錄數: {len(theta_entries)}")
    print(f"   THETA_EXIT記錄數: {len(theta_exits)}")
    
    print(f"\n2. Price欄位分析:")
    print(f"   THETA_ENTRY Price統計:")
    print(f"     - 最小值: {theta_entries['Price'].min()}")
    print(f"     - 最大值: {theta_entries['Price'].max()}")
    print(f"     - 平均值: {theta_entries['Price'].mean():.2f}")
    print(f"     - 等於0的數量: {(theta_entries['Price'] == 0).sum()}")
    
    print(f"\n   THETA_EXIT Price統計:")
    print(f"     - 最小值: {theta_exits['Price'].min()}")
    print(f"     - 最大值: {theta_exits['Price'].max()}")
    print(f"     - 平均值: {theta_exits['Price'].mean():.2f}")
    print(f"     - 等於0的數量: {(theta_exits['Price'] == 0).sum()}")
    
    print(f"\n3. Note欄位分析 (提取credit值):")
    
    # 從Note欄位提取credit值
    credit_values = []
    for note in theta_entries['Note'].dropna():
        if 'credit=' in str(note):
            parts = str(note).split('credit=')
            if len(parts) > 1:
                credit_str = parts[1].split()[0] if ' ' in parts[1] else parts[1]
                try:
                    credit = float(credit_str)
                    credit_values.append(credit)
                except:
                    pass
    
    if credit_values:
        print(f"   Note中的credit值: {credit_values}")
        print(f"   credit平均值: {sum(credit_values)/len(credit_values):.1f}")
        print(f"   credit應為Price值，但Price都是0")
    else:
        print(f"   ⚠ 無法從Note提取credit值")
    
    print(f"\n4. PnL和Balance分析:")
    print(f"   THETA_EXIT PnL統計:")
    print(f"     - 最小值: {theta_exits['PnL'].min()}")
    print(f"     - 最大值: {theta_exits['PnL'].max()}")
    print(f"     - 平均值: {theta_exits['PnL'].mean():.2f}")
    
    print(f"\n   Balance序列:")
    balances = df['Balance'].tolist()
    print(f"     {balances}")
    
    # 檢查Balance是否累計
    cumulative_pnl = df['PnL'].sum()
    last_balance = balances[-1] if balances else 0
    
    print(f"\n5. 問題診斷:")
    print(f"   - 累計PnL總和: {cumulative_pnl:.2f}")
    print(f"   - 最後Balance值: {last_balance:.2f}")
    
    if abs(cumulative_pnl - last_balance) < 0.01:
        print(f"   ✓ Balance計算正確 (累計PnL = 最後Balance)")
    else:
        print(f"   ❌ Balance計算錯誤 (累計PnL={cumulative_pnl:.2f} ≠ 最後Balance={last_balance:.2f})")
    
    print(f"\n6. 根本原因推測:")
    print(f"   問題: Price欄位都是0，但Note顯示credit=183")
    print(f"   可能原因:")
    print(f"     1. log_trade函數的price參數被傳入0")
    print(f"     2. pos.net_credit實際上是0")
    print(f"     3. 有bug導致price值被覆蓋")
    
    print(f"\n   需要檢查:")
    print(f"     1. live_options_squeeze_monitor.py第2334行: self.log_trade('THETA_ENTRY', 'THETA', pos.net_credit, ...)")
    print(f"     2. theta_gang.py中的net_credit計算")
    print(f"     3. log_trade函數是否正確處理price參數")

if __name__ == "__main__":
    test_theta_price_recording()