#!/usr/bin/env python3
"""P1優化：出場機制增強

包含：
1. 移動停利 (Trailing Stop)
2. 時間停損 (Time-based Stop)
3. 動態出場策略
"""

import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

class ExitStrategyEnhancer:
    """出場策略增強器"""
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        
        # 持倉追蹤
        self.position_tracker = {}  # ticker -> position_info
        
    def update_position(self, ticker: str, entry_data: Dict):
        """更新持倉信息"""
        self.position_tracker[ticker] = {
            "entry_time": datetime.now(),
            "entry_price": entry_data.get("entry_price", 0),
            "stop_loss": entry_data.get("stop_loss", 0),
            "take_profit": entry_data.get("take_profit", 0),
            "trailing_start_pct": entry_data.get("trailing_start_pct", 0),
            "trailing_stop_pct": entry_data.get("trailing_stop_pct", 0.02),
            "max_holding_bars": entry_data.get("max_holding_bars", 30),
            "highest_price": entry_data.get("entry_price", 0),
            "trailing_active": False,
            "bars_held": 0
        }
    
    def check_exit_signals(self, ticker: str, current_price: float, 
                          current_time: datetime, bars_passed: int = 1) -> Optional[Dict]:
        """檢查出場信號"""
        if ticker not in self.position_tracker:
            return None
        
        pos = self.position_tracker[ticker]
        
        # 更新持倉時間
        pos["bars_held"] += bars_passed
        
        # 更新最高價（用於移動停利）
        if current_price > pos["highest_price"]:
            pos["highest_price"] = current_price
        
        # 1. 檢查停損
        if current_price <= pos["stop_loss"]:
            return {
                "action": "SELL",
                "reason": "STOP_LOSS_HIT",
                "price": current_price,
                "metadata": {
                    "exit_type": "stop_loss",
                    "entry_price": pos["entry_price"],
                    "pnl_pct": (current_price - pos["entry_price"]) / pos["entry_price"] * 100,
                    "bars_held": pos["bars_held"]
                }
            }
        
        # 2. 檢查停利
        if current_price >= pos["take_profit"]:
            return {
                "action": "SELL",
                "reason": "TAKE_PROFIT_HIT",
                "price": current_price,
                "metadata": {
                    "exit_type": "take_profit",
                    "entry_price": pos["entry_price"],
                    "pnl_pct": (current_price - pos["entry_price"]) / pos["entry_price"] * 100,
                    "bars_held": pos["bars_held"]
                }
            }
        
        # 3. 檢查移動停利
        exit_signal = self._check_trailing_stop(ticker, current_price, pos)
        if exit_signal:
            return exit_signal
        
        # 4. 檢查時間停損
        exit_signal = self._check_time_stop(ticker, current_time, pos)
        if exit_signal:
            return exit_signal
        
        # 5. 檢查持倉時間限制
        exit_signal = self._check_holding_time(ticker, pos)
        if exit_signal:
            return exit_signal
        
        return None
    
    def _check_trailing_stop(self, ticker: str, current_price: float, pos: Dict) -> Optional[Dict]:
        """檢查移動停利條件"""
        # 計算從最高價的回撤
        if pos["highest_price"] > 0:
            drawdown_from_high = (pos["highest_price"] - current_price) / pos["highest_price"]
            
            # 檢查是否達到移動停利啟動條件
            profit_from_entry = (pos["highest_price"] - pos["entry_price"]) / pos["entry_price"]
            if not pos["trailing_active"] and profit_from_entry >= pos["trailing_start_pct"]:
                pos["trailing_active"] = True
                print(f"[移動停利] {ticker} 移動停利啟動，最高價: {pos['highest_price']:.2f}")
            
            # 如果移動停利已啟動，檢查回撤是否觸發
            if pos["trailing_active"] and drawdown_from_high >= pos["trailing_stop_pct"]:
                return {
                    "action": "SELL",
                    "reason": "TRAILING_STOP_HIT",
                    "price": current_price,
                    "metadata": {
                        "exit_type": "trailing_stop",
                        "entry_price": pos["entry_price"],
                        "highest_price": pos["highest_price"],
                        "drawdown_from_high": drawdown_from_high * 100,
                        "pnl_pct": (current_price - pos["entry_price"]) / pos["entry_price"] * 100,
                        "bars_held": pos["bars_held"]
                    }
                }
        
        return None
    
    def _check_time_stop(self, ticker: str, current_time: datetime, pos: Dict) -> Optional[Dict]:
        """檢查時間停損"""
        # 檢查是否接近收盤時間（避免隔夜風險）
        if current_time.hour == 13 and current_time.minute >= 25:  # 收盤前5分鐘
            return {
                "action": "SELL",
                "reason": "MARKET_CLOSE_EXIT",
                "price": 0,  # 市價出場
                "metadata": {
                    "exit_type": "time_stop_market_close",
                    "entry_price": pos["entry_price"],
                    "bars_held": pos["bars_held"]
                }
            }
        
        return None
    
    def _check_holding_time(self, ticker: str, pos: Dict) -> Optional[Dict]:
        """檢查持倉時間限制"""
        if pos["bars_held"] >= pos["max_holding_bars"]:
            return {
                "action": "SELL",
                "reason": "MAX_HOLDING_TIME_REACHED",
                "price": 0,  # 市價出場
                "metadata": {
                    "exit_type": "time_stop_max_bars",
                    "entry_price": pos["entry_price"],
                    "bars_held": pos["bars_held"],
                    "max_bars": pos["max_holding_bars"]
                }
            }
        
        return None
    
    def remove_position(self, ticker: str):
        """移除持倉記錄"""
        if ticker in self.position_tracker:
            del self.position_tracker[ticker]
    
    def get_position_summary(self) -> Dict:
        """獲取所有持倉摘要"""
        summary = {}
        for ticker, pos in self.position_tracker.items():
            summary[ticker] = {
                "bars_held": pos["bars_held"],
                "entry_price": pos["entry_price"],
                "highest_price": pos["highest_price"],
                "trailing_active": pos["trailing_active"],
                "max_bars_remaining": max(0, pos["max_holding_bars"] - pos["bars_held"])
            }
        return summary


# 單例實例
_exit_enhancer = None

def get_exit_enhancer(config: Dict = None) -> ExitStrategyEnhancer:
    """獲取出場增強器單例"""
    global _exit_enhancer
    if _exit_enhancer is None:
        _exit_enhancer = ExitStrategyEnhancer(config)
    return _exit_enhancer

def should_exit_position(ticker: str, current_price: float, 
                        current_time: datetime, bars_passed: int = 1) -> Optional[Dict]:
    """檢查是否應該出場"""
    enhancer = get_exit_enhancer()
    return enhancer.check_exit_signals(ticker, current_price, current_time, bars_passed)

def update_position_entry(ticker: str, entry_data: Dict):
    """更新持倉進入信息"""
    enhancer = get_exit_enhancer()
    enhancer.update_position(ticker, entry_data)

def remove_position_exit(ticker: str):
    """移除已出場的持倉"""
    enhancer = get_exit_enhancer()
    enhancer.remove_position(ticker)

def get_all_positions_summary() -> Dict:
    """獲取所有持倉摘要"""
    enhancer = get_exit_enhancer()
    return enhancer.get_position_summary()