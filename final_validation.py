#!/usr/bin/env python3
"""
最終系統驗證腳本
驗證所有修復是否正確實施
"""
import sys
import os
import glob
import pandas as pd
import datetime

sys.path.append('.')

def test_1_date_logic():
    """測試日期邏輯"""
    print("=== 測試1: 日期邏輯 ===")
    from core.date_utils import get_trade_day, get_session_date_str
    
    now = datetime.datetime.now()
    trade_day = get_trade_day(now).strftime("%Y%m%d")
    session_date_str = get_session_date_str(now)
    
    print(f"當前時間: {now}")
    print(f"get_trade_day: {trade_day}")
    print(f"get_session_date_str: {session_date_str}")
    
    # 驗證文件命名一致性（支持 fallback 至 logs/market_data）
    expected_file = f"./exports/trades/TMF_{trade_day}_trades.csv"
    market_matches = glob.glob(f"./logs/market_data/TMF_{trade_day}*trades.csv")
    exists = os.path.exists(expected_file) or len(market_matches) > 0
    print(f"預期交易文件: {expected_file}")
    print(f"文件存在 (exports or market_data): {exists}")
    
    return trade_day, session_date_str

def test_2_dashboard_variables():
    """測試Dashboard變數"""
    print("\n=== 測試2: Dashboard變數 ===")
    from ui.dashboard import TRADE_DATE_STR, DATE_STR
    
    print(f"TRADE_DATE_STR: {TRADE_DATE_STR}")
    print(f"DATE_STR: {DATE_STR}")
    
    # 驗證Dashboard能找到文件（exports 或 logs/market_data）
    for date_str in [TRADE_DATE_STR, DATE_STR]:
        file_path = f"./exports/trades/TMF_{date_str}_trades.csv"
        market_matches = glob.glob(f"./logs/market_data/TMF_{date_str}*trades.csv")
        exists = os.path.exists(file_path) or len(market_matches) > 0
        print(f"檢查文件 TMF_{date_str}_trades.csv: {exists} (exports or market_data)")
    
    return TRADE_DATE_STR

def test_3_futures_trades():
    """測試期貨交易記錄"""
    print("\n=== 測試3: 期貨交易記錄 ===")
    
    # 使用Dashboard的加載邏輯
    from ui.dashboard import load_futures_trades
    ft, actual_date = load_futures_trades()
    
    if ft is not None:
        print(f"找到 {len(ft)} 筆交易記錄 (日期: {actual_date})")
        print("交易記錄摘要:")
        if not ft.empty and 'timestamp' in ft.columns:
            print(f"  時間範圍: {ft['timestamp'].min()} 到 {ft['timestamp'].max()}")
        if not ft.empty and 'type' in ft.columns:
            print(f"  交易類型: {ft['type'].unique().tolist()}")
        if not ft.empty and 'pnl_pts' in ft.columns:
            print(f"  總PnL點數: {ft['pnl_pts'].sum():.1f}")
        if not ft.empty and 'pnl_cash' in ft.columns:
            print(f"  總PnL現金: {ft['pnl_cash'].sum():.1f}")
        
        # 檢查是否有今天的交易
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        if not ft.empty and 'timestamp' in ft.columns:
            today_trades = ft[ft['timestamp'].astype(str).str.contains(today)]
            print(f"  今天({today})的交易: {len(today_trades)}筆")
        
        return len(ft)
    else:
        print("未找到交易記錄")
        return 0

def test_4_options_trades():
    """測試期權交易記錄"""
    print("\n=== 測試4: 期權交易記錄 ===")
    
    csv_path = "./strategies/options/logs/paper_trading/options_trade_ledger.csv"
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
        print(f"期權交易記錄: {len(df)} 筆")
        
        # 檢查THETA記錄
        theta_entries = df[df['Note'].str.contains('THETA_ENTRY', na=False)]
        theta_exits = df[df['Note'].str.contains('THETA_EXIT', na=False)]
        
        print(f"  THETA_ENTRY: {len(theta_entries)} 筆")
        print(f"  THETA_EXIT: {len(theta_exits)} 筆")
        
        # 檢查價格修復
        zero_price_theta = theta_entries[theta_entries['Price'] == 0]
        print(f"  價格為0的THETA_ENTRY: {len(zero_price_theta)} 筆 (應為0)")
        
        return len(df)
    else:
        print("期權交易記錄文件不存在")
        return 0

def test_5_data_storage_optimization():
    """測試數據儲存優化"""
    print("\n=== 測試5: 數據儲存優化 ===")
    
    try:
        from strategies.futures.squeeze_futures.data.data_storage import DataStorage
        
        storage = DataStorage("TMF")
        print("DataStorage 初始化成功")
        print(f"  緩衝區大小: {storage.buffer_size}")
        print(f"  刷新間隔: {storage.flush_interval}秒")
        
        # 測試緩衝寫入
        test_trade = {
            'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'type': 'TEST',
            'direction': 'LONG',
            'price': 10000,
            'lots': 1,
            'pnl_pts': 0,
            'pnl_cash': 0,
            'reason': 'VALIDATION_TEST',
            'cross_policy': {}
        }
        
        storage.save_trade(test_trade)
        print(f"  測試交易已添加到緩衝區，當前大小: {len(storage.trade_buffer)}")
        
        return True
    except Exception as e:
        print(f"測試失敗: {e}")
        return False

def main():
    """主測試函數"""
    print("台灣期權期貨交易系統最終驗證")
    print("=" * 50)
    
    results = {}
    
    # 執行所有測試
    results['date_logic'] = test_1_date_logic()
    results['dashboard_vars'] = test_2_dashboard_variables()
    results['futures_count'] = test_3_futures_trades()
    results['options_count'] = test_4_options_trades()
    results['storage_optimized'] = test_5_data_storage_optimization()
    
    # 總結
    print("\n" + "=" * 50)
    print("驗證總結:")
    
    # 檢查關鍵問題
    trade_day, session_date_str = results['date_logic']
    if trade_day != session_date_str:
        print(f"⚠️  注意: get_trade_day({trade_day}) != get_session_date_str({session_date_str})")
        print("  這是預期行為，因為夜盤時段的日期歸屬邏輯不同")
    else:
        print("✅ 日期邏輯一致")
    
    # 檢查交易記錄
    if results['futures_count'] > 0:
        print(f"✅ 找到 {results['futures_count']} 筆期貨交易記錄")
    else:
        print("❌ 未找到期貨交易記錄")
    
    if results['options_count'] > 0:
        print(f"✅ 找到 {results['options_count']} 筆期權交易記錄")
    else:
        print("❌ 未找到期權交易記錄")
    
    if results['storage_optimized']:
        print("✅ 數據儲存優化已實施")
    else:
        print("❌ 數據儲存優化測試失敗")
    
    # 檢查Dashboard變數
    trade_date_str = results['dashboard_vars']
    expected_file = f"./exports/trades/TMF_{trade_date_str}_trades.csv"
    if os.path.exists(expected_file):
        print(f"✅ Dashboard可以找到交易文件: {os.path.basename(expected_file)}")
    else:
        print(f"❌ Dashboard找不到交易文件: TMF_{trade_date_str}_trades.csv")
    
    print("\n驗證完成!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
