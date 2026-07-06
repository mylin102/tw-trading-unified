"""
ADR-010 Fault Injection: restart reconciliation tests.

Injects synthetic state files for each non-terminal lifecycle state,
creates a TMFSpread instance, triggers _restore_position_state,
and verifies correct recovery.

Run: python3 -m pytest tests/strategies/test_adr_010_fault_injection.py -v
"""
import json
from datetime import datetime

import pytest

from strategies.plugins.futures.active.tmf_spread import (
    PositionLifecycle, PositionPhase, ReleaseGroup, ReleaseGroupStatus,
    TrailGroup, TrailGroupStatus, Leg, CancelStatus,
    EntryRiskSnapshot, lifecycle_to_dict, PositionPhase, Leg,
)
from strategies.plugins.futures.active import tmf_spread as m


@pytest.fixture
def strategy(tmp_path, monkeypatch):
    """Create TMFSpread with isolated state file path + monkeypatched _MTS_STATE_FILE."""
    from strategies.plugins.futures.active.tmf_spread import TMFSpread
    state_file = tmp_path / "mts_position_state.json"
    monkeypatch.setattr(m, "_MTS_STATE_FILE", str(state_file))
    s = TMFSpread()
    s._has_position = False
    s._lifecycle_oca = PositionLifecycle()
    s._lifecycle = "FLAT"
    s._released_leg = None
    s._peak = 0.0
    s._nadir = 0.0
    s._entry_spread_z = 0.0
    s._entry_ts = datetime.now()
    s._release_ts = None
    s._near_entry = 0.0
    s._far_entry = 0.0
    s._near_side = None
    s._far_side = None
    s._trade_id = "fi-test"
    return s


def _base_state(phase_val: str) -> dict:
    return {
        "has_position": True,
        "state": phase_val,
        "near_entry": 46734.0,
        "far_entry": 46985.0,
        "near_side": "LONG",
        "far_side": "SHORT",
        "trade_id": "mts-fi-test",
        "_updated": datetime.now().isoformat(),
        "entry_spread_z": 2.5,
        "remaining_side": "SHORT",
        "trail_peak": 100.0,
        "trail_nadir": -50.0,
        "released_leg": None,
    }


def _restore(strategy, state: dict) -> dict:
    """Inject state file and call _restore_position_state(). Returns snapshot dict."""
    with open(m._MTS_STATE_FILE, "w") as f:
        json.dump(state, f, default=str)
    result = strategy._restore_position_state()
    _rg = strategy._lifecycle_oca.release_group
    _tl = strategy._lifecycle_oca.trail_group
    return {
        "restored": result,
        "has_position": strategy._has_position,
        "phase": strategy._lifecycle_oca.phase.value,
        "rg_status": _rg.status.value,
        "tl_status": _tl.status.value,
        "near_order_id": _rg.near_order_id,
        "far_order_id": _rg.far_order_id,
        "filled_leg": _rg.filled_leg.value if _rg.filled_leg else None,
        "sibling_cancel_status": (
            _rg.sibling_cancel_status.value if _rg.sibling_cancel_status else None
        ),
    }


# ═══════════════════════════════════════════
# FI-1: Restart @ SUBMITTED
# ═══════════════════════════════════════════

def test_fi_1_restart_submitted(strategy):
    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.SUBMITTED,
            near_order_id="ORD-NEAR-FI1",
            far_order_id="ORD-FAR-FI1",
        ),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    state = _base_state(PositionPhase.SPREAD.value) | {"lifecycle": lifecycle_to_dict(lc)}
    r = _restore(strategy, state)
    assert r["restored"] is True
    assert r["near_order_id"] == "ORD-NEAR-FI1"
    assert r["far_order_id"] == "ORD-FAR-FI1"
    assert r["tl_status"] == TrailGroupStatus.INACTIVE.value
    assert r["rg_status"] == ReleaseGroupStatus.SUBMITTED.value
    print(f"✅ FI-1: SUBMITTED → near={r['near_order_id']} far={r['far_order_id']}")


# ═══════════════════════════════════════════
# FI-2: Restart @ CANCELING_SIBLING
# ═══════════════════════════════════════════

def test_fi_2_restart_canceling_sibling(strategy):
    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.CANCELING_SIBLING,
            near_order_id="ORD-NEAR-FI2",
            far_order_id="ORD-FAR-FI2",
            filled_leg=Leg.NEAR,
            filled_order_id="ORD-NEAR-FI2",
            canceled_leg=Leg.FAR,
            sibling_cancel_order_id="ORD-FAR-FI2",
            sibling_cancel_status=CancelStatus.PENDING,
        ),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    state = _base_state(PositionPhase.SPREAD.value) | {"lifecycle": lifecycle_to_dict(lc)}
    r = _restore(strategy, state)
    assert r["phase"] == PositionPhase.SINGLE_LEG.value
    assert r["rg_status"] == ReleaseGroupStatus.SIBLING_CANCELED.value
    assert r["tl_status"] == TrailGroupStatus.ARMED.value
    assert r["filled_leg"] == Leg.NEAR.value
    print(f"✅ FI-2: CANCELING_SIBLING → SINGLE_LEG/{r['filled_leg']}")


# ═══════════════════════════════════════════
# FI-3: Restart @ SIBLING_CANCELED
# ═══════════════════════════════════════════

def test_fi_3_restart_sibling_canceled(strategy):
    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.SIBLING_CANCELED,
            far_order_id="ORD-FAR-FI3",
            filled_leg=Leg.FAR,
            filled_order_id="ORD-FAR-FI3",
            canceled_leg=Leg.NEAR,
            sibling_cancel_order_id="ORD-NEAR-FI3",
            sibling_cancel_status=CancelStatus.CONFIRMED,
        ),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    state = _base_state(PositionPhase.SPREAD.value) | {"lifecycle": lifecycle_to_dict(lc)}
    r = _restore(strategy, state)
    assert r["phase"] == PositionPhase.SINGLE_LEG.value
    assert r["tl_status"] == TrailGroupStatus.ARMED.value
    print(f"✅ FI-3: SIBLING_CANCELED → SINGLE_LEG trail={r['tl_status']}")


# ═══════════════════════════════════════════
# FI-4: Restart @ SUBMITTING (near only)
# ═══════════════════════════════════════════

def test_fi_4_restart_submitting(strategy):
    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.SUBMITTING,
            near_order_id="ORD-NEAR-FI4",
            far_order_id=None,
        ),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    state = _base_state(PositionPhase.SPREAD.value) | {"lifecycle": lifecycle_to_dict(lc)}
    r = _restore(strategy, state)
    assert r["rg_status"] == ReleaseGroupStatus.SUBMITTING.value
    assert r["near_order_id"] == "ORD-NEAR-FI4"
    assert r["far_order_id"] is None
    assert r["tl_status"] == TrailGroupStatus.INACTIVE.value
    assert r["phase"] == PositionPhase.SPREAD.value
    print(f"✅ FI-4: SUBMITTING (near={r['near_order_id']} far={r['far_order_id']})")


# ═══════════════════════════════════════════
# FI-5: Restart @ PARTIALLY_FILLED
# ═══════════════════════════════════════════

def test_fi_5_restart_partially_filled(strategy):
    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.PARTIALLY_FILLED,
            near_order_id="ORD-NEAR-FI5",
            far_order_id="ORD-FAR-FI5",
            filled_leg=Leg.NEAR,
            filled_order_id="ORD-NEAR-FI5",
            canceled_leg=Leg.FAR,
        ),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    state = _base_state(PositionPhase.SPREAD.value) | {"lifecycle": lifecycle_to_dict(lc)}
    r = _restore(strategy, state)
    assert r["rg_status"] == ReleaseGroupStatus.PARTIALLY_FILLED.value, f"got {r['rg_status']}"
    assert r["tl_status"] == TrailGroupStatus.INACTIVE.value
    assert r["filled_leg"] == Leg.NEAR.value
    print(f"✅ FI-5: PARTIALLY_FILLED → trail={r['tl_status']} filled_leg={r['filled_leg']}")
