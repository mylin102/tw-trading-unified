#!/usr/bin/env python3
"""
股票數據儲存模組
負責即時儲存股票指標數據，供 Dashboard 顯示
"""

import pandas as pd
from datetime import datetime
from pathlib import Path
import pytz

class StockDataStorage:
    """股票指標數據儲存管理器"""
    
    def __init__(self, ticker: str = "STOCK"):
        self.ticker = ticker
        self.tw_tz = pytz.timezone('Asia/Taipei')
        
        # 數據目錄 - 使用絕對路徑
        # 計算項目根目錄：從當前文件向上三級到項目根目錄
        current_file = Path(__file__).resolve()
        project_root = current_file.parent.parent.parent
        self.market_dir = project_root / "logs" / "market_data"
        self.market_dir.mkdir(parents=True, exist_ok=True)
        
        # 當前數據文件
        # 修正：支援交易日邏輯，凌晨 5 點前算在前一天
        now = datetime.now()
        self.date_str = (now - __import__('datetime').timedelta(days=1)).strftime('%Y%m%d') if now.hour < 5 else now.strftime('%Y%m%d')
        
        self.market_file = self.market_dir / f"STOCK_{ticker}_{self.date_str}_indicators.csv"
    
    def save_indicators(self, timestamp: datetime, data: dict):
        """
        儲存指標數據
        
        Args:
            timestamp: 數據時間
            data: 指標數據字典 (包含所有技術指標)
        """
        # 轉換為台北時間
        if hasattr(timestamp, 'tzinfo') and timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(self.tw_tz)
        elif isinstance(timestamp, datetime):
            timestamp = self.tw_tz.localize(timestamp)
        
        # 準備數據 - 標準化列名為小寫
        row = {}
        for key, value in data.items():
            # 將列名轉換為小寫
            lower_key = key.lower()
            # 處理特殊情況：timestamp 和 ts 都映射到 timestamp
            if lower_key == 'ts':
                row['timestamp'] = value
            else:
                row[lower_key] = value
        
        # 確保基本欄位存在
        row.update({
            'timestamp': timestamp.strftime('%Y-%m-%d %H:%M:%S'),
        })
        
        # 轉換為 DataFrame
        df_row = pd.DataFrame([row])
        
        # 儲存 (支援動態欄位擴展)
        if self.market_file.exists():
            try:
                df_existing = pd.read_csv(self.market_file)
                # 標準化現有數據的列名
                df_existing.columns = [c.lower() for c in df_existing.columns]
                # 確保 timestamp 列存在
                if 'ts' in df_existing.columns:
                    df_existing = df_existing.rename(columns={'ts': 'timestamp'})
                
                df_combined = pd.concat([df_existing, df_row], ignore_index=True)
                df_combined.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
                df_combined.to_csv(self.market_file, index=False)
            except Exception:
                # Fallback to append if read fails
                df_row.to_csv(self.market_file, mode='a', index=False, header=False)
        else:
            df_row.to_csv(self.market_file, index=False, header=True)
    
    def get_latest_indicators(self):
        """獲取最新指標數據"""
        if self.market_file.exists():
            try:
                df = pd.read_csv(self.market_file)
                # 標準化列名
                df.columns = [c.lower() for c in df.columns]
                # 確保 timestamp 列存在
                if 'ts' in df.columns:
                    df = df.rename(columns={'ts': 'timestamp'})
                
                if not df.empty:
                    return df.iloc[-1].to_dict()
            except Exception:
                pass
        return None