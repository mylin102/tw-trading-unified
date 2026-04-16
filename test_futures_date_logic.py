#!/usr/bin/env python3
"""
測試期貨交易日期邏輯
"""
import sys
import os
sys.path.append('.')
from core.date_utils import get_trading_day, get_trade_day, get_session_date_str
import datetime

def test_date_logic():
    """測試日期邏輯"""
    print("=== 日期邏輯測試 ===")
    
    # 測試時間點
    test_times = [
        "2026-04-15 20:23:12",  # 夜盤，應屬20260415
        "2026-04-16 00:47:20",  # 夜盤(00:00-05:00)，應屬20260415
        "2026-04-16 02:42:31",  # 夜盤(00:00-05:00)，應屬20260415
        "2026-04-16 08:50:00",  # 日盤，應屬20260416
        "2026-04-16 13:30:00",  # 日盤，應屬20260416
        "2026-04-16 15:30:00",  # 夜盤開始，應屬20260416
    ]
    
    for time_str in test_times:
        dt = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        
        trading_day = get_trading_day(dt)
        trade_day = get_trade_day(dt)
        session_date_str = get_session_date_str(dt)
        
        print(f"時間: {time_str}")
        print(f"  get_trading_day: {trading_day.strftime('%Y%m%d')}")
        print(f"  get_trade_day: {trade_day.strftime('%Y%m%d')}")
        print(f"  get_session_date_str: {session_date_str}")
        print()

def check_current_date():
    """檢查當前日期邏輯"""
    print("=== 當前日期檢查 ===")
    now = datetime.datetime.now()
    
    trading_day = get_trading_day(now)
    trade_day = get_trade_day(now)
    session_date_str = get_session_date_str(now)
    
    print(f"當前時間: {now}")
    print(f"  get_trading_day: {trading_day.strftime('%Y%m%d')}")
    print(f"  get_trade_day: {trade_day.strftime('%Y%m%d')}")
    print(f"  get_session_date_str: {session_date_str}")
    
    # 檢查文件命名
    print(f"\n預期的交易記錄文件名: TMF_{trade_day.strftime('%Y%m%d')}_trades.csv")
    print(f"Dashboard尋找的文件名: TMF_{session_date_str}_trades.csv")

def check_existing_files():
    """檢查現有文件"""
    print("\n=== 現有文件檢查 ===")
    
    import glob
    files = glob.glob("./exports/trades/TMF_*.csv")
    print("現有的TMF交易文件:")
    for f in sorted(files):
        print(f"  {os.path.basename(f)}")
    
    # 檢查今天的文件
    now = datetime.datetime.now()
    trade_day = get_trade_day(now).strftime("%Y%m%d")
    session_date_str = get_session_date_str(now)
    
    trade_day_file = f"./exports/trades/TMF_{trade_day}_trades.csv"
    session_file = f"./exports/trades/TMF_{session_date_str}_trades.csv"
    
    print(f"\n根據get_trade_day預期的文件: {trade_day_file}")
    print(f"  存在: {os.path.exists(trade_day_file)}")
    
    print(f"根據get_session_date_str預期的文件: {session_file}")
    print(f"  存在: {os.path.exists(session_file)}")

def main():
    test_date_logic()
    check_current_date()
    check_existing_files()
    
    # 總結問題
    print("\n=== 問題分析 ===")
    now = datetime.datetime.now()
    trade_day = get_trade_day(now).strftime("%Y%m%d")
    session_date_str = get_session_date_str(now)
    
    if trade_day != session_date_str:
        print(f"❌ 日期不一致問題發現!")
        print(f"  get_trade_day: {trade_day}")
        print(f"  get_session_date_str: {session_date_str}")
        print(f"  差異: 交易記錄使用 {trade_day}，但Dashboard尋找 {session_date_str}")
        return 1
    else:
        print("✅ 日期邏輯一致")
        return 0

if __name__ == "__main__":
    sys.exit(main())
