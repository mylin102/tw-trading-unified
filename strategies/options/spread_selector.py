"""
Vertical spread selector for Taiwan TXO options.

Converts single-leg CALL/PUT signals into debit vertical spreads:
- Bull Call Spread (CALL signal): Buy ATM Call, sell higher OTM Call
- Bear Put Spread  (PUT signal):  Buy ATM Put,  sell lower OTM Put

Design: pure logic, zero dependency on ShioajiOptionsSmartMonitor internals.
Testable standalone with mock contracts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class SpreadLeg:
    """One leg of a vertical spread."""
    contract: Any  # Shioaji Contract object
    action: Any    # sj.constant.Action.Buy or Action.Sell
    strike: int
    option_type: str  # "Call" or "Put"


@dataclass
class VerticalSpread:
    """A complete debit vertical spread decision."""
    direction: str          # "CALL" or "PUT"
    long_leg: SpreadLeg     # the leg we buy (debit)
    short_leg: SpreadLeg    # the leg we sell (credit)
    net_debit: float        # long_ask - short_bid (what we pay)
    max_risk: float         # = net_debit
    max_profit: float       # = strike_width - net_debit
    strike_width: int       # e.g. 50, 100
    expiration: str         # delivery_date string
    long_mid: float         # mid price of long leg
    short_mid: float        # mid price of short leg
    spread_ratio: float     # bid/ask spread / mid for quality check
    reject_reason: str = ""  # if ALLOW, this is empty


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nearest_strike(price: float, chain: List[Any], rounding: int = 50) -> Optional[int]:
    """Find the nearest available strike to the given price, rounded to `rounding`."""
    if not chain:
        return None
    available = sorted({c.strike_price for c in chain})
    # binary search for nearest
    import bisect
    idx = bisect.bisect_left(available, int(round(price / rounding) * rounding))
    if idx == 0:
        return available[0]
    if idx >= len(available):
        return available[-1]
    left = available[idx - 1]
    right = available[idx]
    return left if (price - left) <= (right - price) else right


def _contract_for_strike(chain: List[Any], strike: int, option_type: str) -> Optional[Any]:
    """Find first contract matching strike + option_type (Call/Put)."""
    for c in chain:
        right = str(c.option_right).lower()
        if c.strike_price == strike and option_type.lower() in right:
            return c
    return None


def _current_quote(contract: Any, market_data: Dict[str, Dict[str, float]]) -> Tuple[float, float, float]:
    """Get (bid, ask, mid) for a contract from market_data."""
    code = getattr(contract, 'code', '')
    if code in market_data:
        d = market_data[code]
        bid = float(d.get("bid", 0))
        ask = float(d.get("ask", 0))
    else:
        # fallback: use reference price
        ref = float(getattr(contract, 'reference', 0) or 0)
        bid, ask = ref * 0.98, ref * 1.02
    if bid <= 0 and ask <= 0:
        bid, ask = 0.0, 0.0
    mid = (bid + ask) / 2 if ask > 0 else 0.0
    return bid, ask, mid


# ---------------------------------------------------------------------------
# Core selector
# ---------------------------------------------------------------------------

DEFAULT_WIDTH = 100          # points between strikes
MIN_REWARD_RISK = 1.5        # minimum max_profit / max_risk ratio
MAX_SPREAD_RATIO = 0.30      # max bid/ask spread relative to mid
MIN_NET_DEBIT = 1.0          # minimum net debit in points
FRICTION_ESTIMATE = 0.004    # 0.4% friction for edge gate


def select_vertical_spread(
    direction: str,
    index_price: float,
    option_chain: List[Any],
    market_data: Optional[Dict[str, Dict[str, float]]] = None,
    width: int = DEFAULT_WIDTH,
    strike_rounding: int = 50,
    max_spread_ratio: float = MAX_SPREAD_RATIO,
    min_reward_risk: float = MIN_REWARD_RISK,
) -> Tuple[Optional[VerticalSpread], str]:
    """
    Select the best vertical spread for the given direction.

    Args:
        direction: "CALL" or "PUT"
        index_price: Current MTX/TX index price
        option_chain: List of Shioaji option contracts for the target expiration
        market_data: Optional dict mapping contract_code -> {bid, ask}
        width: Strike width in points (50 or 100)
        strike_rounding: Rounding for ATM strike selection
        max_spread_ratio: Max allowed bid/ask spread / mid price
        min_reward_risk: Minimum reward/risk ratio

    Returns:
        (VerticalSpread or None, reason_string)
    """
    market_data = market_data or {}

    # ── 1. Validate direction ──
    direction_upper = direction.upper()
    if direction_upper not in ("CALL", "PUT"):
        return None, f"INVALID_DIRECTION:{direction}"

    # ── 2. Find ATM strike ──
    atm_strike = _nearest_strike(index_price, option_chain, strike_rounding)
    if atm_strike is None:
        return None, "NO_AVAILABLE_STRIKES"

    # ── 3. Determine strikes for both legs ──
    is_call = direction_upper == "CALL"
    if is_call:
        # Bull Call Spread: Buy ATM Call, sell higher OTM Call
        long_strike = atm_strike
        short_strike = atm_strike + width
        opt_type = "Call"
    else:
        # Bear Put Spread: Buy ATM Put, sell lower OTM Put
        long_strike = atm_strike
        short_strike = atm_strike - width
        opt_type = "Put"

    # ── 4. Find contracts ──
    long_contract = _contract_for_strike(option_chain, long_strike, opt_type)
    short_contract = _contract_for_strike(option_chain, short_strike, opt_type)

    if not long_contract:
        return None, f"LONG_LEG_NOT_FOUND:strike={long_strike}"
    if not short_contract:
        return None, f"SHORT_LEG_NOT_FOUND:strike={short_strike}"

    # ── 5. Get quotes ──
    long_bid, long_ask, long_mid = _current_quote(long_contract, market_data)
    short_bid, short_ask, short_mid = _current_quote(short_contract, market_data)

    if long_ask <= 0 or short_bid <= 0:
        return None, "INVALID_QUOTE"

    # ── 6. Compute net debit and risk metrics ──
    net_debit = long_ask - short_bid  # what we pay
    if net_debit <= MIN_NET_DEBIT:
        return None, f"NET_DEBIT_TOO_SMALL:{net_debit:.1f}"

    max_risk = net_debit
    max_profit = width - net_debit

    if max_profit <= 0:
        return None, f"NEGATIVE_MAX_PROFIT:{max_profit:.1f}"

    # ── 7. Edge gates ──

    # 7a. bid/ask spread quality
    spread_ratio = (long_ask - long_bid) / long_mid if long_mid > 0 else 0
    total_friction = net_debit * FRICTION_ESTIMATE * 2  # both legs

    if spread_ratio > max_spread_ratio:
        return None, f"SPREAD_TOO_WIDE:{spread_ratio:.2f}"

    # 7b. reward/risk
    rr_ratio = max_profit / max_risk if max_risk > 0 else 0
    if rr_ratio < min_reward_risk:
        return None, f"REWARD_RISK_TOO_LOW:{rr_ratio:.2f}"

    # 7c. edge vs friction
    if max_profit < total_friction * 2:
        return None, f"EDGE_TOO_SMALL:profit={max_profit:.1f}_friction={total_friction:.1f}"

    # ── 8. Build result ──
    import shioaji as sj
    long_leg = SpreadLeg(
        contract=long_contract,
        action=sj.constant.Action.Buy,
        strike=long_strike,
        option_type=opt_type,
    )
    short_leg = SpreadLeg(
        contract=short_contract,
        action=sj.constant.Action.Sell,
        strike=short_strike,
        option_type=opt_type,
    )

    expiration = getattr(long_contract, 'delivery_date', '')
    spread = VerticalSpread(
        direction=direction_upper,
        long_leg=long_leg,
        short_leg=short_leg,
        net_debit=round(net_debit, 2),
        max_risk=round(max_risk, 2),
        max_profit=round(max_profit, 2),
        strike_width=width,
        expiration=str(expiration),
        long_mid=round(long_mid, 2),
        short_mid=round(short_mid, 2),
        spread_ratio=round(spread_ratio, 3),
    )
    return spread, "ALLOW"


# ---------------------------------------------------------------------------
# Combo leg builder (for broker.place_comboorder)
# ---------------------------------------------------------------------------

def build_combo_legs(spread: VerticalSpread) -> List[Dict[str, Any]]:
    """
    Build the leg list for ShioajiBrokerAdapter.place_comboorder().

    Returns:
        [{"contract": ..., "action": Action.Buy}, {"contract": ..., "action": Action.Sell}]
    """
    return [
        {"contract": spread.long_leg.contract, "action": spread.long_leg.action},
        {"contract": spread.short_leg.contract, "action": spread.short_leg.action},
    ]
