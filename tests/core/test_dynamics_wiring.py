"""Research wiring gap: the entry_observation records read dz /
spread_slope / velocity_ema from the bar dict, but the monitor never calls
the existing SpreadDynamicsCalculator — those columns are always NULL and
the spread itself is never written into the candidate payload.  Wire the
calculator into the bar pipeline.
"""
from types import SimpleNamespace

from strategies.futures.mts.spread_dynamics import SpreadDynamicsCalculator
from tests.core.test_fills_recovery_live_gate import _AutoMonitor


def _monitor():
    mon = _AutoMonitor.__new__(_AutoMonitor)
    mon._spread_dynamics = SpreadDynamicsCalculator()
    return mon


def test_apply_spread_dynamics_wires_dz_slope_velocity_and_spread():
    mon = _monitor()
    bar1 = {"ts": 1000.0, "near_close": 45856.0, "far_close": 45986.0,
            "spread_z": 15.59}
    bar2 = {"ts": 1005.0, "near_close": 45850.0, "far_close": 45980.0,
            "spread_z": 15.0}
    mon._apply_spread_dynamics(bar1)
    mon._apply_spread_dynamics(bar2)
    # velocity ready after 2 valid ticks
    assert bar1.get("spread") == -130.0
    assert bar2.get("dz") is not None
    assert "spread_slope" in bar2
    assert bar2.get("velocity_ema") is not None


def test_apply_spread_dynamics_noop_without_z():
    mon = _monitor()
    bar = {"ts": 1000.0, "near_close": 45856.0, "far_close": 45986.0}
    mon._apply_spread_dynamics(bar)
    assert "dz" not in bar
    assert "velocity_ema" not in bar
