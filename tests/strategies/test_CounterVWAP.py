from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext
from core.signal import Signal

class CounterVWAPStrategy(StrategyBase):
    @property
    def name(self) -> str:
        return "counter_vwap"

    @property
    def metadata(self) -> dict:
        return {
            "asset_class": "futures",
            "version": "2.0",
            "backtest_pf": 1.95,
            "backtest_wr": 40.7,
            "backtest_maxdd": -7.2,
            "market_regime": "ranging",
            "description": "價格大幅偏離 VWAP 時反向進場，適合盤整或低波動市場",
        }

    def init(self, context: StrategyContext) -> None:
        self.confirm_bars = context.config.get("params", {}).get("confirm_bars", 3)
        self.atr_sl_mult = context.config.get("params", {}).get("atr_sl_mult", 2.0)
        self.exit_on_vwap = context.config.get("params", {}).get("exit_on_vwap", True)
        self.consecutive_deviation = 0

    def on_bar(self, context: StrategyContext) -> Signal | None:
        market = context.market
        bar = market.last_bar
        if not bar or "vwap" not in bar or "atr" not in bar:
            return None

        price = bar["close"]
        vwap = bar["vwap"]
        atr = bar["atr"]

        deviation = (price - vwap) / atr if atr > 0 else 0

        # 計數連續偏離
        if abs(deviation) > 1.5:
            self.consecutive_deviation += 1
        else:
            self.consecutive_deviation = 0

        if self.consecutive_deviation < self.confirm_bars:
            return None

        signal = None
        reason = ""
        stop_loss = 0.0

        if deviation > 2.0 and context.position.size <= 0:  # 嚴重高估 → 做空
            signal = "SELL"
            reason = "COUNTER_VWAP_OVER"
            stop_loss = price + self.atr_sl_mult * atr
        elif deviation < -2.0 and context.position.size >= 0:  # 嚴重低估 → 做多
            signal = "BUY"
            reason = "COUNTER_VWAP_UNDER"
            stop_loss = price - self.atr_sl_mult * atr

        if signal:
            return Signal(
                action=signal,
                reason=reason,
                stop_loss=stop_loss,
                confidence=0.75
            )

        # 出場邏輯：回到 VWAP 附近
        if self.exit_on_vwap and context.position.size != 0:
            if (context.position.size > 0 and price > vwap * 0.998) or \
               (context.position.size < 0 and price < vwap * 1.002):
                return Signal("EXIT", "COUNTER_VWAP_REVERSION", stop_loss=0.0, confidence=0.8)

        return None
