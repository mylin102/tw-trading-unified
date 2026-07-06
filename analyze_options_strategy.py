#!/usr/bin/env python3
"""
期權交易策略模式分析
分析夜盤22筆THETA策略交易
"""
import csv
from datetime import datetime
from collections import defaultdict

# 讀取期權交易數據
trades = []
with open('./strategies/options/logs/paper_trading/options_trade_ledger.csv', 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        # 只分析夜盤時段的交易
        if '2026-04-15' in row['Timestamp'] or '2026-04-16' in row['Timestamp']:
            trades.append(row)

print("=== 期權交易策略分析 ===")
print(f"夜盤時段總交易筆數: {len(trades)}")

# 分類交易類型
entry_trades = [t for t in trades if 'THETA_ENTRY' in t['Note']]
exit_trades = [t for t in trades if 'THETA_EXIT' in t['Note']]

print(f"進場交易: {len(entry_trades)} 筆")
print(f"出場交易: {len(exit_trades)} 筆")

# 分析交易配對
print("\n=== 交易配對分析 ===")
paired_trades = []
for i in range(min(len(entry_trades), len(exit_trades))):
    entry = entry_trades[i]
    exit_trade = exit_trades[i] if i < len(exit_trades) else None
    
    if exit_trade:
        # 計算持續時間
        entry_time = datetime.strptime(entry['Timestamp'], '%Y-%m-%d %H:%M:%S')
        exit_time = datetime.strptime(exit_trade['Timestamp'], '%Y-%m-%d %H:%M:%S')
        duration = (exit_time - entry_time).total_seconds() / 60
        
        # 解析Note欄位
        entry_note = entry['Note']
        exit_note = exit_trade['Note']
        
        # 提取信用金額
        credit = 0
        if 'credit=' in entry_note:
            try:
                credit_part = entry_note.split('credit=')[1].split()[0]
                credit = float(credit_part)
            except:
                credit = 183  # 預設值
        
        # 提取PnL
        pnl = 0
        if 'pnl=' in exit_note:
            try:
                pnl_part = exit_note.split('pnl=')[1].split()[0]
                pnl = float(pnl_part)
            except:
                pnl = 0
        
        paired_trades.append({
            'entry_time': entry_time,
            'exit_time': exit_time,
            'duration_minutes': duration,
            'credit': credit,
            'pnl': pnl,
            'entry_note': entry_note,
            'exit_note': exit_note,
            'strategy': 'iron_condor' if 'iron_condor' in entry_note else 'unknown'
        })

print(f"成功配對交易: {len(paired_trades)} 組")

# 分析每組交易
print("\n=== 詳細交易分析 ===")
for i, trade in enumerate(paired_trades[:10]):  # 只顯示前10組
    print(f"\n交易組 #{i+1}:")
    print(f"  進場時間: {trade['entry_time'].strftime('%H:%M:%S')}")
    print(f"  出場時間: {trade['exit_time'].strftime('%H:%M:%S')}")
    print(f"  持續時間: {trade['duration_minutes']:.1f} 分鐘")
    print(f"  信用金額: {trade['credit']} 點")
    print(f"  PnL: {trade['pnl']} 點")
    print(f"  策略: {trade['strategy']}")
    print(f"  報酬率: {(trade['pnl']/trade['credit']*100):.1f}%" if trade['credit'] > 0 else "報酬率: N/A")

# 統計分析
print("\n=== 統計分析 ===")
if paired_trades:
    total_credit = sum(t['credit'] for t in paired_trades)
    total_pnl = sum(t['pnl'] for t in paired_trades)
    avg_duration = sum(t['duration_minutes'] for t in paired_trades) / len(paired_trades)
    avg_pnl = total_pnl / len(paired_trades)
    winning_trades = [t for t in paired_trades if t['pnl'] > 0]
    losing_trades = [t for t in paired_trades if t['pnl'] < 0]
    breakeven_trades = [t for t in paired_trades if t['pnl'] == 0]
    
    print(f"總信用金額: {total_credit:.1f} 點")
    print(f"總PnL: {total_pnl:.1f} 點")
    print(f"平均持續時間: {avg_duration:.1f} 分鐘")
    print(f"平均每筆PnL: {avg_pnl:.1f} 點")
    print(f"獲利交易: {len(winning_trades)} 筆")
    print(f"虧損交易: {len(losing_trades)} 筆")
    print(f"平盤交易: {len(breakeven_trades)} 筆")
    print(f"勝率: {len(winning_trades)/len(paired_trades)*100:.1f}%")
    
    if winning_trades:
        avg_win = sum(t['pnl'] for t in winning_trades) / len(winning_trades)
        print(f"平均獲利: {avg_win:.1f} 點")
    
    if losing_trades:
        avg_loss = sum(t['pnl'] for t in losing_trades) / len(losing_trades)
        print(f"平均虧損: {avg_loss:.1f} 點")
        print(f"損益比: {abs(avg_win/avg_loss):.2f}" if winning_trades else "損益比: N/A")

# 時間段分析
print("\n=== 時間段分析 ===")
time_slots = defaultdict(list)
for trade in paired_trades:
    hour = trade['entry_time'].hour
    if 17 <= hour < 19:
        slot = "傍晚 (17-19)"
    elif 19 <= hour < 21:
        slot = "晚上 (19-21)"
    elif 21 <= hour < 23:
        slot = "深夜 (21-23)"
    elif 23 <= hour < 1:
        slot = "午夜 (23-01)"
    elif 1 <= hour < 3:
        slot = "凌晨 (01-03)"
    elif 3 <= hour < 5:
        slot = "清晨 (03-05)"
    else:
        slot = "其他"
    
    time_slots[slot].append(trade)

for slot, trades_in_slot in time_slots.items():
    if trades_in_slot:
        avg_pnl = sum(t['pnl'] for t in trades_in_slot) / len(trades_in_slot)
        print(f"{slot}: {len(trades_in_slot)} 筆交易, 平均PnL: {avg_pnl:.1f} 點")

# 策略模式識別
print("\n=== 策略模式識別 ===")

# 1. 交易頻率分析
print("1. 交易頻率分析:")
if paired_trades:
    first_trade = min(t['entry_time'] for t in paired_trades)
    last_trade = max(t['entry_time'] for t in paired_trades)
    total_hours = (last_trade - first_trade).total_seconds() / 3600
    trades_per_hour = len(paired_trades) / total_hours if total_hours > 0 else 0
    print(f"  交易時段: {first_trade.strftime('%H:%M')} - {last_trade.strftime('%H:%M')}")
    print(f"  總時長: {total_hours:.1f} 小時")
    print(f"  每小時交易次數: {trades_per_hour:.1f}")

# 2. 持倉時間分析
print("\n2. 持倉時間分析:")
duration_groups = {
    "超短線 (<5分鐘)": [t for t in paired_trades if t['duration_minutes'] < 5],
    "短線 (5-30分鐘)": [t for t in paired_trades if 5 <= t['duration_minutes'] < 30],
    "中線 (30-60分鐘)": [t for t in paired_trades if 30 <= t['duration_minutes'] < 60],
    "長線 (>60分鐘)": [t for t in paired_trades if t['duration_minutes'] >= 60]
}

for group_name, group_trades in duration_groups.items():
    if group_trades:
        avg_pnl = sum(t['pnl'] for t in group_trades) / len(group_trades)
        print(f"  {group_name}: {len(group_trades)} 筆, 平均PnL: {avg_pnl:.1f} 點")

# 3. 問題識別
print("\n3. 問題識別:")
issues = []

if total_pnl < 0:
    issues.append("總體虧損，策略需要優化")

if len(losing_trades) > len(winning_trades):
    issues.append("虧損交易多於獲利交易，進場條件需要調整")

if any(t['duration_minutes'] < 2 for t in paired_trades):
    issues.append("有交易持倉時間過短（<2分鐘），可能是過度交易")

if any(t['pnl'] < -10 for t in paired_trades):
    issues.append("有單筆虧損過大（>10點），風險控制需要加強")

if issues:
    print("發現的問題:")
    for i, issue in enumerate(issues, 1):
        print(f"  {i}. {issue}")
else:
    print("未發現明顯問題")

# 4. 建議改進方向
print("\n4. 建議改進方向:")
suggestions = [
    "優化THETA策略的進場時機，避免在波動率擴張時進場",
    "調整Iron Condor的履約價間距，增加安全邊際",
    "增加持倉時間過濾，避免過度交易",
    "設置最大虧損限制，加強風險控制",
    "分析不同時間段的策略表現，調整交易時段"
]

for suggestion in suggestions:
    print(f"  - {suggestion}")

print("\n=== 分析完成 ===")
