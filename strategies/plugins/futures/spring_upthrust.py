"""Spring/Upthrust — 假突破反向 (PF=3.36, 33 筆交易)."""
from __future__ import annotations

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext
from strategies.futures.elite_strategies import is_spring_long_context_favorable


class SpringUpthrust(StrategyBase):
    """ELITE #2: BB 擠壓中假跌破/假突破後反向進場。
    
    使用 Monitor 預計算的指標 (bb_upper, bb_lower, sqz_on)，
    不重複計算，保持與 Monitor 一致的數據源。
    """

    @property
    def name(self) -> str:
        return "spring_upthrust"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "3.0",
            "backtest_pf": 3.36,
            "backtest_wr": 33.3,
            "backtest_maxdd": -10.1,
            "market_regime": "squeeze",
            "description": "假突破反向: Spring (假跌破做多) / Upthrust (假突破做空)",
            "indicators": ["squeeze", "atr"],
        }

    def init(self, context: StrategyContext) -> None:
        params = context.config.get("strategy", {}).get("spring_upthrust", {})
        self.atr_mult = params.get("atr_mult", 2.0)

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        if not bar:
            return None

        # 使用 Monitor 預計算的指標
        bb_upper = bar.get("bb_upper", 0.0)
        bb_lower = bar.get("bb_lower", 0.0)
        sqz_on = bar.get("squeeze_on", bar.get("sqz_on", False))
        close = bar.get("Close", 0.0)
        high = bar.get("High", 0.0)
        low = bar.get("Low", 0.0)
        atr = bar.get("atr", 200.0)
        atr_cap = 300
        if atr > atr_cap:
            atr = atr_cap
        stop_loss = atr * self.atr_mult if atr > 0 else 60

        # 必須在擠壓狀態中
        if not sqz_on:
            return None

        # Spring: 假跌破 BB 下軌 → 做多（收盤彈回）
        if low < bb_lower and close > bb_lower and context.position.size <= 0:
            if not is_spring_long_context_favorable(bar):
                return None
            return Signal(
                "BUY",
                "SPRING",
                stop_loss=close - stop_loss,
                target=bar.get("bb_mid", close),
                confidence=0.70,
            )

        # Upthrust: 假突破 BB 上軌 → 做空（收盤跌回）
        if high > bb_upper and close < bb_upper and context.position.size >= 0:
            return Signal(
                "SELL",
                "UPTHRUST",
                stop_loss=close + stop_loss,
                target=bar.get("bb_mid", close),
                confidence=0.70,
            )

        return None

    def cleanup(self) -> None:
        pass
