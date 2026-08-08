"""Pre-registered theta families (per-metric, units/rationale) — skeletal."""


def theta_registry():
    raise NotImplementedError("families.theta_registry: per-metric pre-registered thetas with explicit units + rationale (no ex-post selection)")


def a4_leg_grid(recovery_distances):
    raise NotImplementedError("families.a4_leg_grid: recovery distances grid")


def a4_combined_grid(trough_recoveries):
    raise NotImplementedError("families.a4_combined_grid: recovery-from-post-breach trough grid")


def a4_spread_z_family(z_reversal, velocity, acceleration):
    raise NotImplementedError("families.a4_spread_z_family: |z| / velocity sign+confirmation / acceleration-deceleration")


def sweep_plan(single_factor, factorial_subset):
    raise NotImplementedError("families.sweep_plan: single-factor first, small declared factorial; 0/-100/-200 are NESTED sensitivity, never best-threshold selection")
