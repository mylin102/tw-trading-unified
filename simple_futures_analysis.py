#!/usr/bin/env python3
"""
期貨交易策略簡單分析
"""
import csv
from datetime import datetime

# 讀取交易數據
trades = []
with open('./exports/trades/TMF_20260415_trades.csv', 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        trades.append(row)

print("=== 期貨交易詳細分析 ===")
print(f"總交易筆數: {len(trades)}")

# 配對進出場交易
paired_trades = []
for i in range(0, len(trades), 2):
    if i+1 < len(trades):
        entry = trades[i]
        exit_trade = trades[i+1]
        
        # 計算持續時間
        entry_time = datetime.strptime(entry['timestamp'], '%Y-%m-%d %H:%M:%S')
        exit_time = datetime.strptime(exit_trade['timestamp'], '%Y-%m-%d %H:%M:%S')
        duration = (exit_time - entry_time).total_seconds() / 60
        
        paired_trades.append({
            'entry_time': entry_time,
            'exit_time': exit_time,
            'duration_minutes': duration,
            'entry_price': float(entry['price']),
            'exit_price': float(exit_trade['price']),
            'pnl_pts': float(exit_trade['pnl_pts']),
            'pnl_cash': float(exit_trade['pnl_cash']),
            'entry_reason': entry['reason'],
            'exit_reason': exit_trade['reason']
        })

print("\n=== 交易配對分析 ===")
for i, trade in enumerate(paired_trades):
    print(f"\n交易 #{i+1}:")
    print(f"  進場時間: {trade['entry_time'].strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  出場時間: {trade['exit_time'].strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  持續時間: {trade['duration_minutes']:.1f} 分鐘")
    print(f"  進場價格: {trade['entry_price']:.1f}")
    print(f"  出場價格: {trade['exit_price']:.1f}")
    print(f"  價格變動: {trade['exit_price'] - trade['entry_price']:.1f}")
    print(f"  PnL點數: {trade['pnl_pts']:.1f}")
    print(f"  PnL現金: {trade['pnl_cash']:.1f}")
    print(f"  進場理由: {trade['entry_reason']}")
    print(f"  出場理由: {trade['exit_reason']}")

# 策略有效性分析
print("\n=== 策略有效性分析 ===")

# 1. SPRING策略分析
spring_trades = [t for t in paired_trades if 'SPRING' in t['entry_reason']]
if spring_trades:
    print("1. SPRING策略:")
    for t in spring_trades:
        print(f"  - 持續時間: {t['duration_minutes']:.1f}分鐘, PnL: {t['pnl_pts']:.1f}點")
    avg_duration = sum(t['duration_minutes'] for t in spring_trades) / len(spring_trades)
    avg_pnl = sum(t['pnl_pts'] for t in spring_trades) / len(spring_trades)
    print(f"  平均持續時間: {avg_duration:.1f}分鐘")
    print(f"  平均PnL: {avg_pnl:.1f}點")

# 2. COUNTER_VWAP策略分析
counter_trades = [t for t in paired_trades if 'COUNTER_VWAP' in t['entry_reason']]
if counter_trades:
    print("\n2. COUNTER_VWAP策略:")
    for t in counter_trades:
        print(f"  - 持續時間: {t['duration_minutes']:.1f}分鐘, PnL: {t['pnl_pts']:.1f}點")
    avg_duration = sum(t['duration_minutes'] for t in counter_trades) / len(counter_trades)
    avg_pnl = sum(t['pnl_pts'] for t in counter_trades) / len(counter_trades)
    print(f"  平均持續時間: {avg_duration:.1f}分鐘")
    print(f"  平均PnL: {avg_pnl:.1f}點")

# 3. 市場時段分析
print("\n=== 市場時段分析 ===")
for trade in paired_trades:
    hour = trade['entry_time'].hour
    if 20 <= hour < 22:
        session = "夜盤前半段"
    elif 22 <= hour < 24:
        session = "夜盤中段"
    elif 0 <= hour < 2:
        session = "夜盤後半段"
    elif 2 <= hour < 5:
        session = "夜盤尾聲"
    else:
        session = "其他時段"
    print(f"交易 {trade['entry_time'].strftime('%H:%M')}: {session}, PnL: {trade['pnl_pts']:.1f}點")

# 4. 風險回報分析
print("\n=== 風險回報分析 ===")
total_pnl_pts = sum(t['pnl_pts'] for t in paired_trades)
total_pnl_cash = sum(t['pnl_cash'] for t in paired_trades)
max_drawdown = min(t['pnl_pts'] for t in paired_trades)
winning_trades = [t for t in paired_trades if t['pnl_pts'] > 0]
win_rate = len(winning_trades) / len(paired_trades) if paired_trades else 0

print(f"總PnL點數: {total_pnl_pts:.1f}")
print(f"總PnL現金: {total_pnl_cash:.1f}")
print(f"最大虧損: {max_drawdown:.1f}點")
print(f"勝率: {win_rate:.2%}")
print(f"平均每筆交易虧損: {total_pnl_pts/len(paired_trades):.1f}點")

# 5. 策略問題識別
print("\n=== 策略問題識別 ===")
issues = []

if total_pnl_pts < 0:
    issues.append("總體虧損，策略需要調整")

if win_rate == 0:
    issues.append("勝率為0%，進場時機或出場策略有問題")

if any(t['duration_minutes'] < 5 for t in paired_trades):
    issues.append("有交易持續時間過短（<5分鐘），可能是過度交易")

if any(t['pnl_pts'] < -50 for t in paired_trades):
    issues.append("有單筆虧損過大（>50點），風險控制需要加強")

if issues:
    print("發現的問題:")
    for i, issue in enumerate(issues, 1):
        print(f"{i}. {issue}")
else:
    print("未發現明顯問題")

print("\n=== 分析完成 ===")
