#!/usr/bin/env python3
"""
修復現有CSV中的Price記錄
從Note欄位提取credit值來更新Price
"""
import pandas as pd
import re
import sys
import os

def extract_credit_from_note(note):
    """從Note欄位提取credit值"""
    if not isinstance(note, str):
        return 0
    
    # 尋找 credit=183 這樣的模式
    match = re.search(r'credit=(\d+)', note)
    if match:
        return float(match.group(1))
    
    # 尋找其他可能的credit表示方式
    match = re.search(r'credit[:=]\s*(\d+)', note)
    if match:
        return float(match.group(1))
    
    return 0

def repair_csv_file(csv_path):
    """修復CSV文件中的Price記錄"""
    print(f"修復文件: {csv_path}")
    
    if not os.path.exists(csv_path):
        print(f"❌ 文件不存在: {csv_path}")
        return False
    
    # 讀取CSV
    df = pd.read_csv(csv_path)
    print(f"原始記錄數: {len(df)}")
    
    # 備份原始文件
    backup_path = csv_path + '.backup'
    df.to_csv(backup_path, index=False)
    print(f"已創建備份: {backup_path}")
    
    # 修復THETA_ENTRY的Price
    theta_entry_mask = df["Action"].str.contains("THETA_ENTRY", na=False)
    theta_entry_count = theta_entry_mask.sum()
    print(f"THETA_ENTRY記錄數: {theta_entry_count}")
    
    repaired_count = 0
    for idx in df[theta_entry_mask].index:
        note = df.at[idx, "Note"]
        credit = extract_credit_from_note(note)
        
        if credit > 0:
            old_price = df.at[idx, "Price"]
            if old_price == 0:
                df.at[idx, "Price"] = credit
                repaired_count += 1
                print(f"  修復記錄 {idx}: Price {old_price} → {credit}")
    
    print(f"修復了 {repaired_count}/{theta_entry_count} 筆THETA_ENTRY記錄")
    
    # 重新計算PnL和Balance（如果需要）
    # 注意：這需要更複雜的邏輯，因為需要重新計算所有交易
    
    # 保存修復後的文件
    df.to_csv(csv_path, index=False)
    print(f"✅ 已保存修復後的文件: {csv_path}")
    
    return True

def main():
    csv_path = "./strategies/options/logs/paper_trading/options_trade_ledger.csv"
    
    if not os.path.exists(csv_path):
        print(f"❌ CSV文件不存在: {csv_path}")
        return 1
    
    print("開始修復CSV文件中的Price記錄")
    print("=" * 60)
    
    if repair_csv_file(csv_path):
        print("\n✅ 修復完成!")
        
        # 驗證修復結果
        print("\n驗證修復結果:")
        df = pd.read_csv(csv_path)
        theta_entries = df[df["Action"].str.contains("THETA_ENTRY", na=False)]
        price_zero = theta_entries[theta_entries["Price"] == 0]
        
        print(f"THETA_ENTRY總數: {len(theta_entries)}")
        print(f"Price為0的THETA_ENTRY: {len(price_zero)}")
        
        if len(price_zero) == 0:
            print("✅ 所有THETA_ENTRY的Price都已修復!")
        else:
            print(f"❌ 仍有 {len(price_zero)} 筆THETA_ENTRY的Price為0")
            print("可能需要手動檢查這些記錄:")
            for idx in price_zero.index:
                print(f"  行 {idx}: {df.at[idx, 'Note']}")
        
        return 0
    else:
        print("❌ 修復失敗")
        return 1

if __name__ == "__main__":
    sys.exit(main())
