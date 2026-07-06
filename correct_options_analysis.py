#!/usr/bin/env python3
"""
期權交易正確分析
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

print("=== 期權交易分析 ===")
print(f"總交易筆數: {len(trades)}")

# 分類交易類型
theta_entries = [t for t in trades if t.get('Action') == 'THETA_ENTRY']
theta_exits = [t for t in trades if t.get('Action') == 'THETA_EXIT']

print(f"THETA_ENTRY: {len(theta_entries)} 筆")
print(f"THETA_EXIT: {len(theta_exits)} 筆")

# 分析夜盤交易
print("\n=== 夜盤交易分析 ===")
night_entries = []
night_exits = []

for trade in theta_entries:
    timestamp = trade.get('Timestamp', '')
    try:
        dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
        if dt.hour >= 17 or dt.hour < 5:  # 夜盤時段
            night_entries.append(trade)
    except:
        continue

for trade in theta_exits:
    timestamp = trade.get('Timestamp', '')
    try:
        dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
        if dt.hour >= 17 or dt.hour < 5:  # 夜盤時段
            night_exits.append(trade)
    except:
        continue

print(f"夜盤THETA_ENTRY: {len(night_entries)} 筆")
print(f"夜盤THETA_EXIT: {len(night_exits)} 筆")

# 配對分析
print("\n=== 交易配對分析 ===")
paired_trades = []
min_len = min(len(night_entries), len(night_exits))

for i in range(min_len):
    entry = night_entries[i]
    exit_trade = night_exits[i]
    
    try:
        entry_time = datetime.strptime(entry['Timestamp'], '%Y-%m-%d %H:%M:%S')
        exit_time = datetime.strptime(exit_trade['Timestamp'], '%Y-%m-%d %H:%M:%S')
        duration = (exit_time - entry_time).total_seconds() / 60
        
        # 提取信用金額
        entry_note = entry.get('Note', '')
        credit = 183  # 預設值
        if 'credit=' in entry_note:
            try:
                credit_part = entry_note.split('credit=')[1].split()[0]
                credit = float(credit_part)
            except:
                pass
        
        # 提取PnL
        exit_note = exit_trade.get('Note', '')
        pnl = float(exit_trade.get('PnL', 0))
        
        paired_trades.append({
            'entry_time': entry_time,
            'exit_time': exit_time,
            'duration_minutes': duration,
            'credit': credit,
            'pnl': pnl,
            'entry_price': float(entry.get('Price', 0)),
            'exit_price': float(exit_trade.get('Price', 0))
        })
    except Exception as e:
        continue

print(f"成功配對: {len(paired_trades)} 組交易")

if paired_trades:
    print("\n=== 詳細分析 ===")
    
    # 統計分析
    total_credit = sum(t['credit'] for t in paired_trades)
    total_pnl = sum(t['pnl'] for t in paired_trades)
    avg_duration = sum(t['duration_minutes'] for t in paired_trades) / len(paired_trades)
    avg_pnl = total_pnl / len(paired_trades)
    
    winning = [t for t in paired_trades if t['pnl'] > 0]
    losing = [t for t in paired_trades if t['pnl'] < 0]
    breakeven = [t for t in paired_trades if t['pnl'] == 0]
    
    print(f"總信用金額: {total_credit:.1f} 點")
    print(f"總PnL: {total_pnl:.1f} 點")
    print(f"平均持倉時間: {avg_duration:.1f} 分鐘")
    print(f"平均每筆PnL: {avg_pnl:.1f} 點")
    print(f"獲利交易: {len(winning)} 筆 ({len(winning)/len(paired_trades)*100:.1f}%)")
    print(f"虧損交易: {len(losing)} 筆 ({len(losing)/len(paired_trades)*100:.1f}%)")
    print(f"平盤交易: {len(breakeven)} 筆 ({len(breakeven)/len(paired_trades)*100:.1f}%)")
    
    # 時間段分析
    print("\n=== 時間段表現 ===")
    time_groups = defaultdict(list)
    for trade in paired_trades:
        hour = trade['entry_time'].hour
        if 17 <= hour < 20:
            group = "傍晚 (17-20)"
        elif 20 <= hour < 23:
            group = "晚上 (20-23)"
        elif 23 <= hour < 2:
            group = "深夜 (23-02)"
        elif 2 <= hour < 5:
            group = "凌晨 (02-05)"
        else:
            group = "其他"
        time_groups[group].append(trade)
    
    for group, group_trades in time_groups.items():
        if group_trades:
            avg_pnl = sum(t['pnl'] for t in group_trades) / len(group_trades)
            print(f"{group}: {len(group_trades)} 筆, 平均PnL: {avg_pnl:.1f} 點")
    
    # 持倉時間分析
    print("\n=== 持倉時間表現 ===")
    duration_groups = {
        "超短線 (<2分鐘)": [t for t in paired_trades if t['duration_minutes'] < 2],
        "短線 (2-5分鐘)": [t for t in paired_trades if 2 <= t['duration_minutes'] < 5],
        "中短線 (5-10分鐘)": [t for t in paired_trades if 5 <= t['duration_minutes'] < 10],
        "中線 (10-30分鐘)": [t for t in paired_trades if 10 <= t['duration_minutes'] < 30]
    }
    
    for group_name, group_trades in duration_groups.items():
        if group_trades:
            avg_pnl = sum(t['pnl'] for t in group_trades) / len(group_trades)
            print(f"{group_name}: {len(group_trades)} 筆, 平均PnL: {avg_pnl:.1f} 點")
    
    # 問題識別
    print("\n=== 問題識別 ===")
    issues = []
    
    if total_pnl < 0:
        issues.append(f"總體虧損: {total_pnl:.1f} 點")
    
    if len(losing) > len(winning):
        issues.append(f"虧損交易({len(losing)}筆)多於獲利交易({len(winning)}筆)")
    
    if any(t['duration_minutes'] < 1 for t in paired_trades):
        issues.append("有交易持倉時間過短 (<1分鐘)")
    
    if any(t['pnl'] < -5 for t in paired_trades):
        issues.append("有單筆虧損過大 (>5點)")
    
    if issues:
        print("發現的問題:")
        for i, issue in enumerate(issues, 1):
            print(f"{i}. {issue}")
    else:
        print("未發現明顯問題")

print("\n=== 分析完成 ===")
