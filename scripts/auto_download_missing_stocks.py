#!/usr/bin/env python3
"""
自動下載缺失股票數據腳本
無需交互，直接下載所有缺失的股票數據
"""

import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
import yaml
import pandas as pd
from dotenv import load_dotenv
from core.broker.shioaji_compat import kbars_to_dataframe

# 添加項目根目錄到路徑
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# 加載環境變量
load_dotenv(ROOT / ".env")

# 數據目錄
DATA_DIR = ROOT / "data" / "taifex_raw"
DATA_DIR.mkdir(parents=True, exist_ok=True)

def check_missing_stocks():
    """檢查缺失的股票數據文件"""
    # 讀取監控名單
    config_path = ROOT / "config" / "stocks.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    
    watchlist = config.get('stocks', {}).get('watchlist', [])
    print(f"📋 監控名單: {len(watchlist)} 檔股票")
    print(f"📜 股票代碼: {watchlist}")
    
    # 檢查缺失的數據文件
    missing = []
    existing = []
    
    for ticker in watchlist:
        file_path = DATA_DIR / f"STOCK_{ticker}_5m.csv"
        if file_path.exists():
            # 檢查文件是否為空
            file_size = os.path.getsize(file_path)
            if file_size > 100:  # 大於100字節
                existing.append(ticker)
                # 檢查列名
                try:
                    with open(file_path, 'r') as f:
                        first_line = f.readline().strip()
                    if 'timestamp' not in first_line:
                        print(f"  ⚠ {ticker}: 數據文件列名不正確")
                except:
                    pass
            else:
                missing.append(ticker)
                print(f"  ⚠ {ticker}: 數據文件過小 ({file_size} bytes)")
        else:
            missing.append(ticker)
    
    print(f"\n📊 數據完整性:")
    print(f"  ✅ 已有數據: {len(existing)} 檔")
    print(f"  ❌ 缺失數據: {len(missing)} 檔")
    
    if missing:
        print(f"\n🔍 缺失股票: {missing}")
    
    return missing

def download_missing_stocks(missing_tickers, months=3):
    """下載缺失的股票數據"""
    try:
        import shioaji
    except ImportError:
        print("❌ 錯誤: 請先安裝 shioaji 套件")
        print("執行: pip install shioaji")
        return False
    
    # 初始化Shioaji API
    print("\n🔌 初始化Shioaji API...")
    api = shioaji.Shioaji()
    
    try:
        # 登入API
        api_key = os.getenv("SHIOAJI_API_KEY")
        secret_key = os.getenv("SHIOAJI_SECRET_KEY")
        
        if not api_key or not secret_key:
            print("❌ 錯誤: 請在.env文件中設置SHIOAJI_API_KEY和SHIOAJI_SECRET_KEY")
            return False
        
        print("🔐 登入Shioaji...")
        api.login(api_key=api_key, secret_key=secret_key)
        print("✅ 登入成功")
        
        # 計算開始日期
        start_date = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")
        print(f"📅 下載期間: {start_date} 至今")
        
        # 下載每檔缺失股票
        success_count = 0
        failed_tickers = []
        
        for i, ticker in enumerate(missing_tickers, 1):
            print(f"\n[{i}/{len(missing_tickers)}] 下載 {ticker}...")
            
            try:
                # 獲取合約
                print(f"  🔍 查找合約...")
                contract = api.Contracts.Stocks[ticker]
                if not contract:
                    print(f"  ❌ {ticker}: 合約不存在")
                    failed_tickers.append(ticker)
                    continue
                
                print(f"  ✅ 找到合約: {contract.name}")
                
                # 下載K線數據
                print(f"  📥 下載5分鐘K線數據...")
                kbars = api.kbars(contract, start=start_date)
                
                # 轉換為DataFrame (使用兼容性助手)
                df = kbars_to_dataframe(kbars)
                
                if df.empty:
                    print(f"  ⚠ {ticker}: 無數據返回")
                    failed_tickers.append(ticker)
                    continue
                
                print(f"  🔄 轉換數據格式...")
                # 將索引 ts 轉換為 timestamp 列
                df = df.reset_index().rename(columns={"ts": "timestamp"})
                df = df.sort_values("timestamp").reset_index(drop=True)
                
                # 保存到CSV
                output_file = DATA_DIR / f"STOCK_{ticker}_5m.csv"
                df.to_csv(output_file, index=False)
                
                # 檢查保存的文件
                if output_file.exists():
                    file_size = os.path.getsize(output_file)
                    row_count = len(df)
                    print(f"  ✅ {ticker}: 下載完成 ({row_count} 行, {file_size:,} bytes)")
                    success_count += 1
                    
                    # 顯示數據時間範圍
                    if row_count > 0:
                        start_time = df["timestamp"].min().strftime("%Y-%m-%d")
                        end_time = df["timestamp"].max().strftime("%Y-%m-%d")
                        print(f"    時間範圍: {start_time} 至 {end_time}")
                else:
                    print(f"  ❌ {ticker}: 文件保存失敗")
                    failed_tickers.append(ticker)
                
                # 避免請求過快
                time.sleep(2)
                
            except Exception as e:
                print(f"  ❌ {ticker}: 下載失敗 - {str(e)}")
                failed_tickers.append(ticker)
                continue
        
        print(f"\n📊 下載結果:")
        print(f"  ✅ 成功: {success_count}/{len(missing_tickers)}")
        print(f"  ❌ 失敗: {len(missing_tickers) - success_count}")
        
        if failed_tickers:
            print(f"  失敗的股票: {failed_tickers}")
        
        return success_count > 0
        
    except Exception as e:
        print(f"❌ API錯誤: {str(e)}")
        import traceback
        traceback.print_exc()
        return False
    
    finally:
        # 登出API
        try:
            api.logout()
            print("🔒 已登出Shioaji API")
        except:
            pass

def verify_downloaded_data():
    """驗證下載的數據"""
    print("\n" + "="*60)
    print("          數據驗證")
    print("="*60)
    
    # 讀取監控名單
    config_path = ROOT / "config" / "stocks.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    
    watchlist = config.get('stocks', {}).get('watchlist', [])
    
    all_good = True
    for ticker in watchlist:
        file_path = DATA_DIR / f"STOCK_{ticker}_5m.csv"
        if file_path.exists():
            try:
                # 檢查文件大小
                file_size = os.path.getsize(file_path)
                if file_size < 100:
                    print(f"❌ {ticker}: 文件過小 ({file_size} bytes)")
                    all_good = False
                    continue
                
                # 檢查列名
                with open(file_path, 'r') as f:
                    first_line = f.readline().strip()
                
                if 'timestamp' not in first_line:
                    print(f"❌ {ticker}: 缺少timestamp列")
                    all_good = False
                    continue
                
                # 檢查數據行數
                with open(file_path, 'r') as f:
                    line_count = sum(1 for _ in f) - 1  # 減去標題行
                
                if line_count < 10:
                    print(f"⚠ {ticker}: 數據行數較少 ({line_count} 行)")
                else:
                    print(f"✅ {ticker}: 數據完整 ({line_count} 行, {file_size:,} bytes)")
                    
            except Exception as e:
                print(f"❌ {ticker}: 驗證失敗 - {str(e)}")
                all_good = False
        else:
            print(f"❌ {ticker}: 文件不存在")
            all_good = False
    
    return all_good

def main():
    """主函數"""
    print("=" * 60)
    print("          自動下載缺失股票數據")
    print("=" * 60)
    
    # 檢查缺失的股票
    missing_tickers = check_missing_stocks()
    
    if not missing_tickers:
        print("\n🎉 所有股票數據完整，無需下載！")
        # 仍然驗證現有數據
        verify_downloaded_data()
        return True
    
    print(f"\n🚀 開始自動下載 {len(missing_tickers)} 檔缺失股票數據")
    print("⏳ 這可能需要幾分鐘時間...")
    
    # 下載缺失股票
    print("\n" + "="*60)
    print("          開始下載")
    print("="*60)
    
    success = download_missing_stocks(missing_tickers)
    
    print("\n" + "="*60)
    print("          下載完成")
    print("="*60)
    
    # 驗證下載的數據
    verify_downloaded_data()
    
    if success:
        print("\n✅ 數據下載完成！")
        print("\n📋 下一步:")
        print("  1. 運行系統測試驗證數據完整性")
        print("  2. 準備市場開盤")
    else:
        print("\n⚠ 數據下載部分完成或失敗")
        print("  請檢查錯誤信息並手動處理")
    
    return success

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n操作被用戶中斷")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 程序錯誤: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)exit(1)