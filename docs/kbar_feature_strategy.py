
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

import math
import pandas as pd


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass
class Signal:
    action: str
    side: Side
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    reason: str = ""
    score: float = 0.0


class KbarFeatureStrategy:
    """
    A practical feature-based intraday strategy using precomputed kbar indicators.

    Design:
    - regime filter
    - direction filter
    - pullback + continuation trigger
    - ATR-based risk model

    Expected columns include:
    atr, adx, bull_align, bearish_align, bear_align, bullish_align,
    ema_200_up, in_bull_pb_zone, in_bear_pb_zone, macd_hist, macd_rising,
    momentum, mom_velo, close, open, vwap, price_vs_vwap,
    recent_high, recent_low, is_new_high, is_new_low, score, regime.
    """

    def __init__(self) -> None:
        self.allow_long_regimes = {"NORMAL", "STRONG"}
        self.allow_short_regimes = {"WEAK", "NORMAL"}

        self.min_adx = 18.0
        self.long_score_threshold = 20.0
        self.short_score_threshold = -20.0

        self.stop_atr_mult = 1.2
        self.target_atr_mult = 2.0
        self.breakeven_trigger_atr = 1.0
        self.trail_trigger_atr = 1.5
        self.trail_atr_mult = 1.0

    @staticmethod
    def _to_bool(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if pd.isna(v):
            return False
        if isinstance(v, str):
            return v.strip().lower() == "true"
        return bool(v)

    @staticmethod
    def _safe_float(v: Any, default: float = 0.0) -> float:
        try:
            if pd.isna(v):
                return default
            return float(v)
        except Exception:
            return default

    def _base_checks(self, row: pd.Series) -> Dict[str, Any]:
        return {
            "atr": max(self._safe_float(row.get("atr"), 0.0), 1e-9),
            "adx": self._safe_float(row.get("adx"), 0.0),
            "score": self._safe_float(row.get("score"), 0.0),
            "close": self._safe_float(row.get("close"), 0.0),
            "open": self._safe_float(row.get("open"), 0.0),
            "vwap": self._safe_float(row.get("vwap"), 0.0),
            "price_vs_vwap": self._safe_float(row.get("price_vs_vwap"), 0.0),
            "recent_high": self._safe_float(row.get("recent_high"), 0.0),
            "recent_low": self._safe_float(row.get("recent_low"), 0.0),
            "macd_hist": self._safe_float(row.get("macd_hist"), 0.0),
            "mom_velo": self._safe_float(row.get("mom_velo"), 0.0),
            "regime": str(row.get("regime", "")),
            "ema_200_up": self._to_bool(row.get("ema_200_up")),
            "bull_align": self._to_bool(row.get("bull_align")),
            "bear_align": self._to_bool(row.get("bear_align")),
            "bullish_align": self._to_bool(row.get("bullish_align")),
            "bearish_align": self._to_bool(row.get("bearish_align")),
            "in_bull_pb_zone": self._to_bool(row.get("in_bull_pb_zone")),
            "in_bear_pb_zone": self._to_bool(row.get("in_bear_pb_zone")),
            "macd_rising": self._to_bool(row.get("macd_rising")),
            "is_new_high": self._to_bool(row.get("is_new_high")),
            "is_new_low": self._to_bool(row.get("is_new_low")),
            "sqz_on": self._to_bool(row.get("sqz_on")),
        }

    def long_signal(self, row: pd.Series) -> Signal:
        x = self._base_checks(row)

        regime_ok = (
            x["ema_200_up"]
            and x["bull_align"]
            and x["bullish_align"]
            and x["regime"] in self.allow_long_regimes
            and x["adx"] >= self.min_adx
        )
        if not regime_ok:
            return Signal("HOLD", Side.FLAT, reason="long regime filter failed", score=x["score"])

        pullback_ok = (
            x["in_bull_pb_zone"]
            or x["close"] <= self._safe_float(row.get("ema_fast"), x["close"]) + 0.25 * x["atr"]
        ) and x["price_vs_vwap"] > -0.003 and x["close"] >= x["vwap"]

        trigger_ok = (
            x["macd_rising"]
            and x["macd_hist"] > 0
            and x["mom_velo"] > 0
            and x["close"] > x["open"]
            and (
                x["is_new_high"]
                or x["close"] > x["recent_high"]
                or self._safe_float(row.get("breakout_strength"), 0.0) >= 1
            )
        )

        if pullback_ok and trigger_ok and x["score"] >= self.long_score_threshold:
            entry = x["close"]
            stop = entry - self.stop_atr_mult * x["atr"]
            target = entry + self.target_atr_mult * x["atr"]
            return Signal("ENTER", Side.LONG, entry, stop, target, "long pullback continuation", x["score"])

        return Signal("HOLD", Side.FLAT, reason="long trigger not ready", score=x["score"])

    def short_signal(self, row: pd.Series) -> Signal:
        x = self._base_checks(row)

        regime_ok = (
            (not x["ema_200_up"])
            and x["bear_align"]
            and x["bearish_align"]
            and x["regime"] in self.allow_short_regimes
            and x["adx"] >= self.min_adx
        )
        if not regime_ok:
            return Signal("HOLD", Side.FLAT, reason="short regime filter failed", score=x["score"])

        pullback_ok = (
            x["in_bear_pb_zone"]
            or x["close"] >= self._safe_float(row.get("ema_fast"), x["close"]) - 0.25 * x["atr"]
        ) and x["price_vs_vwap"] < 0.003 and x["close"] <= x["vwap"]

        trigger_ok = (
            (not x["macd_rising"])
            and x["macd_hist"] < 0
            and x["mom_velo"] < 0
            and x["close"] < x["open"]
            and (
                x["is_new_low"]
                or x["close"] < x["recent_low"]
                or self._safe_float(row.get("breakout_strength"), 0.0) <= -1
            )
        )

        if pullback_ok and trigger_ok and x["score"] <= self.short_score_threshold:
            entry = x["close"]
            stop = entry + self.stop_atr_mult * x["atr"]
            target = entry - self.target_atr_mult * x["atr"]
            return Signal("ENTER", Side.SHORT, entry, stop, target, "short pullback continuation", x["score"])

        return Signal("HOLD", Side.FLAT, reason="short trigger not ready", score=x["score"])

    def generate_signal(self, row: pd.Series, allow_long: bool = True, allow_short: bool = True) -> Signal:
        candidates = []
        if allow_long:
            candidates.append(self.long_signal(row))
        if allow_short:
            candidates.append(self.short_signal(row))

        enters = [s for s in candidates if s.action == "ENTER"]
        if not enters:
            return Signal("HOLD", Side.FLAT, reason="no qualified signal")
        if len(enters) == 1:
            return enters[0]

        # If both sides somehow trigger, choose the one with larger absolute score.
        enters.sort(key=lambda s: abs(s.score), reverse=True)
        return enters[0]

    def manage_position(
        self,
        position_side: Side,
        entry_price: float,
        current_row: pd.Series,
        bars_in_trade: int,
        highest_since_entry: Optional[float] = None,
        lowest_since_entry: Optional[float] = None,
    ) -> Signal:
        x = self._base_checks(current_row)
        atr = x["atr"]
        close = x["close"]
        vwap = x["vwap"]
        macd_hist = x["macd_hist"]

        if position_side == Side.LONG:
            hard_stop = entry_price - self.stop_atr_mult * atr
            target = entry_price + self.target_atr_mult * atr
            if close <= hard_stop:
                return Signal("EXIT", Side.LONG, close, reason="long hard stop")
            if close >= target:
                return Signal("EXIT", Side.LONG, close, reason="long target hit")
            if close - entry_price >= self.breakeven_trigger_atr * atr and close < entry_price + 0.2 * atr:
                return Signal("EXIT", Side.LONG, close, reason="long failed breakeven hold")
            if macd_hist < 0 and close < vwap:
                return Signal("EXIT", Side.LONG, close, reason="long momentum failure")
            if bars_in_trade >= 5 and close - entry_price < 0.5 * atr:
                return Signal("EXIT", Side.LONG, close, reason="long time stop")
            return Signal("HOLD", Side.LONG, reason="long position active")

        if position_side == Side.SHORT:
            hard_stop = entry_price + self.stop_atr_mult * atr
            target = entry_price - self.target_atr_mult * atr
            if close >= hard_stop:
                return Signal("EXIT", Side.SHORT, close, reason="short hard stop")
            if close <= target:
                return Signal("EXIT", Side.SHORT, close, reason="short target hit")
            if entry_price - close >= self.breakeven_trigger_atr * atr and close > entry_price - 0.2 * atr:
                return Signal("EXIT", Side.SHORT, close, reason="short failed breakeven hold")
            if macd_hist > 0 and close > vwap:
                return Signal("EXIT", Side.SHORT, close, reason="short momentum failure")
            if bars_in_trade >= 5 and entry_price - close < 0.5 * atr:
                return Signal("EXIT", Side.SHORT, close, reason="short time stop")
            return Signal("HOLD", Side.SHORT, reason="short position active")

        return Signal("HOLD", Side.FLAT, reason="flat")


def run_strategy_on_dataframe(df: pd.DataFrame, allow_long: bool = True, allow_short: bool = True) -> pd.DataFrame:
    strategy = KbarFeatureStrategy()
    outputs = []

    for _, row in df.iterrows():
        sig = strategy.generate_signal(row, allow_long=allow_long, allow_short=allow_short)
        outputs.append({
            "timestamp": row.get("timestamp"),
            "close": row.get("close"),
            "signal_action": sig.action,
            "signal_side": sig.side.value,
            "entry_price": sig.entry_price,
            "stop_price": sig.stop_price,
            "target_price": sig.target_price,
            "reason": sig.reason,
            "signal_score": sig.score,
        })

    return pd.DataFrame(outputs)


if __name__ == "__main__":
    path = "/mnt/data/2026-04-21T19-22_export.csv"
    df = pd.read_csv(path)

    # Suggested first deployment mode based on the uploaded sample:
    # mostly bearish environment => short-only.
    result = run_strategy_on_dataframe(df, allow_long=False, allow_short=True)
    print(result.to_string(index=False))
