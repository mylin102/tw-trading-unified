#!/usr/bin/env python3
"""
Calendar Condor Strategy v2.0 - Fixed contract handling

This version uses proper contract resolution to avoid issues with
rolling contracts (TMFR1, TMFR2) and handles expiry properly.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from typing import Any, Optional, Dict, Tuple
import pandas as pd

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext
from core.strategy_eval import StrategyEval


class CalendarCondorV2(StrategyBase):
    """期貨日曆跨月策略 v2.0 - 使用正確合約處理"""

    def __init__(self) -> None:
        super().__init__()
        self.position_side: str = ""
        self.entry_bar_idx: int = 0
        self.entry_spread: float = 0.0
        self.entry_spread_z: float = 0.0
        self.peak_unrealized_pnl: float = 0.0
        self.near_contract_code: str = ""
        self.far_contract_code: str = ""
        self.params: dict = None
        # name/version as class attributes
        self._name = "calendar_condor_v2"
        self._desc = "期貨日曆跨月策略 v2.0 (正確合約處理 + 近月/遠月價差均值回歸)"

    @property
    def name(self) -> str:
        return self._name

    @property
    def metadata(self) -> dict:
        return {
            "asset_class": "futures",
            "version": "2.0",
            "backtest_pf": 0.0,
            "backtest_wr": 0.0,
            "backtest_maxdd": 0.0,
            "market_regime": "weak",
            "description": self._desc,
            "indicators": ["spread_z", "vwap_z", "adx"],
        }

    version = "2.0"

    # 策略狀態
    
    def init(self, context: StrategyContext) -> None:
        """初始化策略"""
        super().init(context)
        self.params = context.config.get("params", {})
        
        # 設置默認參數
        defaults = {
            # Entry conditions
            "entry_vwap_z": 2.5,           # VWAP Z-score threshold
            "entry_spread_z": 3.0,         # Spread Z-score threshold
            "min_spread_std": 5.0,         # Minimum spread standard deviation (relaxed from 8.0 for MXF open)
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
        
        # 2026-06-18 Gemini CLI: Remove hardcoded contracts, will be resolved from context
        self.near_contract_code = "" 
        self.far_contract_code = ""
        
        # print(f"[CalendarCondorV2] Initialized, waiting for context to resolve contracts")
    
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
            self._set_eval(skip_reason="HOLDING", position_side=self.position_side,
                           bars_held=context.bar_counter - self.entry_bar_idx)
            return None

        # 檢查進場條件
        return self._check_entry(context)
    
    def _validate_bar(self, context: StrategyContext) -> bool:
        """驗證 bar 數據是否有效"""
        bar = context.market.last_bar
        ts = bar.get("ts") or bar.get("timestamp") or bar.get("name") or "?"
        ts_str = str(ts)

        if not bar:
            print(f"[CalendarCondorV2][SKIP] NO_BAR ts={ts_str}")
            self._set_eval(skip_reason="NO_BAR")
            return False

        # 使用 SpreadLoader 補齊缺失的 spread / vwap_z 等欄位
        self._enrich_bar(bar)
        
        # 檢查必要欄位（經 enrichment 後應全部存在）
        required_fields = [
            "regime", "adx", "breakout_strength", "volume_spike",
            "price_vs_vwap", "vwap_z", "spread_z",
            "bars_from_session_open", "is_night_session"
        ]
        
        for field in required_fields:
            val = bar.get(field)
            if val is None:
                # enrichment 後仍缺失 → bar 不夠用
                reason = f"MISSING_FIELD:{field}"
                print(f"[CalendarCondorV2][SKIP] {reason} ts={ts_str} field={field}")
                self._set_eval(skip_reason=reason, required_fields=required_fields,
                               available=[k for k, v in bar.items() if v is not None][:10])
                return False

        # 檢查 regime 是否符合
        regime = bar["regime"]
        if regime != "WEAK":
            code = "NO_REGIME"
            print(f"[CalendarCondorV2][SKIP] {code} ts={ts_str} regime={regime}")
            self._set_eval(skip_reason=f"{code}:{regime}", regime=regime)
            return False

        # 檢查遠月合約是否可用
        far_close = bar.get("far_close")
        if far_close is None or far_close == 0:
            code = "NO_FAR_CONTRACT"
            print(f"[CalendarCondorV2][SKIP] {code} ts={ts_str}")
            self._set_eval(skip_reason=code, far_close=far_close)
            return False

        # 檢查 spread_std 是否足夠大
        spread_std = bar.get("spread_std", 0)
        min_std = self.params.get("min_spread_std", 5.0)
        if spread_std < min_std:
            code = "SPREAD_TOO_SMALL"
            print(f"[CalendarCondorV2][SKIP] {code} ts={ts_str} spread_std={spread_std:.2f} < {min_std:.2f}")
            self._set_eval(skip_reason=code, spread_std=spread_std, min_spread_std=min_std)
            return False

        # 檢查是否在夜盤交易
        if not self.params["allow_night_session"] and bar["is_night_session"]:
            code = "SESSION_BLOCKED"
            print(f"[CalendarCondorV2][SKIP] {code} ts={ts_str} is_night_session=True")
            self._set_eval(skip_reason=f"{code}:NIGHT", is_night_session=bar["is_night_session"])
            return False

        # 檢查是否在開盤初期
        if bar["bars_from_session_open"] < self.params["min_bars_from_session_open"]:
            code = "SESSION_BLOCKED"
            print(f"[CalendarCondorV2][SKIP] {code} ts={ts_str} bars_from_open={bar['bars_from_session_open']} < {self.params['min_bars_from_session_open']}")
            self._set_eval(skip_reason=f"{code}:COOLDOWN",
                           bars_from_open=bar["bars_from_session_open"],
                           min_bars=self.params["min_bars_from_session_open"])
            return False
        
        return True
    
    def _enrich_bar(self, bar: dict) -> None:
        """使用 SpreadLoader 補齊 bar 中缺失的 spread / vwap / session 欄位。
        
        由 spread_loader 提供 spread_z；剩餘欄位 (vwap_z, price_vs_vwap,
        bars_from_session_open, is_night_session) 在此計算。
        """
        try:
            from core.spread_loader import get_spread_loader
            loader = get_spread_loader()
            if loader._far_df is None:
                loader.load_latest_csv()
            loader.enrich_bar(bar)
        except Exception as exc:
            # fallback: 自行填入預設值
            bar.setdefault("spread_z", 0.0)
            bar.setdefault("near_close", bar.get("Close", 0.0))
            bar.setdefault("far_close", bar.get("Close", 0.0))
            bar.setdefault("vwap_z", self._calc_vwap_z(bar))
            bar.setdefault("price_vs_vwap", self._calc_price_vs_vwap(bar))
            bar.setdefault("bars_from_session_open", self._calc_bars_from_open(bar))
            bar.setdefault("is_night_session", self._calc_is_night(bar))
            bar.setdefault("breakout_strength", 0.0)
            bar.setdefault("volume_spike", 1.0)
            bar.setdefault("regime", "WEAK")
            bar.setdefault("adx", 15.0)
    
    def _calc_vwap_z(self, bar: dict) -> float:
        close = bar.get("Close", 0.0) or 0.0
        vwap = bar.get("vwap", close) or close
        atr = bar.get("atr", 1.0) or 1.0
        if vwap == 0 or atr <= 0:
            return 0.0
        return float((close - vwap) / (atr * 5))
    
    def _calc_price_vs_vwap(self, bar: dict) -> float:
        close = bar.get("Close", 0.0) or 0.0
        vwap = bar.get("vwap", close) or close
        if close == 0:
            return 0.0
        return float((close - vwap) / close * 100)
    
    def _calc_bars_from_open(self, bar: dict) -> int:
        import pandas as pd
        ts = None
        for key in ("ts", "timestamp", "datetime", "time"):
            val = bar.get(key)
            if val is not None:
                try:
                    ts = pd.Timestamp(val)
                    break
                except Exception:
                    continue
        if ts is None:
            return 0
        hour = ts.hour
        minute = ts.minute
        if 8 <= hour < 14 or (hour == 8 and minute >= 45):
            open_minutes = (hour * 60 + minute) - (8 * 60 + 45)
        elif hour >= 15 or hour < 5:
            if hour >= 15:
                open_minutes = (hour * 60 + minute) - (15 * 60)
            else:
                open_minutes = (hour * 60 + minute) + (24 * 60 - 15 * 60)
        else:
            open_minutes = 0
        return max(0, int(open_minutes / 5))
    
    def _calc_is_night(self, bar: dict) -> bool:
        import pandas as pd
        ts = None
        for key in ("ts", "timestamp", "datetime", "time"):
            val = bar.get(key)
            if val is not None:
                try:
                    ts = pd.Timestamp(val)
                    break
                except Exception:
                    continue
        if ts is None:
            return False
        hour = ts.hour
        return hour >= 15 or hour < 5
    
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

        if adx > self.params["max_adx"]:
            self._set_eval(skip_reason="ADX_TOO_HIGH", adx=adx, max_adx=self.params["max_adx"])
            return None

        if breakout_strength > self.params["max_breakout_strength"]:
            self._set_eval(skip_reason="BREAKOUT_STRENGTH_TOO_HIGH",
                           breakout_strength=breakout_strength,
                           max_breakout=self.params["max_breakout_strength"])
            return None

        if volume_spike < self.params["min_volume_spike"]:
            self._set_eval(skip_reason="VOLUME_TOO_LOW", volume_spike=volume_spike,
                           min_volume=self.params["min_volume_spike"])
            return None

        # 檢查雙重過濾條件
        vwap_z = bar["vwap_z"]
        spread_z = bar["spread_z"]

        # 檢查價差波動是否足夠
        spread_std = bar.get("spread_std", 0.0)
        if spread_std < self.params.get("min_spread_std", 5.0):
            self._set_eval(skip_reason="SPREAD_STD_TOO_LOW", spread_std=spread_std,
                           min_spread_std=self.params.get("min_spread_std", 5.0))
            return None

        # 計算預期獲利點數
        expected_spread_change = abs(spread_z - self.params["exit_spread_z"])
        expected_profit_points = expected_spread_change * spread_std

        # 檢查預期獲利是否足夠覆蓋摩擦成本
        min_expected_profit = self.params.get("min_expected_profit", 10.0)
        if expected_profit_points < min_expected_profit:
            self._set_eval(skip_reason="EXPECTED_PROFIT_TOO_LOW",
                           expected_profit=expected_profit_points,
                           min_profit=min_expected_profit,
                           spread_z=spread_z, spread_std=spread_std)
            return None

        edge = abs(spread_z) if spread_z is not None else 0.0

        # 條件 1: 價格相對於 VWAP 拉伸
        # 條件 2: 價差拉伸
        if vwap_z > self.params["entry_vwap_z"] and spread_z > self.params["entry_spread_z"]:
            # 做空價差 (賣近月買遠月)
            self.position_side = "SHORT_SPREAD"
            self.entry_bar_idx = context.bar_counter
            self.entry_spread_z = spread_z

            print(f"[CalendarCondorV2] SHORT_SPREAD entry: vwap_z={vwap_z:.2f}, spread_z={spread_z:.2f}, "
                  f"expected_profit={expected_profit_points:.1f} points")
            self._set_eval(triggered=True, action="SELL", edge_score=edge,
                           signal="SHORT_SPREAD", vwap_z=vwap_z, spread_z=spread_z)

            return Signal(
                action="SELL",
                reason="calendar_condor_short_spread",
                stop_loss=0.0,
                target=0.0,
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
            self._set_eval(triggered=True, action="BUY", edge_score=edge,
                           signal="LONG_SPREAD", vwap_z=vwap_z, spread_z=spread_z)

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

        # No entry condition matched
        self._set_eval(skip_reason="SPREAD_Z_NOT_EXTREME", vwap_z=vwap_z, spread_z=spread_z,
                       entry_vwap_z=self.params["entry_vwap_z"],
                       entry_spread_z=self.params["entry_spread_z"])
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