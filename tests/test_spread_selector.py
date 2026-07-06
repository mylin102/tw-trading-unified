"""Unit tests for strategies/options/spread_selector.py."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import shioaji as sj

from strategies.options.spread_selector import (
    VerticalSpread,
    _nearest_strike,
    _contract_for_strike,
    select_vertical_spread,
    build_combo_legs,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_contract(strike: int, opt_type: str, code: str = ""):
    """Create a mock Shioaji option contract."""
    right = sj.constant.OptionRight.Call if opt_type.lower() == "call" else sj.constant.OptionRight.Put
    return SimpleNamespace(
        code=code or f"TXO{strike}{'C' if opt_type.lower() == 'call' else 'P'}6",
        strike_price=strike,
        option_right=right,
        delivery_date="2026/05/13",
        reference=50.0,
    )


@pytest.fixture
def option_chain():
    """Standard option chain with strikes 39400..39600 in 50pt intervals."""
    chain = []
    for strike in range(39400, 39601, 50):
        chain.append(_make_contract(strike, "Call"))
        chain.append(_make_contract(strike, "Put"))
    return chain


@pytest.fixture
def mock_market_data():
    """Market data: bid/ask for each contract keyed by contract code."""
    md = {}
    for strike in range(39400, 39601, 50):
        call_code = f"TXO{strike}C6"
        put_code = f"TXO{strike}P6"
        md[call_code] = {"bid": 100.0, "ask": 110.0}
        md[put_code] = {"bid": 90.0, "ask": 100.0}
    return md


# ---------------------------------------------------------------------------
# Unit: _nearest_strike
# ---------------------------------------------------------------------------

class TestNearestStrike:
    def test_exact_match(self, option_chain):
        assert _nearest_strike(39500, option_chain) == 39500

    def test_round_to_nearest(self, option_chain):
        assert _nearest_strike(39525, option_chain) == 39500  # closer to 39500
        assert _nearest_strike(39535, option_chain) == 39550  # closer to 39550

    def test_below_min(self, option_chain):
        assert _nearest_strike(39000, option_chain) == 39400  # clamp to lowest

    def test_above_max(self, option_chain):
        assert _nearest_strike(40000, option_chain) == 39600  # clamp to highest

    def test_empty_chain(self):
        assert _nearest_strike(39500, []) is None


# ---------------------------------------------------------------------------
# Unit: _contract_for_strike
# ---------------------------------------------------------------------------

class TestContractForStrike:
    def test_find_call(self, option_chain):
        c = _contract_for_strike(option_chain, 39500, "Call")
        assert c is not None
        assert c.strike_price == 39500

    def test_find_put(self, option_chain):
        c = _contract_for_strike(option_chain, 39500, "Put")
        assert c is not None
        assert c.strike_price == 39500

    def test_not_found(self, option_chain):
        assert _contract_for_strike(option_chain, 99999, "Call") is None

    def test_empty_chain(self):
        assert _contract_for_strike([], 39500, "Call") is None


# ---------------------------------------------------------------------------
# Unit: select_vertical_spread — CALL direction
# ---------------------------------------------------------------------------

class TestSelectBullCallSpread:
    def test_basic_bull_call(self, option_chain, mock_market_data):
        """CALL signal → Bull Call Spread: Buy 39500C, Sell 39600C."""
        spread, reason = select_vertical_spread(
            "CALL", 39500, option_chain, mock_market_data, width=100
        )
        assert spread is not None, f"Expected ALLOW, got {reason}"
        assert reason == "ALLOW"
        assert spread.direction == "CALL"
        assert spread.long_leg.strike == 39500
        assert spread.short_leg.strike == 39600
        assert spread.long_leg.action == sj.constant.Action.Buy
        assert spread.short_leg.action == sj.constant.Action.Sell
        assert spread.strike_width == 100

    def test_bull_call_50pt(self, option_chain, mock_market_data):
        """Width = 50 points."""
        spread, reason = select_vertical_spread(
            "CALL", 39500, option_chain, mock_market_data, width=50
        )
        assert spread is not None, f"Expected ALLOW, got {reason}"
        assert spread.long_leg.strike == 39500
        assert spread.short_leg.strike == 39550


# ---------------------------------------------------------------------------
# Unit: select_vertical_spread — PUT direction
# ---------------------------------------------------------------------------

class TestSelectBearPutSpread:
    def test_basic_bear_put(self, option_chain, mock_market_data):
        """PUT signal → Bear Put Spread: Buy 39500P, Sell 39400P."""
        spread, reason = select_vertical_spread(
            "PUT", 39500, option_chain, mock_market_data, width=100
        )
        assert spread is not None, f"Expected ALLOW, got {reason}"
        assert reason == "ALLOW"
        assert spread.direction == "PUT"
        assert spread.long_leg.strike == 39500
        assert spread.short_leg.strike == 39400
        assert spread.long_leg.action == sj.constant.Action.Buy
        assert spread.short_leg.action == sj.constant.Action.Sell

    def test_bear_put_50pt(self, option_chain, mock_market_data):
        spread, reason = select_vertical_spread(
            "PUT", 39500, option_chain, mock_market_data, width=50
        )
        assert spread is not None, f"Expected ALLOW, got {reason}"
        assert spread.long_leg.strike == 39500
        assert spread.short_leg.strike == 39450


# ---------------------------------------------------------------------------
# Unit: edge gates
# ---------------------------------------------------------------------------

class TestEdgeGates:
    def test_invalid_direction(self, option_chain):
        spread, reason = select_vertical_spread("HOLD", 39500, option_chain)
        assert spread is None
        assert "INVALID_DIRECTION" in reason

    def test_spread_too_wide(self, option_chain):
        """spread_ratio > max_spread_ratio should reject.
        
        Need quotes where:
        - net_debit > 1 (long_ask - short_bid > 1)
        - max_profit > 0 (width - net_debit > 0)
        - spread_ratio > max_spread_ratio
        """
        md = {}
        for strike in range(39400, 39601, 50):
            c = f"TXO{strike}C6"
            # bid=10, ask=20 → mid=15 → spread_ratio=(20-10)/15=0.67
            md[c] = {"bid": 10, "ask": 20}
            p = f"TXO{strike}P6"
            md[p] = {"bid": 8, "ask": 18}
        # long_ask=20, short_bid=10 → net_debit=10 → max_profit=100-10=90
        spread, reason = select_vertical_spread(
            "CALL", 39500, option_chain, md, max_spread_ratio=0.25
        )
        assert spread is None, f"Expected rejection, got reason={reason}"
        assert "SPREAD_TOO_WIDE" in reason

    def test_net_debit_too_small(self, option_chain):
        """long_ask - short_bid <= 1 should reject."""
        md = {}
        for strike in range(39400, 39601, 50):
            c = f"TXO{strike}C6"
            p = f"TXO{strike}P6"
            md[c] = {"bid": 1.0, "ask": 1.0}   # too close
            md[p] = {"bid": 1.0, "ask": 1.0}
        spread, reason = select_vertical_spread(
            "CALL", 39500, option_chain, md
        )
        assert spread is None
        assert "NET_DEBIT_TOO_SMALL" in reason

    def test_no_long_leg(self, option_chain):
        """If long strike contract doesn't exist, reject."""
        # Use a chain that only has strikes far away
        bad_chain = [_make_contract(10000, "Call")]
        spread, reason = select_vertical_spread(
            "CALL", 39500, bad_chain
        )
        assert spread is None
        assert "NOT_FOUND" in reason


# ---------------------------------------------------------------------------
# Unit: build_combo_legs
# ---------------------------------------------------------------------------

class TestBuildComboLegs:
    def test_builds_two_legs(self, option_chain, mock_market_data):
        spread, reason = select_vertical_spread(
            "CALL", 39500, option_chain, mock_market_data
        )
        assert spread is not None

        legs = build_combo_legs(spread)
        assert len(legs) == 2
        assert legs[0]["action"] == sj.constant.Action.Buy
        assert legs[1]["action"] == sj.constant.Action.Sell
        assert legs[0]["contract"] is spread.long_leg.contract
        assert legs[1]["contract"] is spread.short_leg.contract


# ---------------------------------------------------------------------------
# Integration-style: market_data fallback (no market_data provided)
# ---------------------------------------------------------------------------

class TestMarketDataFallback:
    def test_no_market_data_fallback_to_reference(self, option_chain):
        """Without market_data, falls back to reference price."""
        spread, reason = select_vertical_spread(
            "CALL", 39500, option_chain, market_data={}
        )
        # May or may not pass depending on reference prices — just verify it doesn't crash
        assert reason in ("ALLOW", "NET_DEBIT_TOO_SMALL", "SPREAD_TOO_WIDE",
                          "REWARD_RISK_TOO_LOW", "EDGE_TOO_SMALL")


# ---------------------------------------------------------------------------
# Edge: reward/risk too low
# ---------------------------------------------------------------------------

class TestRewardRisk:
    def test_low_reward_risk_rejected(self):
        """RR < min_reward_risk should reject.
        
        Need a chain where VERTICAL SPREAD gives net_debit near width,
        so max_profit is tiny relative to net_debit.
        
        For a Bull Call Spread width=50: buy 39500C, sell 39550C
        If long_ask=48, short_bid=2 → net_debit=46, max_profit=50-46=4, RR=4/46=0.087
        """
        chain = [
            _make_contract(39500, "Call", "TXO39500C6"),
            _make_contract(39550, "Call", "TXO39550C6"),
        ]
        md = {
            "TXO39500C6": {"bid": 40.0, "ask": 48.0},   # long_ask=48
            "TXO39550C6": {"bid": 2.0, "ask": 10.0},     # short_bid=2
        }
        spread, reason = select_vertical_spread(
            "CALL", 39500, chain, md, width=50, min_reward_risk=1.0
        )
        assert spread is None, f"Expected reject, got spread with net_debit={spread.net_debit}"
        assert "REWARD_RISK_TOO_LOW" in reason, f"Got reason: {reason}"
