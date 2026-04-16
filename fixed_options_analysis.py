#!/usr/bin/env python3
"""
期權交易策略模式分析 - 修正版
"""
import csv
from datetime import datetime
from collections import defaultdict

# 讀取期權交易數據
trades = []
with open('./strategies/options/logs/paper_trading/options_trade_ledger.csv', 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        trades.append(row)

print("=== 期權交易策略分析 ===")
print(f"總交易筆數: {len(trades)}")

# 顯示前幾筆交易結構
print("\n交易數據結構範例:")
for i, row in enumerate(trades[:3]):
    print(f"交易 #{i+1}:")
    for key, value in row.items():
        print(f"  {key}: {value}")
    print()

# 分類交易類型
entry_trades = []
exit_trades = []

for trade in trades:
    note = trade.get('Note', '')
    if 'THETA_ENTRY' in note:
        entry_trades.append(trade)
    elif 'THETA_EXIT' in note:
        exit_trades.append(trade)

print(f"THETA_ENTRY交易: {len(entry_trades)} 筆")
print(f"THETA_EXIT交易: {len(exit_trades)} 筆")

# 分析夜盤時段交易
print("\n=== 夜盤時段交易分析 ===")
night_trades = []
for trade in trades:
    timestamp = trade.get('Timestamp', '')
    note = trade.get('Note', '')
    
    # 檢查是否為夜盤時段 (17:00之後)
    if '2026-04-15' in timestamp or '2026-04-16' in timestamp:
        try:
            dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
            if dt.hour >= 17 or dt.hour < 5:  # 夜盤時段
                night_trades.append(trade)
        except:
            continue

print(f"夜盤時段總交易筆數: {len(night_trades)}")

# 分析夜盤THETA交易
night_theta_entries = [t for t in night_trades if 'THETA_ENTRY' in t.get('Note', '')]
night_theta_exits = [t for t in night_trades if 'THETA_EXIT' in t.get('Note', '')]

print(f"夜盤THETA_ENTRY: {len(night_theta_entries)} 筆")
print(f"夜盤THETA_EXIT: {len(night_theta_exits)} 筆")

# 簡單統計
print("\n=== 簡單統計 ===")
if night_theta_entries:
    # 分析價格
    prices = [float(t.get('Price', 0)) for t in night_theta_entries]
    avg_price = sum(prices) / len(prices)
    print(f"THETA_ENTRY平均價格: {avg_price:.1f}")
    
    # 分析時間分布
    times = []
    for trade in night_theta_entries[:10]:  # 只顯示前10筆
        timestamp = trade.get('Timestamp', '')
        note = trade.get('Note', '')
        print(f"  {timestamp}: {note[:50]}...")

print("\n=== 策略問題分析 ===")

# 檢查常見問題
issues = []

# 1. 檢查價格是否為0
zero_price_trades = [t for t in night_theta_entries if float(t.get('Price', 0)) == 0]
if zero_price_trades:
    issues.append(f"有 {len(zero_price_trades)} 筆THETA_ENTRY價格為0")

# 2. 檢查交易頻率
if len(night_theta_entries) > 10:
    issues.append(f"交易頻率過高: {len(night_theta_entries)} 筆進場交易")

# 3. 檢查PnL
total_pnl = sum(float(t.get('Realized PnL', 0)) for t in night_trades)
if total_pnl < 0:
    issues.append(f"總體虧損: {total_pnl:.1f}")

if issues:
    print("發現的問題:")
    for i, issue in enumerate(issues, 1):
        print(f"{i}. {issue}")
else:
    print("未發現明顯問題")

print("\n=== 建議改進方向 ===")
suggestions = [
    "1. 檢查THETA策略的進場邏輯，避免過度交易",
    "2. 驗證價格記錄機制，確保價格正確",
    "3. 分析夜盤不同時段的波動率特性",
    "4. 優化Iron Condor策略的參數設定",
    "5. 增加風險控制機制，限制單日最大交易次數"
]

for suggestion in suggestions:
    print(suggestion)

print("\n=== 分析完成 ===")
