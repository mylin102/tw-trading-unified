"""
options_spread_executor_reference.py

Reference implementation: convert directional option signals into protected vertical spreads.

Purpose
-------
Instead of buying naked Calls/Puts, this module builds a capped-risk vertical spread:

    Bullish signal  -> Bull Call Spread:  Buy ATM/near Call, Sell OTM Call
    Bearish signal  -> Bear Put Spread:   Buy ATM/near Put,  Sell OTM Put

Design boundary
---------------
- Router decides whether options trading is allowed and the directional bias.
- OptionsMonitor / executor decides whether the option quote, spread width, cost,
  expected reward, and risk are tradeable.
- This file does NOT place real orders. It returns an executable TradePlan object.

Integrate with your live_options_squeeze_monitor.py by replacing naked BUY_CALL / BUY_PUT
entry with build_vertical_spread_plan(...).

Author: reference template
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple
import math
import logging

logger = logging.getLogger(__name__)


class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class OptionRight(str, Enum):
    CALL = "C"
    PUT = "P"


class LegSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class OptionQuote:
    """Minimal quote model. Adapt fields to your Shioaji contract object if needed."""

    symbol: str
    strike: float
    right: OptionRight
    bid: float
    ask: float
    last: Optional[float] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        if self.bid < 0 or self.ask <= 0 or self.ask < self.bid:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None or self.ask < self.bid:
            return None
        return self.ask - self.bid

    @property
    def spread_ratio(self) -> Optional[float]:
        m = self.mid
        s = self.spread
        if m is None or m <= 0 or s is None:
            return None
        return s / m


@dataclass(frozen=True)
class SpreadLeg:
    side: LegSide
    quote: OptionQuote
    qty: int = 1


@dataclass(frozen=True)
class SpreadConfig:
    """Tune these values per market/product."""

    multiplier: int = 50                 # TXO point value example. Adjust if needed.
    strike_step: int = 50                 # Typical TXO strike step. Adjust for weekly/monthly.
    default_width_points: int = 100       # Buy 19300P / Sell 19200P = 100-point width.
    max_quote_spread_ratio: float = 0.35  # Reject illiquid legs.
    min_credit_or_debit: float = 1.0      # Avoid near-zero garbage quotes.
    max_debit_points: float = 90.0        # Max option points paid for the spread.
    min_reward_to_risk: float = 1.0       # Reward/risk must be >= this value.
    min_volume: int = 1                   # Set higher if you have reliable volume.
    min_open_interest: int = 0            # Set higher for monthly contracts.
    friction_points_per_spread: float = 2.0  # Fees + slippage in option points, round-trip estimate.
    prefer_atm_offset_points: int = 0     # 0 = start from nearest strike.


@dataclass(frozen=True)
class SpreadPlan:
    strategy: str
    direction: Direction
    long_leg: SpreadLeg
    short_leg: SpreadLeg
    width_points: float
    debit_points: float
    max_profit_points: float
    max_loss_points: float
    reward_to_risk: float
    estimated_friction_points: float
    reason: str
    metadata: Dict[str, object] = field(default_factory=dict)

    @property
    def debit_cash(self) -> float:
        return self.debit_points * self.metadata.get("multiplier", 1)

    @property
    def max_profit_cash(self) -> float:
        return self.max_profit_points * self.metadata.get("multiplier", 1)

    @property
    def max_loss_cash(self) -> float:
        return self.max_loss_points * self.metadata.get("multiplier", 1)


@dataclass(frozen=True)
class BlockedPlan:
    allowed: bool
    reason: str
    details: Dict[str, object] = field(default_factory=dict)


def nearest_strike(price: float, step: int) -> int:
    return int(round(price / step) * step)


def _quote_key(strike: float, right: OptionRight) -> Tuple[float, OptionRight]:
    return float(strike), right


def quote_is_tradeable(q: OptionQuote, cfg: SpreadConfig) -> Tuple[bool, str]:
    if q.mid is None:
        return False, "INVALID_QUOTE_MID"
    if q.mid < cfg.min_credit_or_debit:
        return False, "QUOTE_TOO_SMALL"
    if q.spread_ratio is None:
        return False, "INVALID_SPREAD_RATIO"
    if q.spread_ratio > cfg.max_quote_spread_ratio:
        return False, "WIDE_LEG_SPREAD"
    if q.volume is not None and q.volume < cfg.min_volume:
        return False, "LOW_VOLUME"
    if q.open_interest is not None and q.open_interest < cfg.min_open_interest:
        return False, "LOW_OPEN_INTEREST"
    return True, "OK"


def choose_vertical_legs(
    direction: Direction,
    underlying_price: float,
    chain: Dict[Tuple[float, OptionRight], OptionQuote],
    cfg: SpreadConfig,
) -> Tuple[Optional[OptionQuote], Optional[OptionQuote], str]:
    """
    Pick long and short legs for a simple debit vertical spread.

    Bearish:
        long higher-strike Put, short lower-strike Put.
    Bullish:
        long lower-strike Call, short higher-strike Call.
    """

    atm = nearest_strike(underlying_price, cfg.strike_step)
    width = cfg.default_width_points

    if direction == Direction.BEARISH:
        right = OptionRight.PUT
        long_strike = atm + cfg.prefer_atm_offset_points
        short_strike = long_strike - width
    elif direction == Direction.BULLISH:
        right = OptionRight.CALL
        long_strike = atm + cfg.prefer_atm_offset_points
        short_strike = long_strike + width
    else:
        return None, None, "UNKNOWN_DIRECTION"

    long_q = chain.get(_quote_key(long_strike, right))
    short_q = chain.get(_quote_key(short_strike, right))

    if long_q is None:
        return None, None, f"MISSING_LONG_LEG_{long_strike}_{right.value}"
    if short_q is None:
        return None, None, f"MISSING_SHORT_LEG_{short_strike}_{right.value}"
    return long_q, short_q, "OK"


def build_vertical_spread_plan(
    *,
    direction: Direction,
    underlying_price: float,
    chain: Dict[Tuple[float, OptionRight], OptionQuote],
    router_signal: Optional[Dict[str, object]] = None,
    cfg: Optional[SpreadConfig] = None,
) -> SpreadPlan | BlockedPlan:
    """
    Convert a directional signal into a protected vertical spread plan.

    router_signal expected optional fields:
        options_allowed: bool
        options_block_reason: str
        confidence: float
        regime: str
        signal_name: str
    """

    cfg = cfg or SpreadConfig()
    router_signal = router_signal or {}

    if not router_signal.get("options_allowed", True):
        return BlockedPlan(
            allowed=False,
            reason="ROUTER_BLOCKED_OPTIONS",
            details={"router_reason": router_signal.get("options_block_reason")},
        )

    long_q, short_q, reason = choose_vertical_legs(direction, underlying_price, chain, cfg)
    if reason != "OK" or long_q is None or short_q is None:
        return BlockedPlan(False, reason, {"underlying_price": underlying_price})

    for label, q in (("long", long_q), ("short", short_q)):
        ok, q_reason = quote_is_tradeable(q, cfg)
        if not ok:
            return BlockedPlan(
                False,
                f"{label.upper()}_{q_reason}",
                {
                    "symbol": q.symbol,
                    "strike": q.strike,
                    "right": q.right.value,
                    "bid": q.bid,
                    "ask": q.ask,
                    "mid": q.mid,
                    "spread_ratio": q.spread_ratio,
                },
            )

    # Conservative executable debit estimate:
    # buy long at ask, sell short at bid.
    debit = long_q.ask - short_q.bid
    width = abs(long_q.strike - short_q.strike)
    friction = cfg.friction_points_per_spread

    if debit <= 0:
        return BlockedPlan(False, "NON_POSITIVE_DEBIT", {"debit": debit})
    if debit > cfg.max_debit_points:
        return BlockedPlan(False, "DEBIT_TOO_HIGH", {"debit": debit, "max": cfg.max_debit_points})
    if width <= 0:
        return BlockedPlan(False, "INVALID_WIDTH", {"width": width})

    max_loss = debit + friction
    max_profit = max(0.0, width - debit - friction)
    rr = max_profit / max_loss if max_loss > 0 else 0.0

    if rr < cfg.min_reward_to_risk:
        return BlockedPlan(
            False,
            "REWARD_RISK_TOO_LOW",
            {
                "reward_to_risk": rr,
                "min_reward_to_risk": cfg.min_reward_to_risk,
                "max_profit_points": max_profit,
                "max_loss_points": max_loss,
            },
        )

    strategy = "BULL_CALL_SPREAD" if direction == Direction.BULLISH else "BEAR_PUT_SPREAD"

    return SpreadPlan(
        strategy=strategy,
        direction=direction,
        long_leg=SpreadLeg(LegSide.BUY, long_q, qty=1),
        short_leg=SpreadLeg(LegSide.SELL, short_q, qty=1),
        width_points=width,
        debit_points=debit,
        max_profit_points=max_profit,
        max_loss_points=max_loss,
        reward_to_risk=rr,
        estimated_friction_points=friction,
        reason="SPREAD_PLAN_OK",
        metadata={
            "underlying_price": underlying_price,
            "multiplier": cfg.multiplier,
            "regime": router_signal.get("regime"),
            "signal_name": router_signal.get("signal_name"),
            "confidence": router_signal.get("confidence"),
        },
    )


def format_order_preview(plan: SpreadPlan | BlockedPlan) -> str:
    if isinstance(plan, BlockedPlan):
        return f"[BLOCKED] reason={plan.reason} details={plan.details}"

    return (
        f"[{plan.strategy}] {plan.reason}\n"
        f"  LONG : {plan.long_leg.side.value} {plan.long_leg.quote.symbol} "
        f"{plan.long_leg.quote.right.value}{plan.long_leg.quote.strike:g} "
        f"bid={plan.long_leg.quote.bid:g} ask={plan.long_leg.quote.ask:g}\n"
        f"  SHORT: {plan.short_leg.side.value} {plan.short_leg.quote.symbol} "
        f"{plan.short_leg.quote.right.value}{plan.short_leg.quote.strike:g} "
        f"bid={plan.short_leg.quote.bid:g} ask={plan.short_leg.quote.ask:g}\n"
        f"  debit={plan.debit_points:.2f} pts, width={plan.width_points:.0f} pts, "
        f"max_profit={plan.max_profit_points:.2f} pts, max_loss={plan.max_loss_points:.2f} pts, "
        f"R/R={plan.reward_to_risk:.2f}"
    )


def example_chain() -> Dict[Tuple[float, OptionRight], OptionQuote]:
    """Small fake chain for local testing."""
    quotes = [
        OptionQuote("TXO202605P19300", 19300, OptionRight.PUT, bid=120, ask=125, volume=80, open_interest=500),
        OptionQuote("TXO202605P19200", 19200, OptionRight.PUT, bid=72, ask=76, volume=100, open_interest=800),
        OptionQuote("TXO202605C19300", 19300, OptionRight.CALL, bid=118, ask=124, volume=90, open_interest=600),
        OptionQuote("TXO202605C19400", 19400, OptionRight.CALL, bid=70, ask=75, volume=110, open_interest=700),
    ]
    return {_quote_key(q.strike, q.right): q for q in quotes}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    chain = example_chain()
    router_signal = {
        "options_allowed": True,
        "regime": "WEAK",
        "signal_name": "counter_vwap_bearish",
        "confidence": 0.68,
    }

    plan = build_vertical_spread_plan(
        direction=Direction.BEARISH,
        underlying_price=19320,
        chain=chain,
        router_signal=router_signal,
        cfg=SpreadConfig(
            multiplier=50,
            strike_step=50,
            default_width_points=100,
            max_debit_points=80,
            friction_points_per_spread=2.0,
            min_reward_to_risk=0.8,
        ),
    )

    print(format_order_preview(plan))
