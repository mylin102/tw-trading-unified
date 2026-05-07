"""Range Mean Reversion (Bollinger Bands + RSI) - PF=2.1, WinRate=48%."""
from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd
import numpy as np

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext
from core.strategy_eval import StrategyEval

logger = logging.getLogger(__name__)

class RangeMeanReversionV1(StrategyBase):
    """
    ELITE #4: 區間均值回歸策略。
    
    核心邏輯：
    1. 當價格觸及布林帶下軌 (BBL) 且 RSI 超賣時，進場做多。
    2. 當價格觸及布林帶上軌 (BBU) 且 RSI 超買時，進場做空。
    3. 趨勢過強時 (ADX > 35) 自動過濾，防止逆勢被套。
    """

    @property
    def name(self) -> str:
        return "range_mean_reversion_v1"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "1.0",
            "backtest_pf": 2.1,
            "backtest_wr": 48.0,
            "backtest_maxdd": -5.5,
            "market_regime": "ranging",
            "description": "區間均值回歸: 利用布林帶與 RSI 捕捉極端位階回歸 (含 ADX 趨勢過濾)",
            "indicators": ["bbands", "rsi", "adx"],
        }

    def init(self, context: StrategyContext) -> None:
        self._last_signal_bar = -1
        self._last_log_ts = 0.0

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        if bar is None or bar.get("Close") is None:
            return None

        # ── 1. 獲取參數 ──
        config = context.config or {}
        params = config.get("params", {})
        rsi_overbought = params.get("rsi_overbought", 70)
        rsi_oversold = params.get("rsi_oversold", 30)
        adx_trend_limit = params.get("adx_trend_limit", 35)
        
        # ── 2. 獲取指標 ──
        close = bar.get("Close", 0.0)
        bb_up = bar.get("bb_up", np.nan)
        bb_low = bar.get("bb_low", np.nan)
        rsi = bar.get("rsi", 50)
        adx = bar.get("adx", 20)
        
        # [Debug]
        if time.time() - self._last_log_ts > 60:
            print(f"[range_mean_reversion_v1] close={close:.0f} bb={bb_low:.0f}/{bb_up:.0f} rsi={rsi:.1f} adx={adx:.1f}", flush=True)
            self._last_log_ts = time.time()
        
        # ── 3. 趨勢過濾 ──
        # 如果趨勢太強，不做均值回歸
        if adx > adx_trend_limit:
            self._set_eval(skip_reason=f"TREND_TOO_STRONG (ADX={adx:.1f})")
            return None

        # ── 4. 進場邏輯 ──
        signal = None
        
        # 做多邏輯：價格破下軌 + RSI 低位
        if close <= bb_low and rsi <= rsi_oversold:
            signal = Signal(
                action="BUY",
                reason=f"BB_LOW_OVERSOLD (rsi={rsi:.1f})",
                stop_loss=close - 30,
                take_profit=bar.get("bb_mid", close + 60),
                confidence=0.75
            )
            
        # 做空邏輯：價格破上軌 + RSI 高位
        elif close >= bb_up and rsi >= rsi_overbought:
            signal = Signal(
                action="SELL",
                reason=f"BB_UP_OVERBOUGHT (rsi={rsi:.1f})",
                stop_loss=close + 30,
                take_profit=bar.get("bb_mid", close - 60),
                confidence=0.75
            )

        if signal:
            self._set_eval(action=signal.action, score=signal.confidence * 20)
            return signal

        self._set_eval(skip_reason="NO_EXTREME_LEVEL")
        return None
