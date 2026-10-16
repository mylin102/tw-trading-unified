"""
mts_renko_signal.py — Pure Renko signal adapter (spec section 2) for MTS 2.0.

Renko is computed from MID prices (spec 5.2). 2 consecutive same-direction
bricks => trend signal. Brick size locked at entry. PURE, no broker/side
effects; state derives from an immutable ordered price sequence at decision_ts.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Optional, Sequence
from .mts_trend_signal_adapter import (
    TrendDirection, RenkoState, SubSignalState,
    S_RENKO_SAME, S_RENKO_NONE, S_RENKO_OPPOSITE,
)


@dataclass(frozen=True)
class RenkoResult:
    decision_ts: str
    brick_size: float
    last_brick_close: float
    last_price: float
    consecutive_same_direction: int
    direction: RenkoState
    brick_reverse: bool
    n_bricks: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_renko(decision_ts: Any,
                  prices: Sequence[float],
                  brick_size: float,
                  *,
                  seed_price: Optional[float] = None,
                  ) -> RenkoResult:
    """Compute Renko state from an ordered MID price sequence at/before decision_ts."""
    if not prices or brick_size <= 0:
        return RenkoResult(
            decision_ts=decision_ts, brick_size=brick_size, last_brick_close=0.0,
            last_price=prices[0] if prices else 0.0,
            consecutive_same_direction=0, direction=RenkoState.UNKNOWN,
            brick_reverse=False, n_bricks=0,
        )

    anchor = seed_price if seed_price is not None else prices[0]
    cur = anchor
    consecutive = 0
    last_dir = 0
    n_bricks = 0
    reverse_seen = False

    for p in prices:
        if p >= cur + brick_size:
            steps = max(int((p - cur) / brick_size), 1)
            for _ in range(steps):
                cur += brick_size
                n_bricks += 1
                if last_dir > 0:
                    consecutive += 1
                elif last_dir < 0:
                    consecutive = 1
                    reverse_seen = True
                else:
                    last_dir = 1
                    consecutive = 1
                last_dir = 1
        elif p <= cur - brick_size:
            steps = max(int((cur - p) / brick_size), 1)
            for _ in range(steps):
                cur -= brick_size
                n_bricks += 1
                if last_dir < 0:
                    consecutive += 1
                elif last_dir > 0:
                    consecutive = 1
                    reverse_seen = True
                else:
                    last_dir = -1
                    consecutive = 1
                last_dir = -1

    if n_bricks == 0:
        state = RenkoState.FLAT
    elif last_dir > 0:
        state = RenkoState.UP
    else:
        state = RenkoState.DOWN

    return RenkoResult(
        decision_ts=decision_ts, brick_size=brick_size, last_brick_close=cur,
        last_price=prices[-1], consecutive_same_direction=consecutive,
        direction=state, brick_reverse=reverse_seen, n_bricks=n_bricks,
    )


def renko_signal_state(renko: RenkoResult, expected: Optional[TrendDirection]) -> SubSignalState:
    """Map a RenkoResult to a SubSignalState scored against the expected direction.

    S_RENKO = 1.0 if >=2 same-direction bricks aligned; 0.0 if <2 (FLAT);
    -1.0 if opposite (reverse). UNKNOWN -> -1.0 + UNKNOWN (fail-closed).
    """
    if renko.direction == RenkoState.UNKNOWN:
        return SubSignalState(source="renko", direction=TrendDirection.UNKNOWN,
                               score=S_RENKO_OPPOSITE, detail=renko.to_dict())
    if renko.consecutive_same_direction >= 2 and renko.direction in (RenkoState.UP, RenkoState.DOWN):
        d = TrendDirection.BULLISH if renko.direction == RenkoState.UP else TrendDirection.BEARISH
        if expected == d:
            return SubSignalState(source="renko", direction=d, score=S_RENKO_SAME, detail=renko.to_dict())
        return SubSignalState(source="renko", direction=d, score=S_RENKO_OPPOSITE, detail=renko.to_dict())
    # <2 bricks -> FLAT/insufficient
    return SubSignalState(source="renko", direction=TrendDirection.CHOP,
                           score=S_RENKO_NONE, detail=renko.to_dict())