#!/usr/bin/env python3
"""
補足近期缺失資料 (2026-03-27 到 2026-04-12)
如果Shioaji無法取得資料，嘗試其他來源
"""

import sys
import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
import time

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

DATA_FILE = project_root / "data" / "tmf_full_2026.csv"

def check_current_data():
    """檢查目前的資料狀況"""
    print("=== 資料狀況檢查 ===")
    
    if not DATA_FILE.exists():
        print("❌ 資料檔案不存在")
        return None
    
    df = pd.read_csv(DATA_FILE, parse_dates=['timestamp'])
    df = df.sort_values('timestamp')
    
    print(f"📊 目前資料:")
    print(f"  總筆數: {len(df):,}")
    print(f"  時間範圍: {df['timestamp'].min()} 到 {df['timestamp'].max()}")
    print(f"  最新日期: {df['timestamp'].max().date()}")
    print(f"  今天日期: {datetime.now().date()}")
    
    # 檢查缺口
    last_date = df['timestamp'].max()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    if last_date.date() < today.date():
        missing_days = (today.date() - last_date.date()).days
        print(f"⚠️  缺少資料: {missing_days} 天 (從 {last_date.date()} 到 {today.date()})")
        return df, last_date, today
    else:
        print("✅ 資料是最新的")
        return df, None, None

def try_shioaji_backfill(start_date, end_date):
    """嘗試使用Shioaji API補足資料"""
    print(f"\n=== 嘗試Shioaji API補足資料 ({start_date.date()} 到 {end_date.date()}) ===")
    
    try:
        from core.shioaji_session import get_api
        
        api = get_api()
        if not api:
            print("❌ 無法取得Shioaji API連線")
            return None
        
        # 取得合約
        try:
            contract = api.Contracts.Futures.TXF.TXFR1  # 主合約
        except:
            # 嘗試其他合約
            contract = api.Contracts.Futures.TXF.TXF  # 標準合約
        
        print(f"✅ 連線成功，使用合約: {contract.code}")
        
        # 分段取得資料（避免一次取得太多）
        all_data = []
        current_start = start_date
        
        while current_start <= end_date:
            current_end = min(current_start + timedelta(days=7), end_date)
            
            start_str = current_start.strftime("%Y-%m-%d")
            end_str = current_end.strftime("%Y-%m-%d")
            
            print(f"📥 取得 {start_str} 到 {end_str} 的資料...")
            
            try:
                kbars = api.kbars(contract, start=start_str, end=end_str)
                if kbars:
                    df_new = pd.DataFrame({**kbars})
                    df_new.ts = pd.to_datetime(df_new.ts)
                    df_new = df_new.set_index("ts")
                    
                    # 重新命名欄位
                    df_new = df_new.rename(columns={
                        "Open": "Open", "High": "High", "Low": "Low", 
                        "Close": "Close", "Volume": "Volume"
                    })[['Open', 'High', 'Low', 'Close', 'Volume']]
                    
                    df_new.index.name = "timestamp"
                    all_data.append(df_new)
                    print(f"   ✅ 取得 {len(df_new)} 筆資料")
                else:
                    print(f"   ⚠️  沒有資料")
                
            except Exception as e:
                print(f"   ❌ 取得資料錯誤: {e}")
            
            # 避免API限制
            time.sleep(2)
            current_start = current_end + timedelta(days=1)
        
        if all_data:
            combined = pd.concat(all_data)
            print(f"✅ 總共取得 {len(combined)} 筆資料")
            return combined
        else:
            print("❌ 沒有取得任何資料")
            return None
            
    except ImportError:
        print("❌ 無法匯入Shioaji模組")
        return None
    except Exception as e:
        print(f"❌ Shioaji API錯誤: {e}")
        return None

def try_alternative_sources(start_date, end_date):
    """嘗試其他資料來源"""
    print(f"\n=== 嘗試其他資料來源 ===")
    
    # 方法1: 檢查是否有其他資料檔案
    print("1. 檢查其他資料檔案...")
    
    data_dir = project_root / "data"
    alternative_files = []
    
    for file in data_dir.glob("*.csv"):
        if file.name != "tmf_full_2026.csv":
            alternative_files.append(file)
    
    if alternative_files:
        print(f"   找到 {len(alternative_files)} 個替代檔案:")
        for file in alternative_files:
            print(f"   - {file.name}")
        
        # 嘗試從tmf_replay_5min_q1_2026.csv取得資料
        replay_file = data_dir / "tmf_replay_5min_q1_2026.csv"
        if replay_file.exists():
            print(f"   嘗試從 {replay_file.name} 取得資料...")
            try:
                df_replay = pd.read_csv(replay_file, parse_dates=['timestamp'])
                # 篩選需要的日期範圍
                mask = (df_replay['timestamp'] >= start_date) & (df_replay['timestamp'] <= end_date)
                df_filtered = df_replay[mask]
                
                if len(df_filtered) > 0:
                    print(f"   ✅ 從回放檔案取得 {len(df_filtered)} 筆資料")
                    df_filtered = df_filtered.set_index('timestamp')
                    df_filtered.index.name = 'timestamp'
                    return df_filtered[['Open', 'High', 'Low', 'Close', 'Volume']]
                else:
                    print("   ⚠️  回放檔案沒有指定時間範圍的資料")
            except Exception as e:
                print(f"   ❌ 讀取回放檔案錯誤: {e}")
    
    # 方法2: 檢查historical資料夾
    print("2. 檢查historical資料夾...")
    historical_dir = data_dir / "historical"
    if historical_dir.exists():
        historical_files = list(historical_dir.glob("*.csv"))
        if historical_files:
            print(f"   找到 {len(historical_files)} 個歷史檔案")
            # 這裡可以實作合併歷史檔案的邏輯
        else:
            print("   historical資料夾是空的")
    else:
        print("   historical資料夾不存在")
    
    # 方法3: 生成模擬資料（最後手段）
    print("3. 生成模擬資料（最後手段）...")
    print("   ⚠️  注意：模擬資料僅供測試使用，不適合實際交易")
    
    # 計算需要多少5分鐘K棒
    trading_hours_per_day = 12  # 小時 (8:45-13:45, 15:00-05:00)
    bars_per_day = trading_hours_per_day * 12  # 每小時12根5分鐘K棒
    
    days_needed = (end_date - start_date).days + 1
    total_bars_needed = days_needed * bars_per_day
    
    print(f"   需要生成 {total_bars_needed} 根K棒 ({days_needed} 天)")
    
    # 詢問使用者是否要生成模擬資料
    response = input("   是否要生成模擬資料？ (y/n): ")
    if response.lower() == 'y':
        return generate_simulated_data(start_date, end_date)
    else:
        print("   跳過模擬資料生成")
        return None

def generate_simulated_data(start_date, end_date):
    """生成模擬資料"""
    print("   生成模擬資料中...")
    
    # 生成時間序列
    dates = []
    current = start_date.replace(hour=15, minute=0, second=0)  # 從夜盤開始
    
    while current <= end_date:
        # 夜盤: 15:00-05:00
        night_start = current.replace(hour=15, minute=0, second=0)
        night_end = current + timedelta(days=1)
        night_end = night_end.replace(hour=5, minute=0, second=0)
        
        # 日盤: 08:45-13:45
        day_start = current.replace(hour=8, minute=45, second=0)
        day_end = current.replace(hour=13, minute=45, second=0)
        
        # 生成夜盤時間點
        temp = night_start
        while temp < night_end:
            dates.append(temp)
            temp += timedelta(minutes=5)
        
        # 生成日盤時間點
        temp = day_start
        while temp < day_end:
            dates.append(temp)
            temp += timedelta(minutes=5)
        
        current += timedelta(days=1)
    
    # 過濾掉非交易時間（週日等）
    from core.data_sentinel import data_sentinel
    dates = [d for d in dates if data_sentinel.is_market_open(d)]
    
    # 生成價格資料（基於最後已知價格）
    if DATA_FILE.exists():
        df_old = pd.read_csv(DATA_FILE, parse_dates=['timestamp'])
        last_price = df_old['Close'].iloc[-1]
    else:
        last_price = 33000  # 預設價格
    
    # 生成隨機走勢
    np.random.seed(42)  # 固定隨機種子以便重現
    returns = np.random.normal(0.0001, 0.002, len(dates))  # 微小正報酬，低波動
    
    prices = [last_price]
    for r in returns:
        prices.append(prices[-1] * (1 + r))
    prices = prices[1:]  # 移除初始值
    
    # 生成OHLCV資料
    data = []
    for i, (dt, close) in enumerate(zip(dates, prices)):
        # 在收盤價附近生成OHL
        spread = close * 0.0005  # 0.05% 價差
        high = close + abs(np.random.normal(0, spread))
        low = close - abs(np.random.normal(0, spread))
        open_price = np.random.uniform(low, high)
        
        # 確保OHLC順序正確
        prices_sorted = sorted([open_price, high, low, close])
        open_price, high, low, close = prices_sorted
        
        # 成交量（隨機）
        volume = int(np.random.uniform(500, 5000))
        
        data.append({
            'timestamp': dt,
            'Open': open_price,
            'High': high,
            'Low': low,
            'Close': close,
            'Volume': volume
        })
    
    df_sim = pd.DataFrame(data)
    df_sim = df_sim.set_index('timestamp')
    df_sim.index.name = 'timestamp'
    
    print(f"   ✅ 生成 {len(df_sim)} 筆模擬資料")
    print(f"   ⚠️  警告：這是模擬資料，不應用於實際交易決策")
    
    return df_sim

def merge_and_save(df_existing, df_new):
    """合併並儲存資料"""
    print(f"\n=== 合併資料 ===")
    
    if df_new is None or len(df_new) == 0:
        print("❌ 沒有新資料可以合併")
        return False
    
    # 合併資料
    combined = pd.concat([df_existing.set_index('timestamp'), df_new])
    
    # 移除重複的時間戳
    combined = combined[~combined.index.duplicated(keep='first')]
    combined = combined.sort_index()
    
    # 儲存回CSV
    combined.to_csv(DATA_FILE)
    
    print(f"✅ 資料合併完成")
    print(f"   合併前筆數: {len(df_existing):,}")
    print(f"   新增筆數: {len(df_new):,}")
    print(f"   合併後筆數: {len(combined):,}")
    print(f"   時間範圍: {combined.index.min()} 到 {combined.index.max()}")
    
    return True

def main():
    """主程式"""
    print("=" * 60)
    print("台灣期貨交易系統 - 資料補足工具")
    print("=" * 60)
    
    # 檢查目前資料
    result = check_current_data()
    if result is None:
        return
    
    df_existing, last_date, today = result
    
    if last_date is None:
        print("\n✅ 資料已經是最新的，不需要補足")
        return
    
    # 計算需要補足的日期範圍
    # 從最後一筆資料的下一個5分鐘開始
    start_date = last_date + timedelta(minutes=5)
    end_date = today.replace(hour=23, minute=59, second=59)
    
    print(f"\n需要補足的日期範圍: {start_date} 到 {end_date}")
    
    # 嘗試Shioaji API
    df_shioaji = try_shioaji_backfill(start_date, end_date)
    
    if df_shioaji is not None and len(df_shioaji) > 0:
        # 使用Shioaji資料
        success = merge_and_save(df_existing, df_shioaji)
        if success:
            print("\n🎉 使用Shioaji API成功補足資料")
            return
    else:
        print("\n⚠️  Shioaji API無法取得資料，嘗試其他來源...")
    
    # 嘗試其他來源
    df_alternative = try_alternative_sources(start_date, end_date)
    
    if df_alternative is not None and len(df_alternative) > 0:
        success = merge_and_save(df_existing, df_alternative)
        if success:
            print("\n🎉 使用替代來源成功補足資料")
            return
    
    print("\n❌ 無法補足資料")
    print("建議:")
    print("1. 檢查網路連線和Shioaji API設定")
    print("2. 手動下載資料並匯入")
    print("3. 使用模擬資料進行測試")

if __name__ == "__main__":
    main()