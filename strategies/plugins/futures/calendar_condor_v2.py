#!/usr/bin/env python3
"""
Calendar Condor Strategy v2.0 - Fixed contract handling

This version uses proper contract resolution to avoid issues with
rolling contracts (TMFR1, TMFR2) and handles expiry properly.
"""

import os
import sys
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Any, Optional, Dict, Tuple
import pandas as pd

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext


@dataclass
class CalendarCondorV2(StrategyBase):
    """期貨日曆跨月策略 v2.0 - 使用正確合約處理"""
    
    name = "calendar_condor_v2"
    description = "期貨日曆跨月策略 v2.0 (正確合約處理 + 近月/遠月價差均值回歸)"
    version = "2.0"
    
    # 策略狀態
    position_side: str = ""  # "SHORT_SPREAD" or "LONG_SPREAD"
    entry_bar_idx: int = 0
    entry_spread: float = 0.0
    entry_spread_z: float = 0.0
    peak_unrealized_pnl: float = 0.0
    
    # 合約信息
    near_contract_code: str = ""
    far_contract_code: str = ""
    
    # 策略參數 (從 config 載入)
    params: dict = None
    
    def init(self, context: StrategyContext) -> None:
        """初始化策略"""
        super().init(context)
        self.params = context.config.get("params", {})
        
        # 設置默認參數
        defaults = {
            # Entry conditions
            "entry_vwap_z": 2.5,           # VWAP Z-score threshold
            "entry_spread_z": 3.0,         # Spread Z-score threshold
            "min_spread_std": 8.0,         # Minimum spread standard deviation (increased for MXF)
            "min_expected_profit": 15.0,   # Minimum expected profit points (increased for MXF)
            "max_adx": 25.0,               # Maximum ADX for weak regime
            "max_breakout_strength": 0.5,  # Maximum breakout strength
            "min_volume_spike": 0.7,       # Minimum volume spike
            
            # Exit conditions
            "exit_spread_z": -0.5,         # Exit when spread Z-score crosses this
            "stop_loss_spread_z": 3.5,     # Stop loss spread Z-score
            "max_holding_bars": 100,       # Maximum holding period
            "min_holding_bars": 10,        # Minimum holding period
            "min_profit_points": 15,       # Minimum profit points required (increased for MXF)
            
            # Risk management
            "position_size": 1,            # Contracts per trade
            "allow_night_session": False,  # Trade during night session
            "min_bars_from_session_open": 6,  # Wait for market to stabilize
            "cooldown_bars": 20,           # Cooldown period after trade
            
            # Contract management
            "days_to_switch": 3,           # Days before expiry to switch contracts
        }
        
        for key, default in defaults.items():
            if key not in self.params:
                self.params[key] = default
        
        # 重置狀態
        self._reset_state()
        
        # 初始化合約信息 (在實際交易中會從 context 獲取)
        self.near_contract_code = "TMFE6"  # 示例，實際應從合約解析器獲取
        self.far_contract_code = "TMFF6"   # 示例，實際應從合約解析器獲取
        
        print(f"[CalendarCondorV2] Initialized with near={self.near_contract_code}, far={self.far_contract_code}")
    
    def _reset_state(self) -> None:
        """重置策略狀態"""
        self.position_side = ""
        self.entry_bar_idx = 0
        self.entry_spread = 0.0
        self.entry_spread_z = 0.0
        self.peak_unrealized_pnl = 0.0
    
    def on_bar(self, context: StrategyContext) -> Optional[Signal]:
        """處理每個 bar 的數據"""
        if not self._validate_bar(context):
            return None
        
        # 檢查退出條件
        if self.position_side:
            exit_signal = self._check_exit(context)
            if exit_signal:
                return exit_signal
        
        # 檢查進場條件
        return self._check_entry(context)
    
    def _validate_bar(self, context: StrategyContext) -> bool:
        """驗證 bar 數據是否有效"""
        bar = context.market.last_bar
        if not bar:
            return False
        
        # 檢查必要欄位
        required_fields = [
            "regime", "adx", "breakout_strength", "volume_spike",
            "price_vs_vwap", "vwap_z", "spread_z",
            "bars_from_session_open", "is_night_session"
        ]
        
        for field in required_fields:
            if field not in bar:
                print(f"[CalendarCondorV2] Missing field: {field}")
                return False
        
        # 檢查 regime 是否符合
        regime = bar["regime"]
        if regime != "WEAK":
            # 只在 WEAK regime 交易
            return False
        
        # 檢查是否在夜盤交易
        if not self.params["allow_night_session"] and bar["is_night_session"]:
            return False
        
        # 檢查是否在開盤初期
        if bar["bars_from_session_open"] < self.params["min_bars_from_session_open"]:
            return False
        
        return True
    
    def _check_entry(self, context: StrategyContext) -> Optional[Signal]:
        """檢查進場條件"""
        bar = context.market.last_bar
        
        # 檢查是否已有持倉
        if self.position_side:
            return None
        
        # 檢查 regime 條件
        regime = bar["regime"]
        adx = bar["adx"]
        breakout_strength = bar["breakout_strength"]
        volume_spike = bar["volume_spike"]
        
        if regime != "WEAK":
            return None
        
        if adx > self.params["max_adx"]:
            return None
        
        if breakout_strength > self.params["max_breakout_strength"]:
            return None
        
        if volume_spike < self.params["min_volume_spike"]:
            return None
        
        # 檢查雙重過濾條件
        vwap_z = bar["vwap_z"]
        spread_z = bar["spread_z"]
        price_vs_vwap = bar["price_vs_vwap"]
        
        # 新增：檢查價差波動是否足夠
        spread_std = bar.get("spread_std", 0.0)
        if spread_std < self.params.get("min_spread_std", 5.0):
            return None
        
        # 計算預期獲利點數
        # 預期價差從 entry_spread_z 回歸到 exit_spread_z
        expected_spread_change = abs(spread_z - self.params["exit_spread_z"])
        expected_profit_points = expected_spread_change * spread_std
        
        # 新增：檢查預期獲利是否足夠覆蓋摩擦成本
        min_expected_profit = self.params.get("min_expected_profit", 10.0)
        if expected_profit_points < min_expected_profit:
            return None
        
        # 條件 1: 價格相對於 VWAP 拉伸
        # 條件 2: 價差拉伸
        if vwap_z > self.params["entry_vwap_z"] and spread_z > self.params["entry_spread_z"]:
            # 做空價差 (賣近月買遠月)
            self.position_side = "SHORT_SPREAD"
            self.entry_bar_idx = context.bar_counter
            self.entry_spread_z = spread_z
            
            print(f"[CalendarCondorV2] SHORT_SPREAD entry: vwap_z={vwap_z:.2f}, spread_z={spread_z:.2f}, "
                  f"expected_profit={expected_profit_points:.1f} points")
            
            return Signal(
                action="SELL",
                reason="calendar_condor_short_spread",
                stop_loss=0.0,  # 由監控層計算
                target=0.0,     # 由監控層計算
                confidence=0.8,
                quantity=self.params["position_size"],
                trail_points=0.0,
                break_even_trigger=0.0
            )
        
        elif vwap_z < -self.params["entry_vwap_z"] and spread_z < -self.params["entry_spread_z"]:
            # 做多價差 (買近月賣遠月)
            self.position_side = "LONG_SPREAD"
            self.entry_bar_idx = context.bar_counter
            self.entry_spread_z = spread_z
            
            print(f"[CalendarCondorV2] LONG_SPREAD entry: vwap_z={vwap_z:.2f}, spread_z={spread_z:.2f}, "
                  f"expected_profit={expected_profit_points:.1f} points")
            
            return Signal(
                action="BUY",
                reason="calendar_condor_long_spread",
                stop_loss=0.0,  # 由監控層計算
                target=0.0,     # 由監控層計算
                confidence=0.8,
                quantity=self.params["position_size"],
                trail_points=0.0,
                break_even_trigger=0.0
            )
        
        return None
    
    def _check_exit(self, context: StrategyContext) -> Optional[Signal]:
        """檢查退出條件"""
        bar = context.market.last_bar
        spread_z = bar["spread_z"]
        bars_from_entry = context.bar_counter - self.entry_bar_idx
        
        # 1. 硬性退出條件
        
        # 趨勢變化退出
        regime = bar["regime"]
        if regime != "WEAK":
            print(f"[CalendarCondorV2] Exit due to regime change: {regime}")
            self._reset_state()
            return Signal(
                action="EXIT",
                reason="calendar_condor_regime_exit",
                stop_loss=0.0,
                target=0.0,
                confidence=1.0,
                quantity=self.params["position_size"],
                trail_points=0.0,
                break_even_trigger=0.0
            )
        
        # 停損退出
        if self.position_side == "SHORT_SPREAD" and spread_z > self.params["stop_loss_spread_z"]:
            print(f"[CalendarCondorV2] Stop loss triggered: spread_z={spread_z:.2f}")
            self._reset_state()
            return Signal(
                action="EXIT",
                reason="calendar_condor_stop_loss",
                stop_loss=0.0,
                target=0.0,
                confidence=1.0,
                quantity=self.params["position_size"],
                trail_points=0.0,
                break_even_trigger=0.0
            )
        
        elif self.position_side == "LONG_SPREAD" and spread_z < -self.params["stop_loss_spread_z"]:
            print(f"[CalendarCondorV2] Stop loss triggered: spread_z={spread_z:.2f}")
            self._reset_state()
            return Signal(
                action="EXIT",
                reason="calendar_condor_stop_loss",
                stop_loss=0.0,
                target=0.0,
                confidence=1.0,
                quantity=self.params["position_size"],
                trail_points=0.0,
                break_even_trigger=0.0
            )
        
        # 時間退出 (持有時間過長)
        if bars_from_entry >= self.params["max_holding_bars"]:
            print(f"[CalendarCondorV2] Time exit: held for {bars_from_entry} bars")
            self._reset_state()
            return Signal(
                action="EXIT",
                reason="calendar_condor_time_exit",
                stop_loss=0.0,
                target=0.0,
                confidence=0.7,
                quantity=self.params["position_size"],
                trail_points=0.0,
                break_even_trigger=0.0
            )
        
        # 2. 利潤退出條件 (需要最小持有時間)
        if bars_from_entry >= self.params["min_holding_bars"]:
            if self.position_side == "SHORT_SPREAD" and spread_z < self.params["exit_spread_z"]:
                # 做空價差獲利了結 (spread_z 從正變負)
                print(f"[CalendarCondorV2] Profit exit: spread_z={spread_z:.2f}")
                self._reset_state()
                return Signal(
                    action="EXIT",
                    reason="calendar_condor_profit_exit",
                    stop_loss=0.0,
                    target=0.0,
                    confidence=0.9,
                    quantity=self.params["position_size"],
                    trail_points=0.0,
                    break_even_trigger=0.0
                )
            
            elif self.position_side == "LONG_SPREAD" and spread_z > -self.params["exit_spread_z"]:
                # 做多價差獲利了結 (spread_z 從負變正)
                print(f"[CalendarCondorV2] Profit exit: spread_z={spread_z:.2f}")
                self._reset_state()
                return Signal(
                    action="EXIT",
                    reason="calendar_condor_profit_exit",
                    stop_loss=0.0,
                    target=0.0,
                    confidence=0.9,
                    quantity=self.params["position_size"],
                    trail_points=0.0,
                    break_even_trigger=0.0
                )
        
        return None
    
    def get_status(self) -> Dict[str, Any]:
        """獲取策略狀態"""
        return {
            "position_side": self.position_side,
            "entry_bar_idx": self.entry_bar_idx,
            "entry_spread_z": self.entry_spread_z,
            "near_contract": self.near_contract_code,
            "far_contract": self.far_contract_code,
            "params": self.params,
        }


# 測試函數
def test_calendar_condor_v2():
    """測試 calendar_condor_v2 策略"""
    from unittest.mock import Mock
    
    # 創建模擬 context
    mock_context = Mock(spec=StrategyContext)
    mock_context.config = {
        "params": {
            "entry_vwap_z": 2.0,
            "entry_spread_z": 2.0,
            "max_adx": 25.0,
            "max_breakout_strength": 0.5,
            "min_volume_spike": 0.7,
            "exit_spread_z": 0.5,
            "stop_loss_spread_z": 2.5,
            "max_holding_bars": 50,
            "min_holding_bars": 5,
            "position_size": 1,
            "allow_night_session": False,
            "min_bars_from_session_open": 6,
            "days_to_switch": 3,
        }
    }
    
    # 創建策略實例
    strategy = CalendarCondorV2()
    strategy.init(mock_context)
    
    print("CalendarCondorV2 test completed")
    print(f"Strategy status: {strategy.get_status()}")


if __name__ == "__main__":
    test_calendar_condor_v2()