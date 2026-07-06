#!/usr/bin/env python3
"""
即時夜盤交易觀察工具
即時顯示：價格、信號分數、持倉狀態、進出場記錄
每 10 秒刷新一次

Usage:
    python scripts/monitor_night_live.py          # 持續觀察
    python scripts/monitor_night_live.py --lines 20  # 只看最近 20 行
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import time
import json
from pathlib import Path
from datetime import datetime

LOG_FILE = Path(__file__).parent.parent / "logs" / "unified.log"
TRADES_DIR = Path(__file__).parent.parent / "exports" / "trades"

def get_latest_price():
    """從日誌最後幾行提取最新 MTX 價格"""
    try:
        with open(LOG_FILE, 'r') as f:
            lines = f.readlines()[-50:]
        for line in reversed(lines):
            if "MTX updated:" in line:
                parts = line.strip().split("✅ MTX updated: ")
                if len(parts) > 1:
                    return float(parts[-1].strip())
    except:
        pass
    return None

def get_latest_signal():
    """提取最新的 PAPER 信號"""
    try:
        with open(LOG_FILE, 'r') as f:
            lines = f.readlines()[-30:]
        for line in reversed(lines):
            if "[PAPER] mode=V2" in line:
                return line.strip()
    except:
        pass
    return None

def get_new_bars():
    """提取最新的 K 棒"""
    try:
        with open(LOG_FILE, 'r') as f:
            lines = f.readlines()[-30:]
        bars = []
        for line in reversed(lines):
            if "[FuturesMonitor] New Bar:" in line:
                bars.append(line.strip())
        return bars[:3]
    except:
        return []

def get_todays_trades():
    """取得今日交易記錄"""
    today = datetime.now().strftime("%Y%m%d")
    trade_file = TRADES_DIR / f"TMF_{today}_trades.json"
    if trade_file.exists():
        with open(trade_file) as f:
            return json.load(f)
    return []

def get_session():
    """判斷當前 session"""
    h = datetime.now().hour
    return "🌙 夜盤" if (h >= 15 or h < 5) else "☀️ 日盤"

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--lines", type=int, default=0, help="顯示最近 N 行日誌")
    args = parser.parse_args()

    print("=" * 70)
    print("🌙 即時夜盤交易觀察")
    print(f"   設定: futures_night.yaml | counter_vwap | confirm=3 | atr_sl=1.5x")
    print(f"   開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    last_bar_ts = ""

    try:
        while True:
            os.system('clear')
            print("=" * 70)
            print(f"🌙 夜盤即時監控  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print("=" * 70)

            # 價格
            price = get_latest_price()
            if price:
                print(f"\n💰 最新價格: {price:,.0f}")

            # 信號
            signal = get_latest_signal()
            if signal:
                # Parse signal info
                if "position=flat" in signal:
                    print(f"📊 持倉: 空倉")
                elif "position" in signal:
                    # Extract position info
                    pos_start = signal.find("position=")
                    if pos_start >= 0:
                        pos_info = signal[pos_start:].split()[0]
                        print(f"📊 {pos_info}")

                if "signal=" in signal:
                    sig_start = signal.find("signal=")
                    sig_info = signal[sig_start:].split()[0]
                    print(f"📈 {sig_info}")

                # Color code based on score
                if "score=" in signal:
                    score_start = signal.find("score=")
                    score_str = signal[score_start+6:].split()[0]
                    try:
                        score = float(score_str)
                        if score > 50:
                            print(f"   🔴 強烈空頭信號 (score={score})")
                        elif score < -50:
                            print(f"   🟢 強烈多頭信號 (score={score})")
                        else:
                            print(f"   🟡 中性/觀望 (score={score})")
                    except:
                        pass

            # 新 K 棒
            bars = get_new_bars()
            if bars:
                print(f"\n🕒 最近 K 棒:")
                for bar in bars:
                    print(f"   {bar}")

            # 今日交易
            trades = get_todays_trades()
            if trades:
                print(f"\n📋 今日交易 ({len(trades)} 筆):")
                for t in trades:
                    t_type = t.get("type", "?")
                    t_reason = t.get("reason", "?")
                    t_price = t.get("price", 0)
                    t_pnl = t.get("pnl_cash", 0)
                    if t_type == "EXIT":
                        print(f"   {t_type} @ {t_price:,.0f} | PnL: {t_pnl:+,.0f} | {t_reason}")
                    elif t_type in ("BUY", "SELL"):
                        print(f"   {t_type} @ {t_price:,.0f} | {t_reason}")

            # 最近日誌
            if args.lines > 0:
                print(f"\n📜 最近 {args.lines} 行日誌:")
                with open(LOG_FILE, 'r') as f:
                    lines = f.readlines()[-args.lines:]
                    for line in lines:
                        if "MTX updated" not in line and "📡" not in line and "📥" not in line:
                            print(f"   {line.strip()}")

            print(f"\n⏳ 下次更新: 10 秒後...")
            time.sleep(10)

    except KeyboardInterrupt:
        print("\n\n👋 觀察結束")

if __name__ == "__main__":
    main()
