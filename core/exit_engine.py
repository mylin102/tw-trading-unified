"""
Exit Engine v1.5 — ATR-based trailing stop with breakeven + profit lock.

Phases (SHORT side shown; LONG is mirror):
  Phase 0 — Initial:      stop = entry ± 0.6 ATR
  Phase 1 — Breakeven:    浮盈 >= 1.0 ATR → stop = entry
  Phase 2 — Lock Profit:  浮盈 >= 1.5 ATR → stop = entry ± 0.5 ATR
  Phase 3 — Trailing:     浮盈 >= 2.0 ATR → stop = watermark ∓ 0.5 ATR

Key principle: stop can ONLY move in the profit direction, never reversed.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TrailingStopState:
    """Persistent state for one position's trailing stop."""
    entry_price: float
    side: str  # "LONG" or "SHORT"
    atr_at_entry: float
    initial_stop: float
    current_stop: float
    high_watermark: float  # highest price since entry (LONG)
    low_watermark: float   # lowest price since entry (SHORT)
    # Configurable thresholds (ATR multiples)
    breakeven_threshold: float = 1.0
    lock_threshold: float = 1.5
    trail_threshold: float = 2.0
    trail_offset: float = 0.5
    lock_offset: float = 0.5
    stop_atr_mult: float = 0.6

    @classmethod
    def create(cls, entry_price: float, side: str, atr: float,
               stop_atr_mult: float = 0.6, **kwargs) -> "TrailingStopState":
        if side == "LONG":
            initial_stop = entry_price - atr * stop_atr_mult
        else:
            initial_stop = entry_price + atr * stop_atr_mult
        return cls(
            entry_price=entry_price,
            side=side.upper(),
            atr_at_entry=atr,
            initial_stop=initial_stop,
            current_stop=initial_stop,
            high_watermark=entry_price,
            low_watermark=entry_price,
            stop_atr_mult=stop_atr_mult,
            **kwargs,
        )

    def update(self, current_price: float) -> tuple[float, Optional[str]]:
        """
        Update watermark and compute new stop price.

        Returns:
            (new_stop, phase_name or None)
        """
        atr = self.atr_at_entry
        entry = self.entry_price

        if self.side == "LONG":
            self.high_watermark = max(self.high_watermark, current_price)
            profit = self.high_watermark - entry
            new_stop = self.current_stop  # default: keep existing

            if profit >= self.trail_threshold * atr:
                # Phase 3 — trailing
                new_stop = max(new_stop, self.high_watermark - self.trail_offset * atr)
                return new_stop, "PHASE3_TRAIL"
            elif profit >= self.lock_threshold * atr:
                # Phase 2 — lock profit
                locked = entry + self.lock_offset * atr
                new_stop = max(new_stop, locked)
                return new_stop, "PHASE2_LOCK"
            elif profit >= self.breakeven_threshold * atr:
                # Phase 1 — breakeven
                new_stop = max(new_stop, entry)
                return new_stop, "PHASE1_BREAKEVEN"

            # Phase 0 — initial stop
            return new_stop, None

        else:  # SHORT
            self.low_watermark = min(self.low_watermark, current_price)
            profit = entry - self.low_watermark
            new_stop = self.current_stop

            if profit >= self.trail_threshold * atr:
                # Phase 3 — trailing
                new_stop = min(new_stop, self.low_watermark + self.trail_offset * atr)
                return new_stop, "PHASE3_TRAIL"
            elif profit >= self.lock_threshold * atr:
                # Phase 2 — lock profit
                locked = entry - self.lock_offset * atr
                new_stop = min(new_stop, locked)
                return new_stop, "PHASE2_LOCK"
            elif profit >= self.breakeven_threshold * atr:
                # Phase 1 — breakeven
                new_stop = min(new_stop, entry)
                return new_stop, "PHASE1_BREAKEVEN"

            return new_stop, None

    def is_stopped(self, current_price: float) -> bool:
        """Check if current price has hit the stop."""
        if self.side == "LONG":
            return current_price <= self.current_stop
        else:
            return current_price >= self.current_stop


def should_exit(trade_state: dict, context: dict, market: dict) -> tuple[bool, str]:
    """
    Original exit engine — kept for backward compatibility.
    V1.5 trailing stop is managed via TrailingStopState directly.
    """
    from core.edge_model import edge_model
    from core.risk import dynamic_stop_loss

    edge_res = edge_model.evaluate(context.get("signal_score", 50), context, "exit_check")
    edge = edge_res["edge_score"]

    if edge < 0.3:
        return True, f"EXIT_NO_EDGE ({edge:.2f})"

    regime_dict = {
        "volatility": context.get("volatility_norm", 0.5),
        "trend_strength": 0.8 if context.get("regime") == "STRONG" else 0.4
    }

    stop_price = dynamic_stop_loss(
        trade_state["entry_price"],
        market["atr"],
        regime_dict,
        edge=edge,
        side=trade_state["side"]
    )

    curr_p = market["price"]
    side = trade_state["side"]

    if side == "LONG" and curr_p <= stop_price:
        return True, f"ADAPTIVE_SL ({curr_p:.1f} <= {stop_price:.1f})"
    elif side == "SHORT" and curr_p >= stop_price:
        return True, f"ADAPTIVE_SL ({curr_p:.1f} >= {stop_price:.1f})"

    if market["time_to_close_mins"] < 10:
        if edge < 0.6:
            return True, f"EOD_WEAK_EDGE ({edge:.2f})"
        if edge < 0.8:
            return True, f"EOD_FINAL_SETTLE"

    return False, "HOLD"
