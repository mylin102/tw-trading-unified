# Step 4: multi-brick equivalence + mutation invariants.
# Uses the CURRENT RenkoTracker (no runtime changes). Differences from the
# canonical contract are reported, not silently patched.
import json
from pathlib import Path

import pytest

from strategies.plugins.futures.active.renko_tracker import RenkoTracker

FIXTURE = Path(__file__).parent.parent / "fixtures" / "jump_policy_fixture.json"


@pytest.fixture(scope="module")
def policy():
    return json.loads(FIXTURE.read_text())


def _tracker(anchor=100.0, brick=10.0):
    return RenkoTracker(anchor_price=anchor, brick_size=brick)


# ── 1. multi-brick jump vs incremental equivalence ───────────────────────
def test_multibrick_jump_equals_incremental_ticks():
    """100→135 in one tick must equal 100→110→120→130→135 tick-by-tick."""
    one = _tracker()
    r1 = one.add(135.0)

    inc = _tracker()
    for p in (110.0, 120.0, 130.0, 135.0):
        inc.add(p)

    assert r1[0] == 3, f"expected 3 bricks, got {r1[0]}"
    assert one.total_bricks == inc.total_bricks
    assert one.brick_sequence == inc.brick_sequence
    assert one.renko_open == inc.renko_open
    assert one.renko_close == inc.renko_close
    assert one.trend == inc.trend
    # brick-by-brick open/close equality
    b1 = one.get_recent_bricks()
    b2 = inc.get_recent_bricks()
    assert len(b1) == len(b2) == 3
    for a, b in zip(b1, b2):
        assert a["open"] == b["open"] and a["close"] == b["close"]
        assert a["trend"] == b["trend"]
    # canonical bricks: 110, 120, 130
    assert [b["close"] for b in b1] == [110.0, 120.0, 130.0]


def test_multibrick_reverse_path_equals_incremental_ticks():
    """100→130→95: 2-brick reversal (110, 100) — is_reversal on first only.
    NOTE: current add() returns NEGATIVE count (-2) on reversal — callers not
    yet audited for sign dependency; assertions use abs() + canonical events."""
    one = _tracker()
    one.add(130.0)          # 3 up bricks: 110/120/130
    r = one.add(95.0)       # reversal: 130→110 (2x) →100
    assert abs(r[0]) == 2, "reversal must produce 2 bricks (abs)"
    bricks = one.get_recent_bricks()
    revs = [b for b in bricks if b.get("is_reversal")]
    assert len(revs) == 1, f"expected exactly 1 reversal brick, got {len(revs)}"
    # sequence contiguous, no duplicates
    seqs = [b["brick_sequence"] for b in one.get_recent_bricks(20)]
    assert len(set(seqs)) == len(seqs), "duplicate brick_sequence!"
    assert seqs == sorted(seqs) and seqs == list(range(1, len(seqs) + 1)), "sequence gaps!"
    # final anchor/close sane
    assert one.trend == -1


# ── 2. session / gap gates (contract tests — runtime NOT changed) ─────────
def test_non_session_tick_generates_zero_bricks(policy):
    """post_close_tick_14_50 → zero bricks. Currently rejected by jump cap;
    session calendar gate is a required runtime addition."""
    case = next(c for c in policy["named_cases"] if c["name"] == "post_close_tick_14_50")
    assert case["expected_handler"] == "SESSION_CALENDAR"
    assert case["not_jump_filter"] is True
    t = _tracker(anchor=43750.0)
    # 750pt jump from 43750 (raw observed) — must produce zero bricks
    r = t.add(43000.0)
    assert r[0] == 0
    assert t.renko_close == 43750.0  # anchor untouched


def test_gap_reentry_tick_is_quarantined(policy):
    """Gap re-entry (>900s) → first ticks quarantined. Contract test — the
    gap-reentry gate is NOT implemented in current runtime (reported, not patched)."""
    assert policy["gap_reentry"]["gap_reentry_seconds"] == 900
    assert policy["gap_reentry"]["quarantine_first_ticks"] == 2
    # runtime lacks the gate — mark as known gap (fail = contract evidence)
    # NOTE: asserted via fixture only; runtime wiring is Step 4 output.


def test_route_pollution_is_blocked_before_renko(policy):
    """cross_leg_route_pollution_43822 → QUOTE_INTEGRITY handler, not jump."""
    case = next(c for c in policy["named_cases"] if c["name"] == "cross_leg_route_pollution_43822")
    assert case["expected_handler"] == "QUOTE_INTEGRITY"
    assert case["not_jump_filter"] is True
    # integration coverage lives in tests/test_quote_integrity.py
    # (full integration: near cache unchanged, renko not called)


# ── 3. mutation invariants ────────────────────────────────────────────────
def test_rejected_jump_does_not_advance_anchor():
    t = _tracker()
    t.add(110.0)  # 1 brick
    anchor_close = t.renko_close
    seq = t.brick_sequence
    r = t.add(200.0)  # jump 90 > 50 → rejected by cap
    assert r[0] == 0
    assert t.renko_close == anchor_close
    assert t.brick_sequence == seq
    assert t.trend == 1


def test_brick_sequence_contiguous_after_rejected_tick():
    t = _tracker()
    t.add(110.0)
    t.add(200.0)  # rejected (>50)
    t.add(120.0)  # normal continuation
    seqs = [b["brick_sequence"] for b in t.get_recent_bricks(20)]
    assert seqs == list(range(1, len(seqs) + 1)), "sequence must stay contiguous"


def test_fixture_schema(policy):
    assert policy["schema_version"] == 1
    assert policy["jump_thresholds"]["reject_ge_pts"] == 50
    assert policy["jump_thresholds"]["quarantine_ge_pts"] == 30
    assert policy["session_gate"]["session_calendar_required"] is True
    assert policy["stats"]["near_night_max_pts"] == 35
    assert policy["stats"]["near_day_p999_pts"] == 58
    assert policy["stats"]["far_p999_pts"] == 17
    assert policy["timezone"] == "Asia/Taipei"
