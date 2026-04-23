"""
Calendar Condor - 期貨日曆跨月策略 (近月/遠月價差均值回歸)

策略概念:
1. 專注於 WEAK regime (震盪市場)
2. 雙重過濾: 價格相對於 VWAP 的拉伸 + 價差拉伸
3. 嚴格風險控制，硬性退出優先於利潤優化
4. 近月/遠月價差交易，降低單邊風險

版本: v1.0
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext


@dataclass
class CalendarCondor(StrategyBase):
    """期貨日曆跨月策略"""
    
    name = "calendar_condor"
    description = "期貨日曆跨月策略 (近月/遠月價差均值回歸)"
    version = "1.0"
    
    # 策略狀態
    position_side: str = ""  # "SELL_NEAR_BUY_FAR" or "BUY_NEAR_SELL_FAR"
    entry_bar_idx: int = 0
    peak_unrealized_pnl: float = 0.0
    tp_delay_used: int = 0
    partial_exit_done: bool = False
    
    # 策略參數 (從 config 載入)
    params: dict = None
    
    def init(self, context: StrategyContext) -> None:
        """初始化策略"""
        super().init(context)
        self.params = context.config.get("params", {})
        
        # 設置默認參數
        defaults = {
            "entry_adx_max": 25.0,           # ADX 上限
            "entry_breakout_strength_max": 0.5,  # 突破強度上限
            "entry_volume_spike_min": 0.7,   # 成交量放大下限
            "entry_vwap_z": 2.0,             # VWAP Z-score 門檻
            "entry_spread_z": 2.0,           # 價差 Z-score 門檻
            "exit_trend_regime": True,       # 趨勢退出
            "exit_price_reversal": True,     # 價格反轉退出
            "exit_time_bars": 50,            # 時間退出 (bar 數)
            "exit_partial_profit_trigger": 0.5,  # 部分出場利潤觸發
            "exit_partial_size": 0.5,        # 部分出場比例
            "stop_loss_atr_mult": 1.5,       # 停損 ATR 倍數
            "take_profit_atr_mult": 2.5,     # 停利 ATR 倍數
        }
        
        for key, default in defaults.items():
            if key not in self.params:
                self.params[key] = default
        
        # 重置狀態
        self.position_side = ""
        self.entry_bar_idx = 0
        self.peak_unrealized_pnl = 0.0
        self.tp_delay_used = 0
        self.partial_exit_done = False
    
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
        
        required_fields = [
            "regime", "adx", "breakout_strength", "volume_spike",
            "price_vs_vwap", "vwap_z", "spread_z",
            "bars_from_session_open", "is_night_session"
        ]
        
        for field in required_fields:
            if field not in bar:
                return False
        
        return True
    
    def _check_entry(self, context: StrategyContext) -> Optional[Signal]:
        """檢查進場條件"""
        bar = context.market.last_bar
        regime = bar["regime"]
        adx = bar["adx"]
        breakout_strength = bar["breakout_strength"]
        volume_spike = bar["volume_spike"]
        vwap_z = bar["vwap_z"]
        spread_z = bar["spread_z"]
        
        # 只交易 WEAK regime
        if regime != "WEAK":
            return None
        
        # 過濾條件
        if adx > self.params["entry_adx_max"]:
            return None
        
        if breakout_strength > self.params["entry_breakout_strength_max"]:
            return None
        
        if volume_spike < self.params["entry_volume_spike_min"]:
            return None
        
        # 做空近月/做多遠月 (價格超買)
        if vwap_z >= self.params["entry_vwap_z"] and spread_z >= self.params["entry_spread_z"]:
            # 更新策略狀態
            self.position_side = "SELL_NEAR_BUY_FAR"
            self.entry_bar_idx = context.bar_counter
            self.peak_unrealized_pnl = 0.0
            self.tp_delay_used = 0
            self.partial_exit_done = False
            
            return Signal(
                action="SELL",
                reason="calendar_condor_vwap_high_and_spread_high",
                stop_loss=0.0,
                target=0.0,
                confidence=0.8,
                quantity=1,
                trail_points=0.0,
                break_even_trigger=0.0
            )
        
        # 做多近月/做空遠月 (價格超賣)
        if vwap_z <= -self.params["entry_vwap_z"] and spread_z <= -self.params["entry_spread_z"]:
            # 更新策略狀態
            self.position_side = "BUY_NEAR_SELL_FAR"
            self.entry_bar_idx = context.bar_counter
            self.peak_unrealized_pnl = 0.0
            self.tp_delay_used = 0
            self.partial_exit_done = False
            
            return Signal(
                action="BUY",
                reason="calendar_condor_vwap_low_and_spread_low",
                stop_loss=0.0,
                target=0.0,
                confidence=0.8,
                quantity=1,
                trail_points=0.0,
                break_even_trigger=0.0
            )
        
        return None
    
    def _check_exit(self, context: StrategyContext) -> Optional[Signal]:
        """檢查退出條件
        
        退出層級:
        1. 硬性退出: 趨勢變化、價格反轉、時間到期
        2. 利潤優化: 部分出場、追蹤停損
        """
        bar = context.market.last_bar
        regime = bar["regime"]
        price_vs_vwap = bar["price_vs_vwap"]
        bars_from_entry = context.bar_counter - self.entry_bar_idx
        
        # 1. 硬性退出條件 (優先)
        
        # 趨勢退出
        if self.params["exit_trend_regime"] and regime != "WEAK":
            self._reset_state()
            return Signal(
                action="EXIT",
                reason="calendar_condor_trend_exit",
                stop_loss=0.0,
                target=0.0,
                confidence=1.0,
                quantity=1,
                trail_points=0.0,
                break_even_trigger=0.0
            )
        
        # 價格反轉退出
        if self.params["exit_price_reversal"]:
            if self.position_side == "SELL_NEAR_BUY_FAR" and price_vs_vwap < 0:
                self._reset_state()
                return Signal(
                    action="EXIT",
                    reason="calendar_condor_price_reversal",
                    stop_loss=0.0,
                    target=0.0,
                    confidence=0.9,
                    quantity=1,
                    trail_points=0.0,
                    break_even_trigger=0.0
                )
            elif self.position_side == "BUY_NEAR_SELL_FAR" and price_vs_vwap > 0:
                self._reset_state()
                return Signal(
                    action="EXIT",
                    reason="calendar_condor_price_reversal",
                    stop_loss=0.0,
                    target=0.0,
                    confidence=0.9,
                    quantity=1,
                    trail_points=0.0,
                    break_even_trigger=0.0
                )
        
        # 時間退出
        if bars_from_entry >= self.params["exit_time_bars"]:
            self._reset_state()
            return Signal(
                action="EXIT",
                reason="calendar_condor_time_exit",
                stop_loss=0.0,
                target=0.0,
                confidence=0.7,
                quantity=1,
                trail_points=0.0,
                break_even_trigger=0.0
            )
        
        # 2. 利潤優化條件 (次要)
        # 這裡可以實現部分出場邏輯，但為了簡單起見，我們只實現硬性退出
        
        return None
    
    def _reset_state(self) -> None:
        """重置策略狀態"""
        self.position_side = ""
        self.entry_bar_idx = 0
        self.peak_unrealized_pnl = 0.0
        self.tp_delay_used = 0
        self.partial_exit_done = False
    
    def cleanup(self) -> None:
        """清理策略資源"""
        self._reset_state()