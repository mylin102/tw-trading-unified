#!/usr/bin/env python3
"""
下載缺失股票數據腳本
專門下載監控名單中缺失的5檔股票數據
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
        
        # 計算開始日期
        start_date = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")
        print(f"📅 下載期間: {start_date} 至今")
        
        # 下載每檔缺失股票
        success_count = 0
        
        for i, ticker in enumerate(missing_tickers, 1):
            print(f"\n[{i}/{len(missing_tickers)}] 下載 {ticker}...")
            
            try:
                # 獲取合約
                contract = api.Contracts.Stocks[ticker]
                if not contract:
                    print(f"  ❌ {ticker}: 合約不存在")
                    continue
                
                # 下載K線數據
                print(f"  📥 下載5分鐘K線數據...")
                kbars = api.kbars(contract, start=start_date)
                
                # 轉換為DataFrame (使用兼容性助手)
                df = kbars_to_dataframe(kbars)
                
                if df.empty:
                    print(f"  ⚠ {ticker}: 無數據返回")
                    continue
                
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
                else:
                    print(f"  ❌ {ticker}: 文件保存失敗")
                
                # 避免請求過快
                time.sleep(1)
                
            except Exception as e:
                print(f"  ❌ {ticker}: 下載失敗 - {str(e)}")
                continue
        
        print(f"\n📊 下載結果:")
        print(f"  成功: {success_count}/{len(missing_tickers)}")
        print(f"  失敗: {len(missing_tickers) - success_count}")
        
        return success_count > 0
        
    except Exception as e:
        print(f"❌ API錯誤: {str(e)}")
        return False
    
    finally:
        # 登出API
        try:
            api.logout()
            print("🔒 已登出Shioaji API")
        except:
            pass

def main():
    """主函數"""
    print("=" * 60)
    print("          缺失股票數據下載工具")
    print("=" * 60)
    
    # 檢查缺失的股票
    missing_tickers = check_missing_stocks()
    
    if not missing_tickers:
        print("\n🎉 所有股票數據完整，無需下載！")
        return True
    
    print(f"\n🚀 準備下載 {len(missing_tickers)} 檔缺失股票數據")
    
    # 確認是否繼續
    response = input(f"\n是否繼續下載？(y/n): ").strip().lower()
    if response != 'y':
        print("操作取消")
        return False
    
    # 下載缺失股票
    success = download_missing_stocks(missing_tickers)
    
    if success:
        print("\n✅ 數據下載完成！")
        print("\n📋 下一步:")
        print("  1. 檢查下載的數據文件")
        print("  2. 運行系統測試驗證數據完整性")
        print("  3. 準備市場開盤")
    else:
        print("\n❌ 數據下載失敗，請檢查錯誤信息")
    
    return success

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n操作被用戶中斷")
        sys.exit(1)exit(1)