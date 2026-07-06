from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

import math
import pandas as pd


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    EXIT_LONG = "EXIT_LONG"
    EXIT_SHORT = "EXIT_SHORT"
    HOLD = "HOLD"


@dataclass
class SignalDecision:
    action: SignalAction
    reason: str
    symbol: Optional[str] = None
    strategy_name: str = "kbar_feature_v1"
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    size_mult: float = 1.0
    meta: Optional[Dict[str, Any]] = None


@dataclass
class PositionSnapshot:
    symbol: str
    side: str  # LONG / SHORT / FLAT
    qty: int = 0
    avg_price: float = 0.0
    bars_held: int = 0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None

    @property
    def is_flat(self) -> bool:
        return self.side.upper() == "FLAT" or self.qty == 0


class KbarFeatureStrategy:
    """
    Repo-style rule strategy module.

    Responsibility boundary:
    - Input: a feature-enriched latest bar row + current position snapshot
    - Output: one clean decision object
    - Does NOT submit orders
    - Does NOT track broker order state
    """

    NAME = "kbar_feature_v1"
    REQUIRED_COLUMNS = {
        "close", "high", "low", "atr", "vwap", "adx", "score",
        "regime", "bear_align", "bull_align", "bearish_align", "bullish_align",
        "macd_hist", "macd_rising", "mom_velo", "recent_high", "recent_low",
        "price_vs_vwap", "volume_spike",
    }

    def __init__(
        self,
        symbol: str,
        adx_threshold: float = 20.0,
        stop_atr_mult: float = 1.2,
        take_profit_atr_mult: float = 2.0,
        max_hold_bars: int = 12,
        long_enabled: bool = False,
        short_enabled: bool = True,
        require_breakout: bool = True,
        vwap_buffer: float = 0.0,
        risk_per_trade: float = 0.005,
        score_short_threshold: float = -20.0,
        score_long_threshold: float = 20.0,
    ) -> None:
        self.symbol = symbol
        self.adx_threshold = adx_threshold
        self.stop_atr_mult = stop_atr_mult
        self.take_profit_atr_mult = take_profit_atr_mult
        self.max_hold_bars = max_hold_bars
        self.long_enabled = long_enabled
        self.short_enabled = short_enabled
        self.require_breakout = require_breakout
        self.vwap_buffer = vwap_buffer
        self.risk_per_trade = risk_per_trade
        self.score_short_threshold = score_short_threshold
        self.score_long_threshold = score_long_threshold

    def validate_row(self, row: pd.Series) -> None:
        missing = self.REQUIRED_COLUMNS - set(row.index)
        if missing:
            raise ValueError(f"Missing required columns: {sorted(missing)}")

    def _size_mult(self, row: pd.Series) -> float:
        mult = 1.0
        adx = float(row.get("adx", 0) or 0)
        score = float(row.get("score", 0) or 0)
        volume_spike = bool(row.get("volume_spike", False))

        if adx >= 25:
            mult += 0.25
        if adx >= 30:
            mult += 0.25
        if abs(score) >= 80:
            mult += 0.25
        elif abs(score) >= 50:
            mult += 0.10
        if volume_spike:
            mult += 0.10
        return min(mult, 1.75)

    def calc_position_size(self, equity: float, entry: float, stop: float, size_mult: float = 1.0) -> int:
        risk_amount = equity * self.risk_per_trade * size_mult
        per_unit_risk = abs(entry - stop)
        if per_unit_risk <= 0:
            return 0
        return max(int(risk_amount / per_unit_risk), 0)

    def _short_env_ok(self, row: pd.Series) -> bool:
        return (
            self.short_enabled
            and str(row.get("regime", "")).upper() in {"WEAK", "BEAR", "DOWN"}
            and bool(row.get("bear_align", False))
            and bool(row.get("bearish_align", False))
            and float(row.get("adx", 0) or 0) >= self.adx_threshold
            and float(row.get("close", 0) or 0) <= float(row.get("vwap", 0) or 0) + self.vwap_buffer
            and float(row.get("score", 0) or 0) <= self.score_short_threshold
        )

    def _long_env_ok(self, row: pd.Series) -> bool:
        return (
            self.long_enabled
            and str(row.get("regime", "")).upper() in {"STRONG", "BULL", "UP"}
            and bool(row.get("bull_align", False))
            and bool(row.get("bullish_align", False))
            and float(row.get("adx", 0) or 0) >= self.adx_threshold
            and float(row.get("close", 0) or 0) >= float(row.get("vwap", 0) or 0) - self.vwap_buffer
            and float(row.get("score", 0) or 0) >= self.score_long_threshold
        )

    def _short_trigger_ok(self, row: pd.Series) -> bool:
        macd_hist = float(row.get("macd_hist", 0) or 0)
        mom_velo = float(row.get("mom_velo", 0) or 0)
        close = float(row.get("close", 0) or 0)
        recent_low = float(row.get("recent_low", 0) or 0)
        is_new_low = bool(row.get("is_new_low", False))
        momentum_ok = macd_hist < 0 and mom_velo < 0
        return momentum_ok if not self.require_breakout else momentum_ok and (close < recent_low or is_new_low)

    def _long_trigger_ok(self, row: pd.Series) -> bool:
        macd_hist = float(row.get("macd_hist", 0) or 0)
        macd_rising = bool(row.get("macd_rising", False))
        mom_velo = float(row.get("mom_velo", 0) or 0)
        close = float(row.get("close", 0) or 0)
        recent_high = float(row.get("recent_high", 0) or 0)
        is_new_high = bool(row.get("is_new_high", False))
        momentum_ok = macd_hist > 0 and macd_rising and mom_velo > 0
        return momentum_ok if not self.require_breakout else momentum_ok and (close > recent_high or is_new_high)

    def generate_entry_signal(self, row: pd.Series, position: PositionSnapshot) -> SignalDecision:
        self.validate_row(row)
        if not position.is_flat:
            return SignalDecision(
                action=SignalAction.HOLD,
                reason="position already open",
                symbol=self.symbol,
                strategy_name=self.NAME,
            )

        close = float(row["close"])
        atr = float(row["atr"])
        if atr <= 0 or math.isnan(atr):
            return SignalDecision(SignalAction.HOLD, "ATR invalid", symbol=self.symbol, strategy_name=self.NAME)

        size_mult = self._size_mult(row)

        if self._short_env_ok(row) and self._short_trigger_ok(row):
            stop = close + self.stop_atr_mult * atr
            tp = close - self.take_profit_atr_mult * atr
            return SignalDecision(
                action=SignalAction.SELL,
                reason="bear regime + bearish alignment + downside momentum",
                symbol=self.symbol,
                strategy_name=self.NAME,
                stop_loss=stop,
                take_profit=tp,
                size_mult=size_mult,
                meta={"side": "SHORT", "entry_type": "breakdown_continuation"},
            )

        if self._long_env_ok(row) and self._long_trigger_ok(row):
            stop = close - self.stop_atr_mult * atr
            tp = close + self.take_profit_atr_mult * atr
            return SignalDecision(
                action=SignalAction.BUY,
                reason="bull regime + bullish alignment + upside momentum",
                symbol=self.symbol,
                strategy_name=self.NAME,
                stop_loss=stop,
                take_profit=tp,
                size_mult=size_mult,
                meta={"side": "LONG", "entry_type": "breakout_continuation"},
            )

        return SignalDecision(
            action=SignalAction.HOLD,
            reason="entry conditions not met",
            symbol=self.symbol,
            strategy_name=self.NAME,
        )

    def generate_exit_signal(self, row: pd.Series, position: PositionSnapshot) -> SignalDecision:
        self.validate_row(row)
        if position.is_flat:
            return SignalDecision(SignalAction.HOLD, "no open position", symbol=self.symbol, strategy_name=self.NAME)

        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        vwap = float(row["vwap"])
        macd_hist = float(row.get("macd_hist", 0) or 0)
        mom_velo = float(row.get("mom_velo", 0) or 0)
        side = position.side.upper()

        if side == "LONG":
            if position.stop_loss is not None and low <= position.stop_loss:
                return SignalDecision(SignalAction.EXIT_LONG, "long stop hit", symbol=self.symbol, strategy_name=self.NAME)
            if position.take_profit is not None and high >= position.take_profit:
                return SignalDecision(SignalAction.EXIT_LONG, "long take profit hit", symbol=self.symbol, strategy_name=self.NAME)
            if close < vwap and macd_hist < 0:
                return SignalDecision(SignalAction.EXIT_LONG, "lost VWAP and MACD turned negative", symbol=self.symbol, strategy_name=self.NAME)
            if mom_velo < 0:
                return SignalDecision(SignalAction.EXIT_LONG, "long momentum reversal", symbol=self.symbol, strategy_name=self.NAME)
            if position.bars_held >= self.max_hold_bars:
                return SignalDecision(SignalAction.EXIT_LONG, "max hold bars reached", symbol=self.symbol, strategy_name=self.NAME)

        if side == "SHORT":
            if position.stop_loss is not None and high >= position.stop_loss:
                return SignalDecision(SignalAction.EXIT_SHORT, "short stop hit", symbol=self.symbol, strategy_name=self.NAME)
            if position.take_profit is not None and low <= position.take_profit:
                return SignalDecision(SignalAction.EXIT_SHORT, "short take profit hit", symbol=self.symbol, strategy_name=self.NAME)
            if close > vwap and macd_hist > 0:
                return SignalDecision(SignalAction.EXIT_SHORT, "reclaimed VWAP and MACD turned positive", symbol=self.symbol, strategy_name=self.NAME)
            if mom_velo > 0:
                return SignalDecision(SignalAction.EXIT_SHORT, "short momentum reversal", symbol=self.symbol, strategy_name=self.NAME)
            if position.bars_held >= self.max_hold_bars:
                return SignalDecision(SignalAction.EXIT_SHORT, "max hold bars reached", symbol=self.symbol, strategy_name=self.NAME)

        return SignalDecision(SignalAction.HOLD, "hold position", symbol=self.symbol, strategy_name=self.NAME)

    def evaluate(self, row: pd.Series, position: PositionSnapshot) -> SignalDecision:
        """
        Single entry point for main loop.
        Flat -> entry evaluation
        Non-flat -> exit evaluation
        """
        if position.is_flat:
            return self.generate_entry_signal(row, position)
        return self.generate_exit_signal(row, position)
