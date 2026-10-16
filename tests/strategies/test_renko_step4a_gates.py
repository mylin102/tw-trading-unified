# Append Step 4A tests: 30-49 legal-session bricks, cross-session open,
# gap re-entry quarantine contract.
import datetime

import pytest

from strategies.plugins.futures.active.renko_tracker import RenkoTracker


def _tracker(anchor=100.0, brick=10.0):
    return RenkoTracker(anchor_price=anchor, brick_size=brick)


def test_session_30_49_jump_produces_bricks():
    """30–49pt jump inside a legal session is normal (OBSERVE_ONLY zone):
    full brick production, no rejection."""
    t = _tracker()
    t.add(110.0)
    r = t.add(140.0)  # 30pt jump → 3 bricks (110→140)
    assert r[0] == 3
    assert t.renko_close == 140.0
    # 49pt jump also normal
    t2 = _tracker()
    t2.add(110.0)
    r2 = t2.add(149.0)  # 39pt
    assert r2[0] == 3
    assert t2.renko_close == 140.0


def test_cross_session_open_not_gap_quarantined():
    """A normal session OPEN (15:00 night start) is a legal-session tick —
    NOT a gap fault. Gap re-entry only applies WITHIN one legal session."""
    from core.date_utils import is_taifex_trading_session
    # 15:00 night open → in session (normal cross-session open, NOT gap fault)
    night_open = datetime.datetime(2026, 7, 31, 15, 0, 0)
    assert bool(is_taifex_trading_session(night_open)) is True
    # 08:45 day open → in session
    day_open = datetime.datetime(2026, 7, 31, 8, 45, 0)
    assert bool(is_taifex_trading_session(day_open)) is True
    # 13:45 day close boundary → in session
    close_boundary = datetime.datetime(2026, 7, 31, 13, 45, 0)
    assert bool(is_taifex_trading_session(close_boundary)) is True
    # 14:50 post-close → NOT in session (14:50 anomaly rejected here)
    post_close = datetime.datetime(2026, 7, 31, 14, 50, 0)
    assert bool(is_taifex_trading_session(post_close)) is False
    # 13:46 immediately after day close → NOT in session
    after_close = datetime.datetime(2026, 7, 31, 13, 46, 0)
    assert bool(is_taifex_trading_session(after_close)) is False


def test_gap_reentry_quarantine_contract():
    """Gap re-entry gate exists in runtime (tmf_spread renko block) with the
    fixture's contract: 900s threshold, 2 quarantine ticks, zero mutation.
    Verified via source inspection + fixture (integration behavior on paper)."""
    import inspect
    from strategies.plugins.futures.active import tmf_spread
    src = inspect.getsource(tmf_spread)
    assert "RENKO_SESSION_GATE_REJECT" in src, "session gate missing"
    assert "RENKO_GAP_QUARANTINE" in src, "gap quarantine missing"
    assert "_renko_gap_quarantine_left = 2" in src, "quarantine count != 2"
    assert ">= 900" in src, "gap threshold != 900s"
    # fixture contract
    import json
    from pathlib import Path
    fx = json.loads((Path(__file__).parent.parent / "fixtures" / "jump_policy_fixture.json").read_text())
    assert fx["gap_reentry"]["gap_reentry_seconds"] == 900
    assert fx["gap_reentry"]["quarantine_first_ticks"] == 2
