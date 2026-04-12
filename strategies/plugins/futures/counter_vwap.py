"""Counter-VWAP — 反向均值回歸 (PF=1.95, 勝率 40.7%)."""
from __future__ import annotations

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext


class CounterVWAP(StrategyBase):
    """ELITE #1: Squeeze Fire 失敗後反向進場，VWAP 回歸出場。

    Optimizations (v3.0):
    - Regime filter: only trade when squeeze_on (ranging market)
    - Momentum threshold: |momentum| >= 30 for fire events
    - confirm_bars default: 5 → 7 (fewer but higher quality signals)

    Optimizations (v3.1):
    - Bias confirmation: momentum acceleration check (噴發偏向)
    - Confidence boost when Bias aligns with counter direction
    """

    @property
    def name(self) -> str:
        return "counter_vwap"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "3.1",
            "backtest_pf": 1.95,
            "backtest_wr": 40.7,
            "backtest_maxdd": -7.2,
            "market_regime": "ranging",
            "description": "反向均值回歸: 偵測 Squeeze Fire 失敗後反向進場 (含 Bias 確認)",
            "indicators": ["squeeze", "vwap"],
        }

    def init(self, context: StrategyContext) -> None:
        self._fire_pending_dir = 0
        self._fire_bar_idx = 0
        self._fire_high = 0.0
        self._fire_low = 0.0

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        params = context.config.get("params", {})
        confirm_bars = params.get("confirm_bars", 7)  # v3: 5 → 7
        atr_sl_mult = params.get("atr_sl_mult", 2.0)
        min_momentum = params.get("min_momentum", 30.0)  # v3: new filter

        close = bar.get("Close", 0.0)
        vwap = bar.get("vwap", close)
        atr = bar.get("atr", 200.0)
        fired = bar.get("fired", False)
        momentum = bar.get("momentum", 0.0)
        mom_velo = bar.get("mom_velo", 0.0)
        recent_high = bar.get("recent_high", close)
        recent_low = bar.get("recent_low", close)
        squeeze_on = bar.get("squeeze_on", False)
        bar_counter = context.bar_counter

        # ── Bias (噴發偏向): momentum acceleration check ──
        # Bias tells us if momentum is accelerating or decelerating
        df = context.market.df_5m
        momentum_prev = 0.0
        if df is not None and "momentum" in df.columns and len(df) >= 2:
            momentum_prev = df["momentum"].iloc[-2]
        bias = 0.0  # positive = bullish accel, negative = bearish accel
        if momentum_prev != 0:
            bias = momentum - momentum_prev

        if vwap <= 0:
            return None

        # ── Regime Filter: Require squeeze was active recently ────────
        # "fired" means squeeze just turned OFF, so squeeze_on is False at fire bar.
        # Check if squeeze was ON in the last 10 bars (squeeze preceded the fire).
        has_squeeze_col = df is not None and "sqz_on" in df.columns
        if has_squeeze_col and len(df) >= 12:
            recent_squeeze = df["sqz_on"].iloc[-12:-2].any()
        else:
            # Fallback: use squeeze_on from last_bar if available
            recent_squeeze = bar.get("squeeze_on", bar.get("sqz_on", True))

        if not recent_squeeze and self._fire_pending_dir == 0:
            return None

        # 新 Fire 事件 (with momentum threshold filter)
        if fired and self._fire_pending_dir == 0:
            if abs(momentum) < min_momentum:
                return None  # Weak fire, ignore
            self._fire_pending_dir = 1 if momentum > 0 else -1
            self._fire_bar_idx = bar_counter
            self._fire_high = close
            self._fire_low = close
            return None

        if self._fire_pending_dir == 0:
            return None

        # 更新極值
        self._fire_high = max(self._fire_high, close)
        self._fire_low = min(self._fire_low, close)
        bars_since = bar_counter - self._fire_bar_idx

        # 過期
        if bars_since > confirm_bars:
            self._fire_pending_dir = 0
            return None
        if bars_since < 1:
            return None

        # ── 失敗驗證 (未創新高/低 + 動能反轉 或 VWAP 拒絕) ──
        sl_pts = atr * atr_sl_mult if atr > 0 else 60

        # Bullish fire failed → COUNTER_SELL
        if self._fire_pending_dir == 1:
            no_new_high = close < recent_high
            velo_reversed = mom_velo <= 0
            vwap_reject = close < vwap
            if no_new_high and (velo_reversed or vwap_reject):
                self._fire_pending_dir = 0
                # Bias check: bearish accel confirms short entry
                conf = 0.80
                if bias < 0:  # Momentum accelerating downward
                    conf = 0.85  # Boost confidence
                return Signal("SELL", "COUNTER_VWAP", close + sl_pts,
                              target=vwap, confidence=conf)

        # Bearish fire failed → COUNTER_BUY
        elif self._fire_pending_dir == -1:
            no_new_low = close > recent_low
            velo_reversed = mom_velo >= 0
            vwap_reject = close > vwap
            if no_new_low and (velo_reversed or vwap_reject):
                self._fire_pending_dir = 0
                # Bias check: bullish accel confirms long entry
                conf = 0.80
                if bias > 0:  # Momentum accelerating upward
                    conf = 0.85  # Boost confidence
                return Signal("BUY", "COUNTER_VWAP", close - sl_pts,
                              target=vwap, confidence=conf)

        return None

    def cleanup(self) -> None:
        self._fire_pending_dir = 0
