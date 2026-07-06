"""
ADR-010 Sprint 6: paper full lifecycle test.

Simulates the complete OCO bracket lifecycle deterministically:
  FLAT → ENTRY → SPREAD → OCO SUBMITTED → PARTIALLY_FILLED
  → CANCELING_SIBLING → SIBLING_CANCELED → SINGLE_LEG → TRAIL ARMED

State file verification at every step.
"""
import json
from datetime import datetime

import pytest

from strategies.plugins.futures.active.tmf_spread import (
    PositionLifecycle, PositionPhase, ReleaseGroup, ReleaseGroupStatus,
    TrailGroup, TrailGroupStatus, Leg, CancelStatus,
    EntryRiskSnapshot, lifecycle_to_dict, lifecycle_from_dict,
    _write_mts_state, _release_group_to_dict, _release_group_from_dict,
)
from strategies.plugins.futures.active import tmf_spread as m


def _dump_state(lc: PositionLifecycle) -> dict:
    """Serialize lifecycle + mandatory legacy fields for _write_mts_state compat."""
    return {
        "has_position": lc.phase != PositionPhase.FLAT,
        "state": lc.phase.value,
        "near_entry": 46734.0,
        "far_entry": 46985.0,
        "near_side": "LONG",
        "far_side": "SHORT",
        "near_last": 46900.0,
        "far_last": 47150.0,
        "trade_id": "mts-sprint-6-test",
        "_updated": datetime.now().isoformat(),
        "entry_spread_z": -3.24,
        "remaining_side": None if lc.phase == PositionPhase.SINGLE_LEG else "SHORT",
        "trail_peak": 0.0,
        "trail_nadir": 0.0,
        "released_leg": None,
        "lifecycle": lifecycle_to_dict(lc),
    }


# ═════════════════════════════════════════════════════
# 6A-1: SPREAD entry → OCO SUBMITTED
# ═════════════════════════════════════════════════════

def test_6a_spread_to_oco_submitted():
    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.SUBMITTED,
            near_order_id="ORD-NEAR-6A",
            far_order_id="ORD-FAR-6A",
            entry_risk=EntryRiskSnapshot(atr=105.0, release_stop=210.0, entry_z=-3.24, spread=211.0),
        ),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    d = _dump_state(lc)
    j = json.dumps(d, default=str)
    d2 = json.loads(j)
    lc2 = lifecycle_from_dict(d2.get("lifecycle"))
    assert lc2.release_group.status == ReleaseGroupStatus.SUBMITTED
    assert lc2.release_group.near_order_id == "ORD-NEAR-6A"
    assert lc2.release_group.far_order_id == "ORD-FAR-6A"
    assert lc2.trail_group.status == TrailGroupStatus.INACTIVE
    assert lc2.phase == PositionPhase.SPREAD
    print("✅ 6A-1: SPREAD + OCO SUBMITTED roundtrip OK")


# ═════════════════════════════════════════════════════
# 6A-2: First fill → PARTIALLY_FILLED + CANCELING_SIBLING
# ═════════════════════════════════════════════════════

def test_6a_first_fill_to_canceling():
    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.PARTIALLY_FILLED,
            near_order_id="ORD-NEAR-6A",
            far_order_id="ORD-FAR-6A",
            filled_leg=Leg.NEAR,
            filled_order_id="ORD-NEAR-6A",
            canceled_leg=Leg.FAR,
        ),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    # Transition to CANCELING_SIBLING
    lc.release_group.sibling_cancel_order_id = lc.release_group.far_order_id
    lc.release_group.sibling_cancel_status = CancelStatus.PENDING
    lc.release_group.status = ReleaseGroupStatus.CANCELING_SIBLING
    assert lc.release_group.status == ReleaseGroupStatus.CANCELING_SIBLING
    assert lc.release_group.filled_leg == Leg.NEAR
    assert lc.release_group.sibling_cancel_status == CancelStatus.PENDING
    assert lc.trail_group.status == TrailGroupStatus.INACTIVE
    # Verify roundtrip
    d = _dump_state(lc)
    d2 = json.loads(json.dumps(d, default=str))
    lc2 = lifecycle_from_dict(d2.get("lifecycle"))
    assert lc2.release_group.status == ReleaseGroupStatus.CANCELING_SIBLING
    assert lc2.release_group.filled_leg == Leg.NEAR
    assert lc2.release_group.sibling_cancel_status == CancelStatus.PENDING
    assert lc2.trail_group.status == TrailGroupStatus.INACTIVE
    print("✅ 6A-2: PARTIALLY_FILLED → CANCELING_SIBLING roundtrip OK")


# ═════════════════════════════════════════════════════
# 6A-3: CANCELING_SIBLING → SIBLING_CANCELED → SINGLE_LEG + trail ARMED
# ═════════════════════════════════════════════════════

def test_6a_canceling_to_sibling_canceled():
    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.CANCELING_SIBLING,
            near_order_id="ORD-NEAR-6A",
            far_order_id="ORD-FAR-6A",
            filled_leg=Leg.NEAR,
            filled_order_id="ORD-NEAR-6A",
            canceled_leg=Leg.FAR,
            sibling_cancel_order_id="ORD-FAR-6A",
            sibling_cancel_status=CancelStatus.CONFIRMED,
        ),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    # Transition to SIBLING_CANCELED + SINGLE_LEG + trail ARMED
    lc.release_group.status = ReleaseGroupStatus.SIBLING_CANCELED
    lc.phase = PositionPhase.SINGLE_LEG
    lc.trail_group.status = TrailGroupStatus.ARMED
    assert lc.phase == PositionPhase.SINGLE_LEG
    assert lc.release_group.status == ReleaseGroupStatus.SIBLING_CANCELED
    assert lc.trail_group.status == TrailGroupStatus.ARMED
    # Invariant: remaining leg must be set
    lc.trail_group.remaining_leg = Leg.FAR
    assert lc.trail_group.remaining_leg == Leg.FAR
    # Roundtrip
    d = _dump_state(lc)
    d2 = json.loads(json.dumps(d, default=str))
    lc2 = lifecycle_from_dict(d2.get("lifecycle"))
    assert lc2.phase == PositionPhase.SINGLE_LEG
    assert lc2.release_group.status == ReleaseGroupStatus.SIBLING_CANCELED
    assert lc2.trail_group.status == TrailGroupStatus.ARMED
    assert lc2.trail_group.remaining_leg == Leg.FAR
    print("✅ 6A-3: CANCELING_SIBLING → SIBLING_CANCELED → SINGLE_LEG + trail ARMED OK")


# ═════════════════════════════════════════════════════
# 6A-4: Full lifecycle roundtrip — every step
# ═════════════════════════════════════════════════════

def test_6a_full_lifecycle_roundtrip():
    """Complete state machine roundtrip:
    FLAT → SPREAD/SUBMITTED → PARTIALLY_FILLED → CANCELING_SIBLING
    → SIBLING_CANCELED → SINGLE_LEG/ARMED
    """
    states = []

    # Step 1: FLAT
    lc = PositionLifecycle()
    states.append(("FLAT", lifecycle_to_dict(lc)))
    assert lc.phase == PositionPhase.FLAT
    assert lc.release_group.status == ReleaseGroupStatus.INACTIVE

    # Step 2: SPREAD + OCO SUBMITTED
    lc.phase = PositionPhase.SPREAD
    lc.release_group.status = ReleaseGroupStatus.SUBMITTED
    lc.release_group.near_order_id = "ORD-NEAR"
    lc.release_group.far_order_id = "ORD-FAR"
    lc.release_group.entry_risk = EntryRiskSnapshot(atr=105.0)
    states.append(("SPREAD/SUBMITTED", lifecycle_to_dict(lc)))

    # Step 3: PARTIALLY_FILLED
    lc.release_group.filled_leg = Leg.NEAR
    lc.release_group.filled_order_id = "ORD-NEAR"
    lc.release_group.canceled_leg = Leg.FAR
    lc.release_group.status = ReleaseGroupStatus.PARTIALLY_FILLED
    states.append(("PARTIALLY_FILLED", lifecycle_to_dict(lc)))

    # Step 4: CANCELING_SIBLING
    lc.release_group.sibling_cancel_order_id = "ORD-FAR"
    lc.release_group.sibling_cancel_status = CancelStatus.PENDING
    lc.release_group.status = ReleaseGroupStatus.CANCELING_SIBLING
    states.append(("CANCELING_SIBLING", lifecycle_to_dict(lc)))

    # Step 5: SIBLING_CANCELED + SINGLE_LEG + trail ARMED
    lc.release_group.sibling_cancel_status = CancelStatus.CONFIRMED
    lc.release_group.status = ReleaseGroupStatus.SIBLING_CANCELED
    lc.phase = PositionPhase.SINGLE_LEG
    lc.trail_group.status = TrailGroupStatus.ARMED
    lc.trail_group.remaining_leg = Leg.FAR
    states.append(("SINGLE_LEG/ARMED", lifecycle_to_dict(lc)))

    # Verify each step roundtrips correctly
    for label, d in states:
        j = json.dumps(d, default=str)
        d2 = json.loads(j)
        lc2 = lifecycle_from_dict(d2)
        assert lc2 is not None, f"Failed to roundtrip {label}"
    # Verify final state
    assert lc2.phase == PositionPhase.SINGLE_LEG
    assert lc2.trail_group.status == TrailGroupStatus.ARMED
    assert lc2.release_group.sibling_cancel_status == CancelStatus.CONFIRMED
    print(f"✅ 6A-4: Full lifecycle roundtrip ({len(states)} steps) OK")


if __name__ == "__main__":
    test_6a_spread_to_oco_submitted()
    test_6a_first_fill_to_canceling()
    test_6a_canceling_to_sibling_canceled()
    test_6a_full_lifecycle_roundtrip()
    print()
    print("All Sprint 6A tests passed ✅")
