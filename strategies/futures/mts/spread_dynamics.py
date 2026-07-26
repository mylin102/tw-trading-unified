# 2026-07-25 Gemini CLI: Dynamics Telemetry Contract v1 (Causal, Time-Aware, Validated Boundaries)
from dataclasses import dataclass
from enum import Enum
import math
import numpy as np
import pandas as pd

class DynamicsStatus(str, Enum):
    WARMING_UP = "WARMING_UP"
    READY = "READY"
    DUPLICATE_TIMESTAMP = "DUPLICATE_TIMESTAMP"
    TIME_REGRESSION = "TIME_REGRESSION"
    GAP_REANCHORED = "GAP_REANCHORED"

EXPECTED_ENTRY_SNAPSHOT_FIELDS = {
    "snapshot_id",
    "spread_z_at_entry",
    "velocity_ema_at_entry",
    "acceleration_ema_at_entry",
    "rolling_slope_at_entry",
    "slope_fit_r2_at_entry",
    "outward_momentum_ratio_at_entry",
    "regularized_outward_mr_at_entry",
    "outward_deceleration_index_at_entry",
    "inward_deceleration_index_at_entry",
    "motion_deceleration_at_entry",
    "velocity_ema_valid_at_entry",
    "acceleration_ema_valid_at_entry",
    "outward_momentum_ratio_valid_at_entry",
    "trend_ready_at_entry",
    "dynamics_status_at_entry",
    "dynamics_event_time_at_entry",
    "entry_decision_time",
    "dynamics_feature_age_ms",
    "dynamics_contract_version",
    "dynamics_feature_version",
    "dynamics_schema_version",
    "source_commit",
    "source_tree_dirty",
    "runtime_host_role",
    "calculation_config_hash",
}

def compute_snapshot_id(
    trade_id: str,
    entry_decision_time_iso: str,
    dynamics_event_time_iso: str,
    contract_version: str = "1.0",
) -> str:
    """
    Computes deterministic unique ID for entry snapshot to prevent duplicate joins or partial fill state ambiguities.
    """
    import hashlib
    raw_str = f"{trade_id}:{entry_decision_time_iso}:{dynamics_event_time_iso}:{contract_version}"
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()[:16]

def assert_event_ordering_invariants(
    event_time_iso: str,
    received_at_iso: str,
    processed_at_iso: str,
    entry_decision_time_iso: str,
    snapshot_persisted_at_iso: str | None = None,
) -> None:
    """
    Enforces event_time <= received_at <= processed_at <= entry_decision_time <= snapshot_persisted_at.
    """
    t_evt = pd.Timestamp(event_time_iso)
    t_rec = pd.Timestamp(received_at_iso)
    t_prc = pd.Timestamp(processed_at_iso)
    t_dec = pd.Timestamp(entry_decision_time_iso)

    if not (t_evt <= t_rec <= t_prc <= t_dec):
        raise ValueError(
            f"Event ordering violation: event({t_evt}) <= received({t_rec}) <= processed({t_prc}) <= decision({t_dec}) failed!"
        )

    if snapshot_persisted_at_iso is not None:
        t_pst = pd.Timestamp(snapshot_persisted_at_iso)
        if not (t_dec <= t_pst):
            raise ValueError(
                f"Persistence ordering violation: decision({t_dec}) <= persisted({t_pst}) failed!"
            )

def assert_valid_dynamics_provenance(
    source_commit: str,
    config_hash: str,
    host_role: str,
    source_tree_dirty: bool = False,
) -> None:
    """
    Fail-closed validation on provenance metadata.
    """
    invalid_placeholders = {None, "", "unknown", "dirty", "<git-sha>", "<sha256-hash>"}
    if source_commit in invalid_placeholders or not isinstance(source_commit, str):
        raise ValueError(f"Invalid source_commit provenance: {source_commit}")
    if config_hash in invalid_placeholders or not isinstance(config_hash, str):
        raise ValueError(f"Invalid calculation_config_hash provenance: {config_hash}")
    if host_role in invalid_placeholders or not isinstance(host_role, str):
        raise ValueError(f"Invalid runtime_host_role provenance: {host_role}")
    if host_role == "mini-live" and source_tree_dirty:
        raise ValueError("Mini Live production requires clean source tree (source_tree_dirty == False)")

@dataclass(frozen=True)
class SpreadDynamicsMetrics:
    """
    Dynamics Telemetry Contract v1 with explicit validity flags, quality contracts, and directional symmetry.
    """
    z: float
    dynamics_contract_version: str = "1.0"
    dynamics_feature_version: str = "1.0.0"
    
    # Derivatives
    z_velocity: float | None = None
    velocity_valid: bool = False
    velocity_ready: bool = False
    
    velocity_ema: float | None = None
    velocity_ema_valid: bool = False
    
    acceleration_ema: float | None = None
    acceleration_ema_valid: bool = False
    acceleration_ready: bool = False
    
    # Directional Symmetry & Outward Momentum
    outward_velocity: float | None = None
    outward_momentum_ratio: float | None = None         # NaN when |z| < 0.25 (Pure Research)
    outward_momentum_ratio_valid: bool = False
    regularized_outward_mr: float | None = None        # Floor regularized at 0.25 (Dashboard)
    momentum_ratio_signed_axis: float | None = None    # Raw axis
    
    # Deceleration Decomposition (Option A Contract: Clamped >= 0)
    motion_deceleration: float | None = None            # Unclamped directional motion decel
    is_outward_expanding: bool = False                  # outward_velocity > 0
    is_inward_reverting: bool = False                   # outward_velocity < 0
    outward_deceleration_index: float = 0.0             # Clamped >= 0 (Option A contract)
    inward_deceleration_index: float = 0.0              # Clamped >= 0 (Option A contract)
    
    # Causal Local Trend
    rolling_slope: float | None = None
    rolling_slope_valid: bool = False
    trend_ready: bool = False
    slope_fit_r2: float | None = None
    window_duration_sec: float = 0.0
    window_sample_count: int = 0
    
    # Quality & Stability Contract
    dynamics_status: DynamicsStatus = DynamicsStatus.WARMING_UP
    dt_sec: float = 0.0
    dynamics_schema_version: str = "1.0"

    def to_entry_snapshot(
        self,
        entry_decision_time_iso: str,
        dynamics_event_time_iso: str,
        trade_id: str = "trade-000",
        source_commit: str = "HEAD",
        runtime_host_role: str = "mini-live",
        config_hash: str = "",
        source_tree_dirty: bool = False,
    ) -> dict:
        """
        Builds complete point-in-time causal entry snapshot dictionary with unique snapshot_id.
        """
        assert_valid_dynamics_provenance(
            source_commit=source_commit,
            config_hash=config_hash,
            host_role=runtime_host_role,
            source_tree_dirty=source_tree_dirty,
        )

        snap_id = compute_snapshot_id(
            trade_id=trade_id,
            entry_decision_time_iso=entry_decision_time_iso,
            dynamics_event_time_iso=dynamics_event_time_iso,
            contract_version=self.dynamics_contract_version,
        )

        try:
            t_dec = pd.Timestamp(entry_decision_time_iso)
            t_evt = pd.Timestamp(dynamics_event_time_iso)
            feature_age_ms = round((t_dec - t_evt).total_seconds() * 1000.0, 2)
        except Exception:
            feature_age_ms = 0.0

        return {
            "snapshot_id": snap_id,
            "spread_z_at_entry": self.z,
            "velocity_ema_at_entry": self.velocity_ema,
            "acceleration_ema_at_entry": self.acceleration_ema,
            "rolling_slope_at_entry": self.rolling_slope,
            "slope_fit_r2_at_entry": self.slope_fit_r2,
            "outward_momentum_ratio_at_entry": self.outward_momentum_ratio,
            "regularized_outward_mr_at_entry": self.regularized_outward_mr,
            "outward_deceleration_index_at_entry": self.outward_deceleration_index,
            "inward_deceleration_index_at_entry": self.inward_deceleration_index,
            "motion_deceleration_at_entry": self.motion_deceleration,
            "velocity_ema_valid_at_entry": self.velocity_ema_valid,
            "acceleration_ema_valid_at_entry": self.acceleration_ema_valid,
            "outward_momentum_ratio_valid_at_entry": self.outward_momentum_ratio_valid,
            "trend_ready_at_entry": self.trend_ready,
            "dynamics_status_at_entry": getattr(self.dynamics_status, "value", str(self.dynamics_status)),
            "dynamics_event_time_at_entry": dynamics_event_time_iso,
            "entry_decision_time": entry_decision_time_iso,
            "dynamics_feature_age_ms": feature_age_ms,
            "dynamics_contract_version": self.dynamics_contract_version,
            "dynamics_feature_version": self.dynamics_feature_version,
            "dynamics_schema_version": self.dynamics_schema_version,
            "source_commit": source_commit,
            "source_tree_dirty": source_tree_dirty,
            "runtime_host_role": runtime_host_role,
            "calculation_config_hash": config_hash,
        }

class SpreadDynamicsCalculator:
    """
    Online calculator enforcing Dynamics Telemetry Contract v1.
    Guarantees state safety during feed gaps, duplicate timestamps, small |z| ratios, and feature-level readiness.
    """
    def __init__(
        self,
        tau_sec: float = 2.0,
        window_sec: float = 5.0,
        min_dt_sec: float = 0.001,
        max_derivative_gap_sec: float = 15.0,
        min_abs_z_for_ratio: float = 0.25,
        min_slope_samples: int = 5,
        min_slope_duration_sec: float = 1.0,
    ):
        self.tau_sec = tau_sec
        self.window_sec = window_sec
        self.min_dt_sec = min_dt_sec
        self.max_derivative_gap_sec = max_derivative_gap_sec
        self.min_abs_z_for_ratio = min_abs_z_for_ratio
        self.min_slope_samples = min_slope_samples
        self.min_slope_duration_sec = min_slope_duration_sec

        self._reset_state()

    @property
    def config_hash(self) -> str:
        import hashlib
        cfg_str = f"tau={self.tau_sec};w={self.window_sec};gap={self.max_derivative_gap_sec};z_min={self.min_abs_z_for_ratio};s_min={self.min_slope_samples};dur={self.min_slope_duration_sec}"
        return hashlib.sha256(cfg_str.encode("utf-8")).hexdigest()[:12]

    def _reset_state(self) -> None:
        self.last_ts: float | None = None
        self.last_z: float | None = None
        self.last_v_ema: float = 0.0
        self.v_ema: float = 0.0
        self.a_ema: float = 0.0
        self.valid_tick_count: int = 0
        self.history: list[tuple[float, float]] = []  # (ts, z)

    def update(self, ts: float, z: float) -> SpreadDynamicsMetrics:
        # First tick initialization
        if self.last_ts is None or self.last_z is None:
            self.last_ts = ts
            self.last_z = z
            self.valid_tick_count = 1
            self.history.append((ts, z))
            return SpreadDynamicsMetrics(
                z=z,
                dynamics_status=DynamicsStatus.WARMING_UP,
                window_duration_sec=0.0,
                window_sample_count=1,
            )

        dt = ts - self.last_ts

        # 1. Non-monotonic timestamp (Time regression) -> fail-closed
        if dt < 0:
            return SpreadDynamicsMetrics(
                z=z,
                dynamics_status=DynamicsStatus.TIME_REGRESSION,
                dt_sec=round(dt, 4),
            )

        # 2. Duplicate timestamp (dt == 0) -> keep internal state, mark current observation INVALID
        if dt == 0:
            return SpreadDynamicsMetrics(
                z=z,
                velocity_ema=round(self.v_ema, 4),
                velocity_ema_valid=False,
                acceleration_ema=round(self.a_ema, 4),
                acceleration_ema_valid=False,
                dynamics_status=DynamicsStatus.DUPLICATE_TIMESTAMP,
                dt_sec=0.0,
            )

        # 3. Feed gap re-anchoring (dt > max_derivative_gap_sec) -> COMPLETE STATE FLUSH
        if dt > self.max_derivative_gap_sec:
            self._reset_state()
            self.last_ts = ts
            self.last_z = z
            self.valid_tick_count = 1
            self.history.append((ts, z))
            return SpreadDynamicsMetrics(
                z=z,
                dynamics_status=DynamicsStatus.GAP_REANCHORED,
                dt_sec=round(dt, 4),
                window_duration_sec=0.0,
                window_sample_count=1,
            )

        # 4. Valid step calculation
        dt_calc = max(dt, self.min_dt_sec)
        dz = z - self.last_z
        v_raw = dz / dt_calc

        # Time-aware EMA: alpha = 1 - exp(-dt / tau)
        alpha = 1.0 - math.exp(-dt_calc / self.tau_sec)
        self.v_ema = alpha * v_raw + (1.0 - alpha) * self.v_ema

        dv_ema = self.v_ema - self.last_v_ema
        a_raw = dv_ema / dt_calc
        self.a_ema = alpha * a_raw + (1.0 - alpha) * self.a_ema

        self.last_v_ema = self.v_ema
        self.last_ts = ts
        self.last_z = z
        self.valid_tick_count += 1

        # Readiness Flags
        v_ready = self.valid_tick_count >= 2
        a_ready = self.valid_tick_count >= 3

        # Maintain causal time window [t - W, t]
        self.history.append((ts, z))
        cutoff = ts - self.window_sec
        self.history = [(t_val, z_val) for t_val, z_val in self.history if t_val >= cutoff]

        win_duration = self.history[-1][0] - self.history[0][0] if len(self.history) >= 2 else 0.0
        win_count = len(self.history)
        t_ready = (win_count >= self.min_slope_samples) and (win_duration >= self.min_slope_duration_sec)

        # Rolling slope & R²
        slope: float | None = None
        r2: float | None = None
        if t_ready:
            ts_arr = np.array([t_val for t_val, _ in self.history])
            z_arr = np.array([z_val for _, z_val in self.history])
            t_rel = ts_arr - ts_arr[0]
            
            poly, residuals, rank, singular_values, rcond = np.polyfit(t_rel, z_arr, 1, full=True)
            slope = float(poly[0])
            
            z_mean = np.mean(z_arr)
            ss_tot = float(np.sum((z_arr - z_mean) ** 2))
            if ss_tot > 1e-8 and len(residuals) > 0:
                ss_res = float(residuals[0])
                r2 = max(0.0, min(1.0, 1.0 - (ss_res / ss_tot)))
            else:
                r2 = None  # NaN if flat or indeterminate

        overall_ready = v_ready and a_ready and t_ready
        status = DynamicsStatus.READY if overall_ready else DynamicsStatus.WARMING_UP

        # Directional Symmetry & Outward Momentum Ratio Contract
        abs_z = abs(z)
        z_copysign = math.copysign(1.0, z) if z != 0 else 1.0
        outward_velocity = self.v_ema * z_copysign

        if abs_z < self.min_abs_z_for_ratio:
            outward_mr = None
            outward_mr_valid = False
        else:
            outward_mr = outward_velocity / abs_z
            outward_mr_valid = True

        regularized_outward_mr = outward_velocity / max(abs_z, self.min_abs_z_for_ratio)
        raw_mr = self.v_ema / (abs_z + 1e-5)

        # Deceleration Index Disambiguation (Option A: Clamped >= 0)
        v_copysign = math.copysign(1.0, self.v_ema) if self.v_ema != 0 else 1.0
        motion_decel = -self.a_ema * v_copysign
        is_outward = outward_velocity > 0
        is_inward = outward_velocity < 0

        outward_decel = max(motion_decel, 0.0) if is_outward else 0.0
        inward_decel = max(motion_decel, 0.0) if is_inward else 0.0

        return SpreadDynamicsMetrics(
            z=z,
            z_velocity=round(v_raw, 4) if v_ready else None,
            velocity_valid=v_ready,
            velocity_ready=v_ready,
            velocity_ema=round(self.v_ema, 4) if v_ready else None,
            velocity_ema_valid=v_ready,
            acceleration_ema=round(self.a_ema, 4) if a_ready else None,
            acceleration_ema_valid=a_ready,
            acceleration_ready=a_ready,
            outward_velocity=round(outward_velocity, 4) if v_ready else None,
            outward_momentum_ratio=round(outward_mr, 4) if (v_ready and outward_mr is not None) else None,
            outward_momentum_ratio_valid=outward_mr_valid and v_ready,
            regularized_outward_mr=round(regularized_outward_mr, 4) if v_ready else None,
            momentum_ratio_signed_axis=round(raw_mr, 4) if v_ready else None,
            motion_deceleration=round(motion_decel, 4) if a_ready else None,
            is_outward_expanding=is_outward if v_ready else False,
            is_inward_reverting=is_inward if v_ready else False,
            outward_deceleration_index=round(outward_decel, 4) if a_ready else 0.0,
            inward_deceleration_index=round(inward_decel, 4) if a_ready else 0.0,
            rolling_slope=round(slope, 4) if (t_ready and slope is not None) else None,
            rolling_slope_valid=t_ready and slope is not None,
            trend_ready=t_ready,
            slope_fit_r2=round(r2, 4) if (t_ready and r2 is not None) else None,
            window_duration_sec=round(win_duration, 4),
            window_sample_count=win_count,
            dynamics_status=status,
            dt_sec=round(dt, 4),
        )
