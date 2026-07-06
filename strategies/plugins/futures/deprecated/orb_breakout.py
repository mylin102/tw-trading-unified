"""Opening Range Breakout — 開盤區間突破 (PF=1.78, WR=52%)."""
from __future__ import annotations

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext


class OpeningRangeBreakout(StrategyBase):
    """開盤前 N 根 bar 計算區間，突破高點做多/跌破低點做空。
    適合趨勢/高波動市場，和 Counter-VWAP（盤整）互補。
    
    每天開盤自動重置區間，避免跨日殘留。
    """

    @property
    def name(self) -> str:
        return "orb_breakout"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "2.0",
            "backtest_pf": 1.78,
            "backtest_wr": 52.0,
            "backtest_maxdd": -8.4,
            "market_regime": "trending",
            "description": "開盤區間突破: 適合趨勢/高波動市場",
            "indicators": ["atr"],
        }

    def init(self, context: StrategyContext) -> None:
        params = context.config.get("params", {})
        self.range_bars = params.get("range_bars", 6)  # ~30 min (5m bars)
        self.atr_mult = params.get("atr_mult", 1.5)
        self._reset_state()

    def _reset_state(self):
        self._range_high = 0.0
        self._range_low = float('inf')
        self._bar_count = 0
        self._range_built = False
        self._signaled = False
        self._last_session = None

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        if not bar:
            self._set_eval(skip_reason="NO_BAR")
            return None

        close = bar.get("Close", 0.0)
        high = bar.get("High", 0.0)
        low = bar.get("Low", 0.0)
        atr = bar.get("atr", 200.0)
        session = bar.get("session", 1)  # 1=day, 2=night

        # Reset range on new session/day
        current_session = bar.get("trading_day", session)
        if self._last_session != current_session:
            self._reset_state()
            self._last_session = current_session

        # Build opening range
        if not self._range_built:
            self._range_high = max(self._range_high, high)
            self._range_low = min(self._range_low, low)
            self._bar_count += 1
            if self._bar_count >= self.range_bars:
                self._range_built = True
            self._set_eval(skip_reason="ORB_BUILDING", bars=self._bar_count)
            return None

        if self._signaled:
            self._set_eval(skip_reason="ALREADY_SIGNALED")
            return None
        
        if context.position.size != 0:
            self._set_eval(skip_reason="POSITION_OPEN", position=context.position.size)
            return None

        # Breakout logic
        if close > self._range_high:
            self._signaled = True
            self._set_eval(triggered=True, action="BUY", reason="ORB_UP_BREAKOUT")
            return Signal(
                "BUY",
                "ORB_UP_BREAKOUT",
                stop_loss=self._range_low - atr * self.atr_mult,
                target=self._range_high + (self._range_high - self._range_low),
                confidence=0.68,
            )

        if close < self._range_low:
            self._signaled = True
            self._set_eval(triggered=True, action="SELL", reason="ORB_DOWN_BREAKOUT")
            return Signal(
                "SELL",
                "ORB_DOWN_BREAKOUT",
                stop_loss=self._range_high + atr * self.atr_mult,
                target=self._range_low - (self._range_high - self._range_low),
                confidence=0.68,
            )

        self._set_eval(skip_reason="NO_BREAKOUT", close=close, range=[self._range_low, self._range_high])
        return None

    def cleanup(self) -> None:
        self._reset_state()
