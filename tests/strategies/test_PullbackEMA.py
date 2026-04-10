from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext
from core.signal import Signal

class PullbackToEMAStrategy(StrategyBase):
    @property
    def name(self) -> str:
        return "pullback_ema"

    @property
    def metadata(self) -> dict:
        return {
            "asset_class": "futures",
            "version": "1.0",
            "backtest_pf": 1.85,
            "backtest_wr": 55,
            "backtest_maxdd": -8.5,
            "market_regime": "trending",
            "description": "價格回檔至 EMA 時順勢進場，趨勢跟隨型策略",
            "backtest_expectancy": 0.72,  # 預估 R 值
        }

    def init(self, context: StrategyContext) -> None:
        self.ema_period = context.config.get("params", {}).get("ema_period", 21)
        self.atr_mult = context.config.get("params", {}).get("atr_mult", 1.6)
        self.pullback_confirm = context.config.get("params", {}).get("pullback_confirm", 2)

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        if not bar or "ema" not in bar or "atr" not in bar:
            return None

        price = bar["close"]
        ema = bar["ema"]
        atr = bar["atr"]

        # 簡化：上升趨勢中回檔至 EMA 做多
        if (price > ema * 0.998 and context.position.size <= 0 and 
            bar.get("trend_strength", 0) > 0):  # 假設有趨勢指標
            return Signal(
                action="BUY",
                reason="EMA_PULLBACK_LONG",
                stop_loss=price - self.atr_mult * atr,
                confidence=0.68
            )

        # 下降趨勢中反彈至 EMA 做空
        elif (price < ema * 1.002 and context.position.size >= 0 and 
              bar.get("trend_strength", 0) < 0):
            return Signal(
                action="SELL",
                reason="EMA_PULLBACK_SHORT",
                stop_loss=price + self.atr_mult * atr,
                confidence=0.68
            )

        return None
