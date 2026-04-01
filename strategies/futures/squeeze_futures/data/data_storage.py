#!/usr/bin/env python3
"""
數據儲存模組
負責即時儲存 K 棒數據、交易記錄，並自動轉換為回測格式
"""

import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
import json
import pytz


class DataStorage:
    """即時數據儲存管理器"""
    
    def __init__(self, ticker: str = "TMF"):
        self.ticker = ticker
        self.tw_tz = pytz.timezone('Asia/Taipei')
        
        # 數據目錄
        self.market_dir = Path("logs/market_data")
        self.trade_dir = Path("exports/trades")
        self.backtest_dir = Path("exports/backtests")
        
        # 建立目錄
        self.market_dir.mkdir(parents=True, exist_ok=True)
        self.trade_dir.mkdir(parents=True, exist_ok=True)
        self.backtest_dir.mkdir(parents=True, exist_ok=True)
        
        # 當前數據文件
        self.date_str = datetime.now().strftime("%Y%m%d")
        self.market_file = self.market_dir / f"{ticker}_{self.date_str}_indicators.csv"
        self.trade_file = self.trade_dir / f"{ticker}_{self.date_str}_trades.json"
        
        # 初始化交易記錄
        self.trades = []
        if not self.trade_file.exists():
            self._save_trades()
    
    def save_kbar(self, timestamp: datetime, data: dict):
        """
        儲存 K 棒數據
        
        Args:
            timestamp: K 棒時間
            data: K 棒數據字典 (open, high, low, close, volume, score, sqz_on, mom_state, ...)
        """
        # 轉換為台北時間
        if hasattr(timestamp, 'tzinfo') and timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(self.tw_tz)
        elif isinstance(timestamp, datetime):
            timestamp = self.tw_tz.localize(timestamp)
        
        # 準備數據
        row = {
            'timestamp': timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            'open': data.get('open', 0),
            'high': data.get('high', 0),
            'low': data.get('low', 0),
            'close': data.get('close', 0),
            'volume': data.get('volume', 0),
            'vwap': data.get('vwap', 0),
            'score': data.get('score', 0),
            'sqz_on': data.get('sqz_on', False),
            'mom_state': data.get('mom_state', 0),
            'regime': data.get('regime', 'NORMAL'),
            'bull_align': data.get('bull_align', False),
            'bear_align': data.get('bear_align', False),
            'in_pb_zone': data.get('in_pb_zone', False),
        }
        
        # 轉換為 DataFrame
        df_row = pd.DataFrame([row])
        
        # 檢查檔案是否存在
        header = not self.market_file.exists()
        
        # 儲存
        df_row.to_csv(self.market_file, mode='a', index=False, header=header)
    
    def save_trade(self, trade: dict):
        """
        儲存交易記錄
        
        Args:
            trade: 交易字典 (type, timestamp, price, lots, pnl, reason, ...)
        """
        # 轉換時間為台北時間
        if 'timestamp' in trade and isinstance(trade['timestamp'], datetime):
            ts = trade['timestamp']
            if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
                ts = ts.astimezone(self.tw_tz)
            else:
                ts = self.tw_tz.localize(ts)
            trade['timestamp'] = ts.strftime('%Y-%m-%d %H:%M:%S')
        
        self.trades.append(trade)
        self._save_trades()
        
        # 同時儲存為 CSV (方便回測)
        self._save_trades_csv()
    
    def _save_trades(self):
        """儲存交易記錄為 JSON"""
        with open(self.trade_file, 'w', encoding='utf-8') as f:
            json.dump(self.trades, f, indent=2, ensure_ascii=False)
    
    def _save_trades_csv(self):
        """儲存交易記錄為 CSV (回測格式)"""
        if not self.trades:
            return
        
        csv_file = self.trade_dir / f"{self.ticker}_{self.date_str}_trades.csv"
        
        # 標準化欄位
        standardized = []
        for t in self.trades:
            std = {
                'timestamp': t.get('timestamp', ''),
                'type': t.get('type', ''),  # ENTRY, EXIT, PARTIAL_EXIT
                'direction': t.get('direction', ''),  # LONG, SHORT
                'price': t.get('price', 0),
                'lots': t.get('lots', 1),
                'pnl_pts': t.get('pnl_pts', 0),
                'pnl_cash': t.get('pnl_cash', 0),
                'reason': t.get('reason', ''),  # STOP_LOSS, TAKE_PROFIT, VWAP, EOD
            }
            standardized.append(std)
        
        df = pd.DataFrame(standardized)
        df.to_csv(csv_file, index=False)
    
    def export_for_backtest(self):
        """
        導出為回測格式
        
        Returns:
            dict: 包含 K 棒數據和交易記錄
        """
        # 載入 K 棒數據
        if not self.market_file.exists():
            return None
        
        df = pd.read_csv(self.market_file, index_col=0, parse_dates=True)
        
        # 載入交易記錄
        trades_df = None
        csv_file = self.trade_dir / f"{self.ticker}_{self.date_str}_trades.csv"
        if csv_file.exists():
            trades_df = pd.read_csv(csv_file, index_col=0, parse_dates=True)
        
        return {
            'kbars': df,
            'trades': trades_df,
            'date': self.date_str,
            'ticker': self.ticker,
        }
    
    def get_today_summary(self) -> dict:
        """獲取今日交易摘要"""
        if not self.trades:
            return {
                'total_trades': 0,
                'total_pnl': 0,
                'winning': 0,
                'losing': 0,
                'win_rate': 0,
            }
        
        total_pnl = sum(t.get('pnl_cash', 0) for t in self.trades if t.get('type') == 'EXIT')
        winning = [t for t in self.trades if t.get('pnl_cash', 0) > 0]
        losing = [t for t in self.trades if t.get('pnl_cash', 0) < 0]
        
        return {
            'total_trades': len([t for t in self.trades if t.get('type') == 'EXIT']),
            'total_pnl': total_pnl,
            'winning': len(winning),
            'losing': len(losing),
            'win_rate': len(winning) / max(len(winning) + len(losing), 1) * 100,
        }


# 全域實例
_storage = None

def get_storage(ticker: str = "TMF") -> DataStorage:
    """獲取數據儲存實例"""
    global _storage
    if _storage is None or _storage.ticker != ticker:
        _storage = DataStorage(ticker)
    return _storage


def save_kbar(timestamp: datetime, data: dict, ticker: str = "TMF"):
    """快速儲存 K 棒"""
    storage = get_storage(ticker)
    storage.save_kbar(timestamp, data)


def save_trade(trade: dict, ticker: str = "TMF"):
    """快速儲存交易"""
    storage = get_storage(ticker)
    storage.save_trade(trade)


if __name__ == "__main__":
    # 測試
    storage = DataStorage("TMF")
    
    # 測試儲存 K 棒
    now = datetime.now()
    storage.save_kbar(now, {
        'open': 32000,
        'high': 32100,
        'low': 31900,
        'close': 32050,
        'volume': 1000,
        'score': 45.5,
        'sqz_on': False,
        'mom_state': 2,
    })
    
    # 測試儲存交易
    storage.save_trade({
        'type': 'ENTRY',
        'timestamp': now,
        'direction': 'LONG',
        'price': 32050,
        'lots': 1,
    })
    
    print(f"✓ 測試完成")
    print(f"  K 棒檔案：{storage.market_file}")
    print(f"  交易檔案：{storage.trade_file}")
