from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(slots=True)
class CalendarCondorParamsV2:
    # Entry
    entry_vwap_z: float = 2.0
    entry_spread_z: float = 2.0
    max_adx: float = 25.0
    max_breakout_strength: float = 0.50
    max_volume_spike: float = 1.2
    continuation_price_vs_vwap: float = 0.004
    allow_squeeze: bool = False
    allow_night_session: bool = False
    min_bars_from_session_open: int = 6

    # Exit
    take_profit_vwap_z: float = 0.5
    take_profit_vwap_z_extended: float = 0.3
    soft_exit_confirm_vwap_z: float = 1.0
    soft_exit_confirm_adx: float = 25.0
    soft_exit_confirm_breakout: float = 0.50

    spread_stop_mult: float = 1.0
    max_holding_bars: int = 8
    tp_delay_bars: int = 2
    peak_pnl_retention: float = 0.80  # exit if current pnl < peak_pnl * retention

    # Partial exit
    enable_partial_exit: bool = False
    partial_exit_fraction: float = 0.50
    partial_exit_vwap_z: float = 0.5
    final_exit_vwap_z: float = 0.3


@dataclass(slots=True)
class CalendarCondorSignalV2:
    side: str
    signal_type: str
    reason: str
    near_action: str
    far_action: str
    meta: dict[str, Any]


class CalendarCondorStrategyV2:
    """
    Research / backtest-first calendar spread mean-reversion strategy.

    v2 adds:
    - hard exits that must not be delayed
    - soft take-profit optimization using time buffer
    - extended take-profit price buffer
    - peak-PnL giveback control
    - optional partial exit support

    This is NOT a true iron condor. It is a near/far month calendar spread analog.

    Expected context fields (dict-like or attribute-like):

    Entry-related:
    - regime
    - adx
    - breakout_strength
    - volume_spike
    - price_vs_vwap
    - vwap_z
    - spread_z
    - bars_from_session_open
    - is_night_session

    Position / exit-related:
    - in_position
    - position_side                    # "SELL_NEAR_BUY_FAR" / "BUY_NEAR_SELL_FAR"
    - hold_bars
    - adverse_spread_move
    - spread_atr_proxy
    - current_pnl
    - peak_unrealized_pnl
    - tp_delay_used                    # optional state counter
    - partial_exit_done                # optional state flag
    """

    name = "calendar_condor"

    def __init__(self, params: Optional[CalendarCondorParamsV2] = None) -> None:
        self.params = params or CalendarCondorParamsV2()

    def supports_regime(self, regime: str) -> bool:
        return regime == "WEAK"

    def on_bar(self, context: Any) -> Optional[CalendarCondorSignalV2]:
        if self._get(context, "in_position", False):
            return self._check_exit(context)
        return self._check_entry(context)

    def _check_entry(self, context: Any) -> Optional[CalendarCondorSignalV2]:
        regime = self._get(context, "regime", "UNKNOWN")
        adx = float(self._get(context, "adx", 999.0))
        breakout_strength = float(self._get(context, "breakout_strength", 999.0))
        volume_spike = float(self._get(context, "volume_spike", 999.0))
        vwap_z = self._float_or_none(self._get(context, "vwap_z"))
        spread_z = self._float_or_none(self._get(context, "spread_z"))
        bars_from_open = int(self._get(context, "bars_from_session_open", 9999))
        is_night = bool(self._get(context, "is_night_session", False))
        price_vs_vwap_abs = abs(float(self._get(context, "price_vs_vwap", 0.0)))

        if regime != "WEAK":
            return None
        if adx >= self.params.max_adx:
            return None
        if breakout_strength >= self.params.max_breakout_strength:
            return None
        if volume_spike >= self.params.max_volume_spike:
            return None
        if is_night and not self.params.allow_night_session:
            return None
        if bars_from_open < self.params.min_bars_from_session_open:
            return None
        if vwap_z is None or spread_z is None:
            return None

        # Avoid already-runaway conditions
        if price_vs_vwap_abs > self.params.continuation_price_vs_vwap * 1.25:
            return None

        # Fade overheated upside: sell near / buy far
        if vwap_z >= self.params.entry_vwap_z and spread_z >= self.params.entry_spread_z:
            return CalendarCondorSignalV2(
                side="SELL_NEAR_BUY_FAR",
                signal_type="CALENDAR_CONDOR_SHORT_NEAR",
                reason="vwap_high_and_spread_high",
                near_action="SELL",
                far_action="BUY",
                meta={
                    "regime": regime,
                    "adx": adx,
                    "breakout_strength": breakout_strength,
                    "volume_spike": volume_spike,
                    "vwap_z": vwap_z,
                    "spread_z": spread_z,
                },
            )

        # Fade oversold downside: buy near / sell far
        if vwap_z <= -self.params.entry_vwap_z and spread_z <= -self.params.entry_spread_z:
            return CalendarCondorSignalV2(
                side="BUY_NEAR_SELL_FAR",
                signal_type="CALENDAR_CONDOR_LONG_NEAR",
                reason="vwap_low_and_spread_low",
                near_action="BUY",
                far_action="SELL",
                meta={
                    "regime": regime,
                    "adx": adx,
                    "breakout_strength": breakout_strength,
                    "volume_spike": volume_spike,
                    "vwap_z": vwap_z,
                    "spread_z": spread_z,
                },
            )

        return None

    def _check_exit(self, context: Any) -> Optional[CalendarCondorSignalV2]:
        """
        Exit hierarchy:
        1. Hard trend / continuation exit
        2. Spread stop
        3. Session/event exit hook (if caller sets force_exit)
        4. Time stop
        5. Soft take-profit optimization
        6. Extended TP / optional partial exit
        """
        regime = self._get(context, "regime", "UNKNOWN")
        adx = float(self._get(context, "adx", 0.0))
        breakout_strength = float(self._get(context, "breakout_strength", 0.0))
        hold_bars = int(self._get(context, "hold_bars", 0))
        price_vs_vwap = abs(float(self._get(context, "price_vs_vwap", 0.0)))
        vwap_z = self._float_or_none(self._get(context, "vwap_z"))
        adverse_spread_move = float(self._get(context, "adverse_spread_move", 0.0))
        spread_atr_proxy = float(self._get(context, "spread_atr_proxy", 0.0))
        position_side = str(self._get(context, "position_side", "UNKNOWN"))

        current_pnl = float(self._get(context, "current_pnl", 0.0))
        peak_unrealized_pnl = float(self._get(context, "peak_unrealized_pnl", current_pnl))
        tp_delay_used = int(self._get(context, "tp_delay_used", 0))
        partial_exit_done = bool(self._get(context, "partial_exit_done", False))
        force_exit = bool(self._get(context, "force_exit", False))

        # ---------------------------
        # 1) Hard exits: not delayable
        # ---------------------------
        if regime == "TREND" or breakout_strength >= 0.60 or adx >= 30:
            return self._exit_signal(
                reason="trend_exit",
                position_side=position_side,
                regime=regime,
                adx=adx,
                breakout_strength=breakout_strength,
                hold_bars=hold_bars,
                price_vs_vwap=price_vs_vwap,
                vwap_z=vwap_z,
                current_pnl=current_pnl,
                peak_unrealized_pnl=peak_unrealized_pnl,
            )

        if price_vs_vwap > self.params.continuation_price_vs_vwap:
            return self._exit_signal(
                reason="vwap_continuation_exit",
                position_side=position_side,
                regime=regime,
                adx=adx,
                breakout_strength=breakout_strength,
                hold_bars=hold_bars,
                price_vs_vwap=price_vs_vwap,
                vwap_z=vwap_z,
                current_pnl=current_pnl,
                peak_unrealized_pnl=peak_unrealized_pnl,
            )

        if spread_atr_proxy > 0 and adverse_spread_move > self.params.spread_stop_mult * spread_atr_proxy:
            return self._exit_signal(
                reason="spread_stop",
                position_side=position_side,
                regime=regime,
                adx=adx,
                breakout_strength=breakout_strength,
                hold_bars=hold_bars,
                price_vs_vwap=price_vs_vwap,
                vwap_z=vwap_z,
                adverse_spread_move=adverse_spread_move,
                spread_atr_proxy=spread_atr_proxy,
                current_pnl=current_pnl,
                peak_unrealized_pnl=peak_unrealized_pnl,
            )

        if force_exit:
            return self._exit_signal(
                reason="session_or_event_exit",
                position_side=position_side,
                regime=regime,
                adx=adx,
                breakout_strength=breakout_strength,
                hold_bars=hold_bars,
                price_vs_vwap=price_vs_vwap,
                vwap_z=vwap_z,
                current_pnl=current_pnl,
                peak_unrealized_pnl=peak_unrealized_pnl,
            )

        # ---------------------------
        # 2) Time stop
        # ---------------------------
        if hold_bars >= self.params.max_holding_bars:
            return self._exit_signal(
                reason="time_stop",
                position_side=position_side,
                regime=regime,
                adx=adx,
                breakout_strength=breakout_strength,
                hold_bars=hold_bars,
                price_vs_vwap=price_vs_vwap,
                vwap_z=vwap_z,
                current_pnl=current_pnl,
                peak_unrealized_pnl=peak_unrealized_pnl,
            )

        if vwap_z is None:
            return None

        # Soft-exit confirmation: only optimize TP in non-trending state
        soft_exit_ok = (
            abs(vwap_z) <= self.params.soft_exit_confirm_vwap_z
            and adx < self.params.soft_exit_confirm_adx
            and breakout_strength < self.params.soft_exit_confirm_breakout
        )

        # ---------------------------
        # 3) Peak-PnL giveback safety
        # ---------------------------
        if peak_unrealized_pnl > 0:
            if current_pnl < peak_unrealized_pnl * self.params.peak_pnl_retention:
                return self._exit_signal(
                    reason="peak_pnl_giveback_exit",
                    position_side=position_side,
                    regime=regime,
                    adx=adx,
                    breakout_strength=breakout_strength,
                    hold_bars=hold_bars,
                    price_vs_vwap=price_vs_vwap,
                    vwap_z=vwap_z,
                    current_pnl=current_pnl,
                    peak_unrealized_pnl=peak_unrealized_pnl,
                    tp_delay_used=tp_delay_used,
                )

        # ---------------------------
        # 4) Optional partial exit
        # ---------------------------
        if (
            self.params.enable_partial_exit
            and not partial_exit_done
            and abs(vwap_z) <= self.params.partial_exit_vwap_z
            and soft_exit_ok
        ):
            return CalendarCondorSignalV2(
                side="PARTIAL_EXIT",
                signal_type="CALENDAR_CONDOR_PARTIAL_EXIT",
                reason="partial_take_profit",
                near_action="REDUCE",
                far_action="REDUCE",
                meta={
                    "position_side": position_side,
                    "exit_fraction": self.params.partial_exit_fraction,
                    "regime": regime,
                    "adx": adx,
                    "breakout_strength": breakout_strength,
                    "hold_bars": hold_bars,
                    "price_vs_vwap": price_vs_vwap,
                    "vwap_z": vwap_z,
                    "current_pnl": current_pnl,
                    "peak_unrealized_pnl": peak_unrealized_pnl,
                },
            )

        # ---------------------------
        # 5) Soft take-profit optimization
        # ---------------------------
        # Primary TP reached, but allow short delay if state remains benign
        if abs(vwap_z) <= self.params.take_profit_vwap_z and soft_exit_ok:
            if tp_delay_used < self.params.tp_delay_bars:
                return CalendarCondorSignalV2(
                    side="HOLD",
                    signal_type="CALENDAR_CONDOR_HOLD_BUFFER",
                    reason="tp_time_buffer",
                    near_action="HOLD",
                    far_action="HOLD",
                    meta={
                        "position_side": position_side,
                        "tp_delay_used": tp_delay_used,
                        "tp_delay_next": tp_delay_used + 1,
                        "tp_delay_limit": self.params.tp_delay_bars,
                        "regime": regime,
                        "adx": adx,
                        "breakout_strength": breakout_strength,
                        "hold_bars": hold_bars,
                        "vwap_z": vwap_z,
                        "current_pnl": current_pnl,
                        "peak_unrealized_pnl": peak_unrealized_pnl,
                    },
                )

            # delay consumed, then require extended TP or exit
            if abs(vwap_z) <= self.params.take_profit_vwap_z_extended:
                return self._exit_signal(
                    reason="extended_take_profit_exit",
                    position_side=position_side,
                    regime=regime,
                    adx=adx,
                    breakout_strength=breakout_strength,
                    hold_bars=hold_bars,
                    price_vs_vwap=price_vs_vwap,
                    vwap_z=vwap_z,
                    current_pnl=current_pnl,
                    peak_unrealized_pnl=peak_unrealized_pnl,
                    tp_delay_used=tp_delay_used,
                )

            # if extended TP not reached after delay, still allow normal TP exit
            return self._exit_signal(
                reason="take_profit_reversion",
                position_side=position_side,
                regime=regime,
                adx=adx,
                breakout_strength=breakout_strength,
                hold_bars=hold_bars,
                price_vs_vwap=price_vs_vwap,
                vwap_z=vwap_z,
                current_pnl=current_pnl,
                peak_unrealized_pnl=peak_unrealized_pnl,
                tp_delay_used=tp_delay_used,
            )

        return None

    def _exit_signal(self, reason: str, **meta: Any) -> CalendarCondorSignalV2:
        return CalendarCondorSignalV2(
            side="EXIT",
            signal_type="CALENDAR_CONDOR_EXIT",
            reason=reason,
            near_action="FLAT",
            far_action="FLAT",
            meta=meta,
        )

    @staticmethod
    def _get(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @staticmethod
    def _float_or_none(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


if __name__ == "__main__":
    strategy = CalendarCondorStrategyV2()

    sample_entry = {
        "regime": "WEAK",
        "adx": 18,
        "breakout_strength": 0.32,
        "volume_spike": 0.8,
        "price_vs_vwap": 0.0018,
        "vwap_z": 2.3,
        "spread_z": 2.1,
        "bars_from_session_open": 12,
        "is_night_session": False,
        "in_position": False,
    }

    sample_exit_hold = {
        "regime": "WEAK",
        "adx": 19,
        "breakout_strength": 0.28,
        "price_vs_vwap": 0.0009,
        "vwap_z": 0.45,
        "hold_bars": 5,
        "adverse_spread_move": 0.2,
        "spread_atr_proxy": 0.8,
        "position_side": "SELL_NEAR_BUY_FAR",
        "in_position": True,
        "current_pnl": 120.0,
        "peak_unrealized_pnl": 130.0,
        "tp_delay_used": 0,
        "partial_exit_done": False,
    }

    sample_exit_trend = {
        "regime": "TREND",
        "adx": 31,
        "breakout_strength": 0.72,
        "price_vs_vwap": 0.0048,
        "vwap_z": 1.8,
        "hold_bars": 3,
        "adverse_spread_move": 0.5,
        "spread_atr_proxy": 0.6,
        "position_side": "SELL_NEAR_BUY_FAR",
        "in_position": True,
        "current_pnl": -80.0,
        "peak_unrealized_pnl": 10.0,
    }

    print("Entry signal      :", strategy.on_bar(sample_entry))
    print("Soft TP hold      :", strategy.on_bar(sample_exit_hold))
    print("Hard trend exit   :", strategy.on_bar(sample_exit_trend))
