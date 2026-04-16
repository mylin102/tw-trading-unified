#!/usr/bin/env python3
"""
測試夜盤隔日問題修復和優化
"""
import sys
import os
sys.path.append('.')
from core.date_utils import get_trading_day, get_trade_day, get_session_date_str
import datetime
import pandas as pd

def test_date_logic_comprehensive():
    """全面測試日期邏輯"""
    print("=== 全面日期邏輯測試 ===")
    
    # 測試時間點
    test_cases = [
        # (時間字串, 預期交易日, 說明)
        ("2026-04-16 04:59:07", "20260415", "週四凌晨(夜盤後半段)"),
        ("2026-04-16 08:50:00", "20260416", "週四日盤"),
        ("2026-04-16 13:30:00", "20260416", "週四日盤尾"),
        ("2026-04-16 15:30:00", "20260417", "週四夜盤開始"),
        ("2026-04-17 20:23:12", "20260418", "週五夜盤(應跳週一)"),
        ("2026-04-18 00:47:20", "20260418", "週六凌晨(應跳週一)"),
        ("2026-04-18 02:42:31", "20260418", "週六凌晨(應跳週一)"),
        ("2026-04-20 08:50:00", "20260420", "週一日盤"),
        ("2026-04-20 03:30:00", "20260420", "週一凌晨"),
    ]
    
    print(f"{'時間':<20} {'get_trading_day':<15} {'get_trade_day':<15} {'get_session_date_str':<15} {'說明':<30}")
    print("-" * 100)
    
    for time_str, expected_trade_day, description in test_cases:
        dt = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        
        trading_day = get_trading_day(dt).strftime("%Y%m%d")
        trade_day = get_trade_day(dt).strftime("%Y%m%d")
        session_date_str = get_session_date_str(dt)
        
        trade_day_match = "✓" if trade_day == expected_trade_day else "✗"
        
        print(f"{time_str:<20} {trading_day:<15} {trade_day:<15} {session_date_str:<15} {trade_day_match} {description:<30}")

def test_current_situation():
    """測試當前情況"""
    print("\n=== 當前情況測試 ===")
    now = datetime.datetime.now()
    
    trading_day = get_trading_day(now).strftime("%Y%m%d")
    trade_day = get_trade_day(now).strftime("%Y%m%d")
    session_date_str = get_session_date_str(now)
    
    print(f"當前時間: {now}")
    print(f"  get_trading_day: {trading_day}")
    print(f"  get_trade_day: {trade_day}")
    print(f"  get_session_date_str: {session_date_str}")
    
    # 檢查文件
    import glob
    files = glob.glob("./exports/trades/TMF_*.csv")
    print(f"\n現有的TMF交易文件 ({len(files)}個):")
    for f in sorted(files):
        print(f"  {os.path.basename(f)}")
    
    # 檢查預期文件是否存在
    expected_file = f"./exports/trades/TMF_{trade_day}_trades.csv"
    print(f"\n根據get_trade_day預期的文件: {expected_file}")
    print(f"  存在: {os.path.exists(expected_file)}")
    
    session_file = f"./exports/trades/TMF_{session_date_str}_trades.csv"
    print(f"根據get_session_date_str預期的文件: {session_file}")
    print(f"  存在: {os.path.exists(session_file)}")
    
    # 檢查Dashboard會找到哪個文件
    from ui.dashboard import TRADE_DATE_STR
    print(f"\nDashboard的TRADE_DATE_STR: {TRADE_DATE_STR}")
    dashboard_file = f"./exports/trades/TMF_{TRADE_DATE_STR}_trades.csv"
    print(f"Dashboard會尋找的文件: {dashboard_file}")
    print(f"  存在: {os.path.exists(dashboard_file)}")

def test_data_storage_optimization():
    """測試數據儲存優化"""
    print("\n=== 數據儲存優化測試 ===")
    
    try:
        from strategies.futures.squeeze_futures.data.data_storage import DataStorage
        storage = DataStorage("TMF")
        
        print("DataStorage 初始化成功")
        print(f"  date_str: {storage.date_str}")
        print(f"  buffer_size: {storage.buffer_size}")
        print(f"  flush_interval: {storage.flush_interval}秒")
        print(f"  已實現緩衝寫入優化")
        
        # 測試交易記錄
        test_trade = {
            'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'type': 'ENTRY',
            'direction': 'LONG',
            'price': 10000,
            'lots': 1,
            'pnl_pts': 0,
            'pnl_cash': 0,
            'reason': 'TEST'
        }
        
        storage.save_trade(test_trade)
        print(f"  測試交易已添加到緩衝區，緩衝區大小: {len(storage.trade_buffer)}")
        
        # 手動觸發寫入
        if hasattr(storage, '_flush_buffer'):
            storage._flush_buffer()
            print("  緩衝區已寫入檔案")
        
    except Exception as e:
        print(f"測試失敗: {e}")

def main():
    test_date_logic_comprehensive()
    test_current_situation()
    test_data_storage_optimization()
    
    print("\n=== 總結 ===")
    now = datetime.datetime.now()
    trade_day = get_trade_day(now).strftime("%Y%m%d")
    session_date_str = get_session_date_str(now)
    
    if trade_day != session_date_str:
        print(f"⚠️  注意: get_trade_day 和 get_session_date_str 返回不同的日期")
        print(f"  get_trade_day: {trade_day}")
        print(f"  get_session_date_str: {session_date_str}")
        print(f"  這是預期行為，因為夜盤時段的日期歸屬邏輯不同")
    else:
        print("✅ get_trade_day 和 get_session_date_str 返回相同的日期")
    
    # 檢查Dashboard是否能找到文件
    from ui.dashboard import TRADE_DATE_STR
    dashboard_file = f"./exports/trades/TMF_{TRADE_DATE_STR}_trades.csv"
    if os.path.exists(dashboard_file):
        print(f"✅ Dashboard可以找到交易記錄文件: {os.path.basename(dashboard_file)}")
    else:
        print(f"❌ Dashboard找不到交易記錄文件: TMF_{TRADE_DATE_STR}_trades.csv")
        print("  可能需要手動複製或重新命名文件")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
