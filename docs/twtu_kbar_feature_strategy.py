from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import math
import pandas as pd


@dataclass
class SignalDecision:
    action: str  # BUY, SELL, EXIT_LONG, EXIT_SHORT, HOLD
    reason: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    size_mult: float = 1.0


class KbarFeatureStrategy:
    """
    Rule-based strategy for feature-enriched kbar snapshots.

    Designed for integration into a live system such as tw-trading-unified.
    It assumes the dataframe already contains engineered features like:
      - regime, adx, ema_200_up
      - bull_align / bear_align
      - bullish_align / bearish_align
      - macd_hist / macd_rising
      - mom_velo / score
      - recent_high / recent_low
      - atr / vwap / price_vs_vwap

    Default version is intentionally conservative and short-biased, because the
    sample data shared by the user was mostly WEAK / bearish aligned.
    """

    REQUIRED_COLUMNS = {
        "close", "high", "low", "atr", "vwap", "adx", "score",
        "regime", "bear_align", "bull_align", "bearish_align", "bullish_align",
        "macd_hist", "macd_rising", "mom_velo", "recent_high", "recent_low",
        "price_vs_vwap", "volume_spike",
    }

    def __init__(
        self,
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

    def validate_columns(self, df: pd.DataFrame) -> None:
        missing = self.REQUIRED_COLUMNS - set(df.columns)
        if missing:
            raise ValueError(f"Missing required columns: {sorted(missing)}")

    def preprocess(self, df: pd.DataFrame) -> pd.DataFrame:
        self.validate_columns(df)
        out = df.copy()
        if "timestamp" in out.columns:
            out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
        return out

    def compute_size_multiplier(self, row: pd.Series) -> float:
        """
        Position scaling based on conviction.
        Keep it simple and bounded for live use.
        """
        mult = 1.0

        adx = float(row.get("adx", 0) or 0)
        score = float(row.get("score", 0) or 0)
        volume_spike = bool(row.get("volume_spike", False))

        if adx >= 25:
            mult += 0.25
        if adx >= 30:
            mult += 0.25

        if score <= -80 or score >= 80:
            mult += 0.25
        elif score <= -50 or score >= 50:
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

    # ---------- regime filters ----------

    def is_short_environment_ok(self, row: pd.Series) -> bool:
        return (
            self.short_enabled
            and str(row.get("regime", "")).upper() in {"WEAK", "BEAR", "DOWN"}
            and bool(row.get("bear_align", False))
            and bool(row.get("bearish_align", False))
            and float(row.get("adx", 0) or 0) >= self.adx_threshold
            and float(row.get("close", 0) or 0) <= float(row.get("vwap", 0) or 0) + self.vwap_buffer
            and float(row.get("score", 0) or 0) <= self.score_short_threshold
        )

    def is_long_environment_ok(self, row: pd.Series) -> bool:
        return (
            self.long_enabled
            and str(row.get("regime", "")).upper() in {"STRONG", "BULL", "UP"}
            and bool(row.get("bull_align", False))
            and bool(row.get("bullish_align", False))
            and float(row.get("adx", 0) or 0) >= self.adx_threshold
            and float(row.get("close", 0) or 0) >= float(row.get("vwap", 0) or 0) - self.vwap_buffer
            and float(row.get("score", 0) or 0) >= self.score_long_threshold
        )

    # ---------- entry logic ----------

    def short_trigger_ok(self, row: pd.Series) -> bool:
        macd_hist = float(row.get("macd_hist", 0) or 0)
        mom_velo = float(row.get("mom_velo", 0) or 0)
        close = float(row.get("close", 0) or 0)
        recent_low = float(row.get("recent_low", 0) or 0)
        is_new_low = bool(row.get("is_new_low", False))

        momentum_ok = macd_hist < 0 and mom_velo < 0
        if not self.require_breakout:
            return momentum_ok
        return momentum_ok and (close < recent_low or is_new_low)

    def long_trigger_ok(self, row: pd.Series) -> bool:
        macd_hist = float(row.get("macd_hist", 0) or 0)
        macd_rising = bool(row.get("macd_rising", False))
        mom_velo = float(row.get("mom_velo", 0) or 0)
        close = float(row.get("close", 0) or 0)
        recent_high = float(row.get("recent_high", 0) or 0)
        is_new_high = bool(row.get("is_new_high", False))

        momentum_ok = macd_hist > 0 and macd_rising and mom_velo > 0
        if not self.require_breakout:
            return momentum_ok
        return momentum_ok and (close > recent_high or is_new_high)

    def generate_entry_signal(self, row: pd.Series) -> SignalDecision:
        close = float(row["close"])
        atr = float(row["atr"])
        if atr <= 0 or math.isnan(atr):
            return SignalDecision("HOLD", "ATR invalid")

        size_mult = self.compute_size_multiplier(row)

        if self.is_short_environment_ok(row) and self.short_trigger_ok(row):
            stop = close + self.stop_atr_mult * atr
            tp = close - self.take_profit_atr_mult * atr
            return SignalDecision(
                action="SELL",
                reason="bear regime + bearish alignment + momentum breakdown",
                stop_loss=stop,
                take_profit=tp,
                size_mult=size_mult,
            )

        if self.is_long_environment_ok(row) and self.long_trigger_ok(row):
            stop = close - self.stop_atr_mult * atr
            tp = close + self.take_profit_atr_mult * atr
            return SignalDecision(
                action="BUY",
                reason="bull regime + bullish alignment + momentum breakout",
                stop_loss=stop,
                take_profit=tp,
                size_mult=size_mult,
            )

        return SignalDecision("HOLD", "entry conditions not met")

    # ---------- exit logic ----------

    def generate_exit_signal(
        self,
        row: pd.Series,
        position_side: str,
        entry_price: float,
        bars_held: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> SignalDecision:
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        vwap = float(row["vwap"])
        macd_hist = float(row.get("macd_hist", 0) or 0)
        mom_velo = float(row.get("mom_velo", 0) or 0)

        side = position_side.upper()

        if side == "LONG":
            if stop_loss is not None and low <= stop_loss:
                return SignalDecision("EXIT_LONG", "long stop loss hit")
            if take_profit is not None and high >= take_profit:
                return SignalDecision("EXIT_LONG", "long take profit hit")
            if close < vwap and macd_hist < 0:
                return SignalDecision("EXIT_LONG", "lost VWAP and MACD turned negative")
            if mom_velo < 0:
                return SignalDecision("EXIT_LONG", "momentum velocity turned negative")
            if bars_held >= self.max_hold_bars:
                return SignalDecision("EXIT_LONG", "max hold bars reached")
            return SignalDecision("HOLD", "keep long")

        if side == "SHORT":
            if stop_loss is not None and high >= stop_loss:
                return SignalDecision("EXIT_SHORT", "short stop loss hit")
            if take_profit is not None and low <= take_profit:
                return SignalDecision("EXIT_SHORT", "short take profit hit")
            if close > vwap and macd_hist > 0:
                return SignalDecision("EXIT_SHORT", "reclaimed VWAP and MACD turned positive")
            if mom_velo > 0:
                return SignalDecision("EXIT_SHORT", "momentum velocity turned positive")
            if bars_held >= self.max_hold_bars:
                return SignalDecision("EXIT_SHORT", "max hold bars reached")
            return SignalDecision("HOLD", "keep short")

        return SignalDecision("HOLD", "unknown position side")

    # ---------- backtest-like signal scan ----------

    def scan_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self.preprocess(df)
        rows = []
        for _, row in df.iterrows():
            sig = self.generate_entry_signal(row)
            rows.append({
                "timestamp": row.get("timestamp"),
                "close": row.get("close"),
                "action": sig.action,
                "reason": sig.reason,
                "stop_loss": sig.stop_loss,
                "take_profit": sig.take_profit,
                "size_mult": sig.size_mult,
            })
        return pd.DataFrame(rows)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scan feature-enriched kbar CSV for trading signals")
    parser.add_argument("csv_path", help="Path to feature CSV")
    parser.add_argument("--equity", type=float, default=1_000_000)
    parser.add_argument("--long", action="store_true", help="Enable long entries")
    parser.add_argument("--no-short", action="store_true", help="Disable short entries")
    args = parser.parse_args()

    df = pd.read_csv(args.csv_path)
    strat = KbarFeatureStrategy(long_enabled=args.long, short_enabled=not args.no_short)
    result = strat.scan_signals(df)

    if not result.empty:
        print(result.to_string(index=False))

        actionable = result[result["action"].isin(["BUY", "SELL"])]
        if not actionable.empty:
            last = actionable.iloc[-1]
            entry = float(last["close"])
            stop = float(last["stop_loss"])
            qty = strat.calc_position_size(args.equity, entry, stop, float(last["size_mult"]))
            print("\nLast actionable signal:")
            print(last.to_string())
            print(f"Suggested qty by risk model: {qty}")
        else:
            print("\nNo actionable BUY/SELL signals found.")
