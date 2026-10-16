"""Pre-registered theta families (per-metric units/rationale) — A4 engine.

No ex-post selection: every theta is pre-registered with explicit units and
rationale; nested threshold sweeps report ALL rows, never a winner.
"""

THETA_REGISTRY = {
    "a4_leg_recovery": {
        "unit": "ticks",
        "rationale": "loss-leg recovery distance from the post-breach trough",
        "grid": [30, 60, 120],
    },
    "a4_combined_recovery": {
        "unit": "TWD",
        "rationale": "combined-net recovery from the post-breach trough",
        "grid": [50, 100, 200],
    },
    "a4_z_reversal": {
        "unit": "z-score",
        "rationale": "|z| reversal threshold (breach arms risk, not release)",
        "grid": [1.5, 2.0, 2.5],
    },
    "a4_velocity_confirm": {
        "unit": "bars",
        "rationale": "velocity sign confirmation window",
        "grid": [2, 3, 5],
    },
    "a4_acceleration": {
        "unit": "z per bar",
        "rationale": "acceleration/deceleration threshold",
        "grid": [0.3, 0.5, 0.8],
    },
}

NESTED_THRESHOLDS = [0, -100, -200]


def theta_registry():
    """Per-metric pre-registered thetas: {metric: {unit, rationale, grid}}."""
    return {m: dict(spec) for m, spec in THETA_REGISTRY.items()}


def a4_leg_grid(recovery_distances):
    return {"unit": "ticks", "grid": list(recovery_distances)}


def a4_combined_grid(trough_recoveries):
    return {"unit": "TWD", "grid": list(trough_recoveries)}


def a4_spread_z_family(z_reversal, velocity, acceleration):
    return {"z_reversal": z_reversal, "velocity_confirm": velocity,
            "acceleration": acceleration}


def sweep_plan(single_factor, factorial_subset):
    """Single-factor sweeps first, then a small declared factorial subset;
    0/-100/-200 are NESTED sensitivity rows — all reported, no winner."""
    return {
        "reports_all_thetas": True,
        "nested_threshold_rows": [
            {"threshold": t} for t in NESTED_THRESHOLDS],
        "single_factor": list(single_factor),
        "factorial_subset": dict(factorial_subset or {}),
    }
