"""Vol-Squeeze — 順勢動能噴發策略."""
from __future__ import annotations

import logging
from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext

logger = logging.getLogger(__name__)

class VolSqueeze(StrategyBase):
    """捕捉 Squeeze 釋放後的順勢噴發機會。
    
    這是根據 TTM Squeeze 經典理論實作的插件：
    1. Sqz 狀態由 🔒 轉 🔓 (fired).
    2. 動能直方圖在零軸上方則買入，下方則賣出。
    3. 結合綜合評分 Score 作為品質過濾。
    """

    @property
    def name(self) -> str:
        return "vol_squeeze"

    @property
    def metadata(self) -> dict:
        return {
            "asset_class": "futures",
            "version": "1.0",
            "market_regime": "trending",
            "description": "順勢噴發: 捕捉 Squeeze 釋放後的動能爆發 (TTM 經典)",
            "indicators": ["squeeze", "atr"],
        }

    def init(self, context: StrategyContext) -> None:
        """初始化策略參數。"""
        # 這裡可以根據 config 設定一些快取的長度或本地變數
        params = context.config.get("params", {})
        self.entry_score = params.get("entry_score", 20)
        logger.info(f"VolSqueeze initialized with entry_score={self.entry_score}")

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        # ... (rest of implementation)
        entry_threshold = self.entry_score
        
        close = bar.get("Close", 0.0)
        fired = bar.get("fired", False)
        momentum = bar.get("momentum", 0.0)
        score = bar.get("score", 0.0)
        atr = bar.get("atr", 50.0)
        
        # ── 噴發進場條件 ──
        # 1. 必須是剛釋放的第一棒 (或最近幾棒，這裡取第一棒最精確)
        if not fired:
            return None
            
        # 2. 品質過濾：Score 必須達標
        if abs(score) < entry_threshold:
            return None
            
        # 3. 判斷方向並產生信號
        sl_pts = atr * 1.5 if atr > 0 else 60
        
        # 向上噴發
        if momentum > 0:
            return Signal(
                action="BUY",
                reason="VOL_SQZ_UP",
                stop_loss=close - sl_pts,
                target=close + sl_pts * 2,
                confidence=min(abs(score)/100, 1.0)
            )
            
        # 向下噴發
        elif momentum < 0:
            return Signal(
                action="SELL",
                reason="VOL_SQZ_DOWN",
                stop_loss=close + sl_pts,
                target=close - sl_pts * 2,
                confidence=min(abs(score)/100, 1.0)
            )

        return None

    def cleanup(self) -> None:
        """策略退出時的清理工作。"""
        logger.info("VolSqueeze shutting down...")
