#!/usr/bin/env python3
"""
weak_bear_trend Paper Trading Monitor
監控夜盤實時表現

Usage:
  python3 scripts/monitor_weak_bear_paper.py
"""
import os
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

LOG_DIR = Path("~/Documents/mylin102/tw-trading-unified/logs").expanduser()
PAPER_TRADING_LOG = LOG_DIR / "paper_trading_weak_bear.jsonl"


def parse_log_line(line):
    """解析日誌行."""
    try:
        return json.loads(line)
    except:
        return None


def monitor_paper_trading():
    """監控 paper trading 表現."""
    print("="*60)
    print("weak_bear_trend Paper Trading 監控")
    print("="*60)
    print(f"監控時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"日誌文件：{PAPER_TRADING_LOG}")
    print("="*60)
    
    if not PAPER_TRADING_LOG.exists():
        print("\n⚠️  尚未找到交易日誌，等待夜盤開盤...")
        print("\n提示:")
        print("  1. 確保 monitor.py 已啟動")
        print("  2. 配置文件：config/futures_night_weak_bear.yaml")
        print("  3. 夜盤開盤時間：18:00 (台指期)")
        return
    
    # 讀取日誌
    trades = []
    signals = []
    skips = []
    
    with open(PAPER_TRADING_LOG, 'r') as f:
        for line in f:
            data = parse_log_line(line)
            if not data:
                continue
            
            event_type = data.get("event", "")
            
            if event_type == "SIGNAL":
                signals.append(data)
            elif event_type == "TRADE":
                trades.append(data)
            elif event_type == "SKIP":
                skips.append(data)
    
    print(f"\n📊 統計摘要")
    print(f"  信號數量：{len(signals)}")
    print(f"  交易數量：{len(trades)}")
    print(f"  跳過次數：{len(skips)}")
    
    if trades:
        print(f"\n💼 交易記錄")
        print("-"*60)
        
        total_pnl = 0
        winning = 0
        losing = 0
        
        for i, trade in enumerate(trades[-10:], 1):  # 最後 10 筆
            entry_price = trade.get("entry_price", 0)
            exit_price = trade.get("exit_price", 0)
            side = trade.get("side", "SELL")
            pnl = trade.get("pnl", 0)
            entry_time = trade.get("entry_time", "")
            exit_time = trade.get("exit_time", "")
            exit_reason = trade.get("exit_reason", "")
            
            total_pnl += pnl
            if pnl > 0:
                winning += 1
            else:
                losing += 1
            
            color = "🟢" if pnl > 0 else "🔴"
            print(f"  {i}. {color} {entry_time} → {exit_time}")
            print(f"     {side} @ {entry_price:.0f} → {exit_price:.0f} ({exit_reason})")
            print(f"     PnL: {pnl:+,.0f}")
            print()
        
        # 總結
        print("-"*60)
        print(f"📈 績效總結")
        print(f"  總交易：{len(trades)}")
        print(f"  獲利：{winning} ({winning/len(trades)*100:.1f}%)")
        print(f"  虧損：{losing} ({losing/len(trades)*100:.1f}%)")
        print(f"  總 PnL: {total_pnl:+,.0f} 點")
        print(f"  平均 PnL: {total_pnl/len(trades):+.1f} 點/交易")
        
        if total_pnl > 0:
            print(f"\n✅ 今晚表現良好！")
        else:
            print(f"\n⚠️  今晚表現欠佳，需要檢討")
    
    if signals and not trades:
        print(f"\n📡 最新信號")
        print("-"*60)
        for sig in signals[-5:]:
            ts = sig.get("timestamp", "")
            action = sig.get("action", "")
            price = sig.get("price", 0)
            reason = sig.get("reason", "")
            print(f"  {ts} | {action} @ {price:.0f} | {reason}")
    
    # Regime 分析
    print(f"\n🔍 Regime 分析")
    print("-"*60)
    
    if skips:
        skip_reasons = {}
        for skip in skips:
            reason = skip.get("reason", "UNKNOWN")
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        
        print(f"  跳過原因分佈:")
        for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1])[:10]:
            print(f"    {reason}: {count} 次")
    
    print("\n" + "="*60)
    print("下次更新：30 秒後 (Ctrl+C 退出)")
    print("="*60)


def tail_log():
    """實時追蹤日誌 (類似 tail -f)."""
    print("="*60)
    print("weak_bear_trend 實時日誌追蹤")
    print("="*60)
    
    if not PAPER_TRADING_LOG.exists():
        print(f"\n等待日誌文件創建：{PAPER_TRADING_LOG}")
        print("請確保 monitor.py 已啟動...")
        import time
        while not PAPER_TRADING_LOG.exists():
            time.sleep(5)
            print(".", end="", flush=True)
        print("\n日誌文件已創建！\n")
    
    print(f"監控：{PAPER_TRADING_LOG}\n")
    
    import time
    
    with open(PAPER_TRADING_LOG, 'r') as f:
        # 移到文件末尾
        f.seek(0, 2)
        
        while True:
            line = f.readline()
            if not line:
                time.sleep(1)
                continue
            
            data = parse_log_line(line)
            if data:
                event = data.get("event", "")
                ts = data.get("timestamp", datetime.now().isoformat())
                
                if event == "SIGNAL":
                    action = data.get("action", "")
                    price = data.get("price", 0)
                    reason = data.get("reason", "")
                    print(f"\n📡 [{ts}] SIGNAL: {action} @ {price:.0f} - {reason}")
                
                elif event == "TRADE":
                    side = data.get("side", "")
                    entry = data.get("entry_price", 0)
                    exit_p = data.get("exit_price", 0)
                    pnl = data.get("pnl", 0)
                    exit_reason = data.get("exit_reason", "")
                    color = "🟢" if pnl > 0 else "🔴"
                    print(f"\n💼 [{ts}] TRADE: {color} {side} {entry:.0f}→{exit_p:.0f} ({exit_reason}) PnL={pnl:+,.0f}")
                
                elif event == "SKIP":
                    reason = data.get("reason", "")
                    # 只顯示重要跳過原因
                    if "REGIME" in reason or "BIAS" in reason:
                        print(f"  ⚠️  [{ts}] SKIP: {reason}")
                
                elif event == "REGIME":
                    regime = data.get("regime", "")
                    bias = data.get("bias", "")
                    print(f"  📊 [{ts}] Regime: {regime}, Bias: {bias}")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="weak_bear_trend Paper Trading Monitor")
    parser.add_argument("--live", action="store_true", help="實時追蹤模式 (tail -f)")
    parser.add_argument("--summary", action="store_true", help="只显示摘要 (預設)")
    
    args = parser.parse_args()
    
    if args.live:
        tail_log()
    else:
        monitor_paper_trading()


if __name__ == "__main__":
    main()
