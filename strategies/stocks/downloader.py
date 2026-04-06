import os
import pandas as pd
from pathlib import Path
from datetime import timedelta
import shioaji as sj
from dotenv import load_dotenv

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
DATA_DIR = ROOT / "data" / "taifex_raw"

class StockDownloader:
    def __init__(self, api):
        self.api = api

    def update_ticker(self, ticker: str):
        """更新單一標的的 5分K 數據 (整股數據，分析用)"""
        file_path = DATA_DIR / f"STOCK_{ticker}_5m.csv"
        
        # 1. 決定開始日期
        start_date = "2025-01-01" # 預設起始點
        existing_df = None
        
        if file_path.exists():
            try:
                existing_df = pd.read_csv(file_path)
                date_col = "Date" if "Date" in existing_df.columns else "timestamp"
                # 強制轉為 datetime 並移除時區資訊 (tz-naive)
                existing_df[date_col] = pd.to_datetime(existing_df[date_col], errors="coerce").dt.tz_localize(None)
                last_ts = existing_df[date_col].max()
                if pd.notna(last_ts):
                    start_date = (last_ts + timedelta(days=1)).strftime("%Y-%m-%d")
                    print(f"🔄 {ticker}: Found existing data up to {last_ts}. Fetching from {start_date}...")
            except Exception as e:
                print(f"⚠️ Error reading {file_path}: {e}. Restarting from scratch.")

        # 2. 抓取數據 (使用整股 Kbars)
        try:
            contract = self.api.Contracts.Stocks[ticker]
            kbars = self.api.kbars(contract, start=start_date)
            new_df = pd.DataFrame({**kbars})
            
            if new_df.empty:
                print(f"✅ {ticker}: Already up to date.")
                return

            new_df.ts = pd.to_datetime(new_df.ts)
            # 標準化欄位名稱與 tw-trading-unified 一致
            new_df = new_df.rename(columns={
                "ts": "Date", "Open": "Open", "High": "High", 
                "Low": "Low", "Close": "Close", "Volume": "Volume"
            })

            # 3. 合併並存檔
            if existing_df is not None:
                combined_df = pd.concat([existing_df, new_df], ignore_index=True)
                # 去重 (基於時間戳記)
                combined_df = combined_df.drop_duplicates(subset=["Date"]).sort_values("Date")
            else:
                combined_df = new_df

            combined_df.to_csv(file_path, index=False)
            print(f"🚀 {ticker}: Successfully updated. New size: {len(combined_df)} rows.")
            
        except Exception as e:
            print(f"❌ Failed to update {ticker}: {e}")

def run_update_all():
    load_dotenv(override=True)
    user_id = os.getenv('SHIOAJI_API_KEY') or os.getenv('SHIOAJI_PERSON_ID')
    password = os.getenv('SHIOAJI_SECRET_KEY') or os.getenv('SHIOAJI_PASSWD')

    api = sj.Shioaji()
    api.login(user_id, password, contracts_timeout=10000)
    
    downloader = StockDownloader(api)
    
    # 掃描目前的資料庫檔案
    tickers = [f.stem.split("_")[1] for f in DATA_DIR.glob("STOCK_*_5m.csv")]
    print(f"🔍 Found {len(tickers)} tickers to update.")
    
    for t in tickers:
        downloader.update_ticker(t)
        
    api.logout()

if __name__ == "__main__":
    run_update_all()
