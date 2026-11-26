"""2026-11-25 Hermes Agent: paper fills must land on the executable side.

Option A (basis alignment). Without this, the BBO-based bracket fix would
stop out within one second of entry: an entry booked at `close` on a 21-point
book is already -33 on its own exit side (trade mts-auto-171646-409).

Invariants:
  B1. A combo buy fills at the ask, a leg sell fills at the bid.
  B2. Missing/zero/invalid quote falls back to close (never blocks a fill).
  B3. Entry at the executable price leaves bracket P&L at ~0, so a 30-point
      stop is armed with the full 30 points of room (no instant stop).
"""
import math
from types import SimpleNamespace

import pytest

from core.order_management.paper_fill import PaperFillSimulator
from strategies.plugins.futures.active.tmf_spread import TMFSpread


NEAR = SimpleNamespace(code="TMFL6", symbol="TMFL6", close=37413.0,
                       bid=37410.0, ask=37416.0)
FAR = SimpleNamespace(code="TMFA7", symbol="TMFA7", close=37537.0,
                      bid=37537.0, ask=37558.0)


def test_buy_legs_lift_ask_and_sell_legs_hit_bid():
    sim = PaperFillSimulator.__new__(PaperFillSimulator)
    # BUY_NEAR_SELL_FAR: near buys -> ask, far sells -> bid
    assert sim._leg_exec_price(NEAR, NEAR.close, buy=True) == 37416.0
    assert sim._leg_exec_price(FAR, FAR.close, buy=False) == 37537.0
    # SELL_NEAR_BUY_FAR: near sells -> bid, far buys -> ask
    assert sim._leg_exec_price(NEAR, NEAR.close, buy=False) == 37410.0
    assert sim._leg_exec_price(FAR, FAR.close, buy=True) == 37558.0


def test_falls_back_to_close_without_quote():
    sim = PaperFillSimulator.__new__(PaperFillSimulator)
    bare = SimpleNamespace(code="X", symbol="X", close=100.0)
    assert sim._leg_exec_price(bare, 100.0, buy=True) == 100.0
    assert sim._leg_exec_price(bare, 100.0, buy=False) == 100.0
    zero = SimpleNamespace(code="X", symbol="X", close=100.0, bid=0.0, ask=float("nan"))
    assert sim._leg_exec_price(zero, 100.0, buy=True) == 100.0
    assert sim._leg_exec_price(zero, 100.0, buy=False) == 100.0


def test_entry_on_executable_basis_leaves_stop_room():
    """B3 — the acceptance test for the whole slice.

    Far leg SHORT entered at the bid (37537); its bracket reads the ask
    (37558). Same instant P&L is -21 (one crossing), not -33, and stays
    inside the 30-point stop. If the entry were booked at `close` (37525
    for that trade) the same instant reads -33 and stops out at once.
    """
    s = TMFSpread.__new__(TMFSpread)
    s._far_side = "SHORT"
    s._near_side = "LONG"
    s._far_entry = 37537.0        # filled at bid
    s._near_entry = 37416.0       # filled at ask

    instant_far = s._pnl_far(s._bracket_exit_price("far", {}, FAR.ask))
    instant_near = s._pnl_near(s._bracket_exit_price("near", {}, NEAR.bid))
    assert instant_far == pytest.approx(-21.0)
    assert instant_near == pytest.approx(-6.0)
    assert instant_far > -30.0 and instant_near > -30.0   # no instant stop

    # old basis: entry at close -> instant breach
    s._far_entry = 37525.0
    assert s._pnl_far(FAR.ask) == pytest.approx(-33.0)     # <= -30 -> would stop
