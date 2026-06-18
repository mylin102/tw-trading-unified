#!/usr/bin/env python3
"""
檢查資料連續性並補足今天(4月12日)的資料
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time
from pathlib import Path
import sys

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

DATA_FILE = project_root / "data" / "tmf_full_2026.csv"

def check_data_continuity():
    """檢查資料連續性"""
    print("=== 資料連續性檢查 ===")
    
    df = pd.read_csv(DATA_FILE, parse_dates=['timestamp'])
    df = df.sort_values('timestamp')
    
    # 載入市場時間檢查
    from core.data_sentinel import data_sentinel
    
    # 只檢查交易時間內的連續性
    trading_times = []
    for dt in df['timestamp']:
        if data_sentinel.is_market_open(dt):
            trading_times.append(dt)
    
    print(f"總筆數: {len(df):,}")
    print(f"交易時間筆數: {len(trading_times):,}")
    print(f"非交易時間筆數: {len(df) - len(trading_times):,}")
    
    # 檢查交易時間內的連續性
    if trading_times:
        trading_series = pd.Series(trading_times)
        time_diff = trading_series.diff().dt.total_seconds() / 60  # 轉換為分鐘
        
        # 找出不是5分鐘間隔的地方
        irregular_mask = (time_diff != 5) & (~time_diff.isna())
        irregular_count = irregular_mask.sum()
        
        print(f"\n交易時間內不連續處: {irregular_count}")
        
        if irregular_count > 0:
            print("不連續的時間點:")
            irregular_times = trading_series[irregular_mask]
            for i, dt in enumerate(irregular_times.head(10)):  # 只顯示前10個
                print(f"  {i+1}. {dt}")
            if len(irregular_times) > 10:
                print(f"  ... 還有 {len(irregular_times) - 10} 個")
    else:
        print("沒有交易時間資料")
    
    return df

def check_today_data():
    """檢查今天的資料"""
    print(f"\n=== 今天({datetime.now().strftime('%m月%d日')})資料檢查 ===")
    
    today = datetime.now().date()
    df = pd.read_csv(DATA_FILE, parse_dates=['timestamp'])
    
    # 篩選今天的資料
    today_data = df[df['timestamp'].dt.date == today]
    
    if len(today_data) > 0:
        print(f"✅ 今天已有 {len(today_data)} 筆資料")
        print(f"時間範圍: {today_data['timestamp'].min().time()} 到 {today_data['timestamp'].max().time()}")
        
        # 檢查是否完整
        from core.data_sentinel import data_sentinel
        
        # 生成今天預期的交易時間
        start_of_day = datetime.combine(today, time(0, 0))
        end_of_day = datetime.combine(today, time(23, 59, 59))
        
        expected_times = []
        current = start_of_day
        while current <= end_of_day:
            if data_sentinel.is_market_open(current):
                expected_times.append(current)
            current += timedelta(minutes=5)
        
        # 找出缺失的時間點
        actual_times = set(today_data['timestamp'])
        missing_times = [t for t in expected_times if t not in actual_times]
        
        if missing_times:
            print(f"⚠️  今天缺少 {len(missing_times)} 筆資料")
            print("缺失時間點範例:")
            for t in missing_times[:5]:
                print(f"  {t.time()}")
            if len(missing_times) > 5:
                print(f"  ... 還有 {len(missing_times) - 5} 個")
        else:
            print("✅ 今天的資料完整")
    else:
        print("❌ 今天還沒有資料")
    
    return today_data

def get_today_market_data():
    """取得今天的市場資料"""
    print("\n=== 取得今天市場資料 ===")
    
    today = datetime.now().date()
    
    try:
        from core.shioaji_session import get_api
        
        api = get_api()
        if not api:
            print("❌ 無法取得Shioaji API連線")
            return None
        
        # 2026-06-18 Gemini CLI: Resolve ticker dynamically from config to avoid hardcoded TXF
        from core.bar_utils import load_config
        futures_cfg = load_config("config/futures.yaml")
        product = futures_cfg.get("ticker", "TMF")
        
        # 取得合約
        try:
            contract = getattr(api.Contracts.Futures, product).TXFR1 if product == "TXF" else getattr(api.Contracts.Futures, product)[f"{product}R1"]
        except Exception:
            try:
                contract = getattr(api.Contracts.Futures, product)[product]
            except Exception:
                # Fallback to resolver
                from core.contract_resolver import ContractResolver
                resolver = ContractResolver(api)
                near, _ = resolver.get_near_far_contracts(product)
                contract = near
        
        if not contract:
            print(f"❌ 無法取得 {product} 合約")
            return None

        print(f"✅ 連線成功，使用合約: {contract.code}")
        
        # 取得今天資料
        today_str = today.strftime("%Y-%m-%d")
        print(f"📥 取得 {today_str} 的資料...")
        
        import time
        time.sleep(1)  # 避免API限制
        
        kbars = api.kbars(contract, start=today_str, end=today_str)
        
        # Check if kbars is empty by converting to DataFrame first
        try:
            df_new = pd.DataFrame({**kbars})
        except Exception:
            print("⚠️  API沒有返回今天的資料或資料格式錯誤")
            print("可能原因:")
            print("  1. 市場還沒開盤")
            print("  2. 今天是假日")
            print("  3. API資料延遲")
            return None
        
        if df_new.empty:
            print("⚠️  API返回空的資料")
            return None
        
        df_new.ts = pd.to_datetime(df_new.ts)
        df_new = df_new.set_index("ts")
        
        # 重新命名欄位
        df_new = df_new.rename(columns={
            "Open": "Open", "High": "High", "Low": "Low", 
            "Close": "Close", "Volume": "Volume"
        })[['Open', 'High', 'Low', 'Close', 'Volume']]
        
        df_new.index.name = "timestamp"
        
        print(f"✅ 取得 {len(df_new)} 筆今天資料")
        print(f"時間範圍: {df_new.index.min()} 到 {df_new.index.max()}")
        
        return df_new
        
    except Exception as e:
        print(f"❌ 取得今天資料錯誤: {e}")
        return None

def update_today_data():
    """更新今天的資料"""
    print("\n=== 更新今天資料 ===")
    
    # 讀取現有資料
    df_existing = pd.read_csv(DATA_FILE, parse_dates=['timestamp'])
    
    # 取得今天資料
    df_today = get_today_market_data()
    
    if df_today is None or len(df_today) == 0:
        print("⚠️  無法取得今天資料，使用現有資料")
        return df_existing
    
    # 移除現有資料中今天的部分（如果有）
    today = datetime.now().date()
    mask = df_existing['timestamp'].dt.date != today
    df_clean = df_existing[mask].copy()
    
    # 合併新資料
    df_today_reset = df_today.reset_index()
    df_combined = pd.concat([df_clean, df_today_reset], ignore_index=True)
    
    # 排序並移除重複
    df_combined = df_combined.sort_values('timestamp')
    df_combined = df_combined.drop_duplicates(subset=['timestamp'], keep='last')
    
    # 儲存
    df_combined.to_csv(DATA_FILE, index=False)
    
    print(f"✅ 資料更新完成")
    print(f"更新前筆數: {len(df_existing):,}")
    print(f"更新後筆數: {len(df_combined):,}")
    print(f"新增今天資料: {len(df_today):,} 筆")
    
    return df_combined

def main():
    """主程式"""
    print("=" * 60)
    print("資料連續性檢查與今天資料更新")
    print("=" * 60)
    
    # 檢查資料連續性
    df = check_data_continuity()
    
    # 檢查今天資料
    today_data = check_today_data()
    
    # 如果今天資料不完整，嘗試更新
    if len(today_data) == 0:
        print("\n嘗試更新今天資料...")
        df = update_today_data()
    else:
        # 檢查是否需要更新（可能只有部分資料）
        from core.data_sentinel import data_sentinel
        
        today = datetime.now().date()
        expected_count = 0
        current_time = datetime.now()
        
        # 計算到今天目前時間為止應該有多少交易時間的K棒
        start_of_day = datetime.combine(today, time(0, 0))
        temp = start_of_day
        while temp <= current_time:
            if data_sentinel.is_market_open(temp):
                expected_count += 1
            temp += timedelta(minutes=5)
        
        actual_count = len(today_data[today_data['timestamp'] <= current_time])
        
        print(f"\n今天資料完整性:")
        print(f"  預期筆數(到目前時間): {expected_count}")
        print(f"  實際筆數: {actual_count}")
        
        if actual_count < expected_count * 0.8:  # 如果少於80%
            print(f"⚠️  資料可能不完整，嘗試更新...")
            df = update_today_data()
        else:
            print("✅ 今天資料基本完整")
    
    # 最終檢查
    print("\n" + "=" * 60)
    print("最終資料狀態")
    print("=" * 60)
    
    df_final = pd.read_csv(DATA_FILE, parse_dates=['timestamp'])
    print(f"總筆數: {len(df_final):,}")
    print(f"時間範圍: {df_final['timestamp'].min()} 到 {df_final['timestamp'].max()}")
    
    # 檢查最後一天
    last_date = df_final['timestamp'].max().date()
    today = datetime.now().date()
    
    if last_date == today:
        print(f"✅ 資料已更新到今天 ({today})")
    elif last_date == today - timedelta(days=1):
        print(f"⚠️  資料更新到昨天 ({last_date})")
        print("這可能是正常的，因為今天市場可能還沒結束或資料有延遲")
    else:
        print(f"❌ 資料只到 {last_date}，落後 {today - last_date} 天")
    
    print("\n資料修復完成！系統已準備好進行交易。")

if __name__ == "__main__":
    main()