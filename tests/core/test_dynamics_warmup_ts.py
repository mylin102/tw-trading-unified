"""Dynamics warm-up must not be reset by the 5-minute bar cadence: the
calculator's max_derivative_gap_sec=15 rejects dt>15s, so feeding bar
timestamps (300s apart) resets the sample counter every update and dz /
spread_slope never form — the entry evaluation never leaves
CANDIDATE_AWAITING_EVALUATION.  The wiring must use real arrival time.
"""
import time

from strategies.futures.mts.spread_dynamics import SpreadDynamicsCalculator
from tests.core.test_fills_recovery_live_gate import _AutoMonitor


def _monitor():
    mon = _AutoMonitor.__new__(_AutoMonitor)
    mon._spread_dynamics = SpreadDynamicsCalculator()
    return mon


def test_warmup_progresses_with_real_arrival_time(monkeypatch):
    mon = _monitor()
    # bars carry 5-minute-cadence timestamps; real arrival time ticks fast
    now = 1755200000.0
    monkeypatch.setattr(time, "time", lambda: now)
    bar1 = {"ts": 1755180000.0, "near_close": 45856.0, "far_close": 45986.0,
            "spread_z": 15.59}
    bar2 = {"ts": 1755180300.0, "near_close": 45850.0, "far_close": 45980.0,
            "spread_z": 15.0}
    mon._apply_spread_dynamics(bar1)
    now += 3.0  # real arrival 3s later — inside max_gap
    mon._apply_spread_dynamics(bar2)
    # bar-ts dt would be 300s (>15s gap → reset); arrival dt=3s accumulates
    assert bar2.get("dz") is not None
    assert "spread_slope" in bar2
    assert bar2.get("velocity_ema") is not None
