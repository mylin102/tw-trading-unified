# 2026-07-26 Gemini CLI: Unit tests for Wave J1.5-A Policy J Shadow Telemetry Contract
import json
from datetime import datetime
import pytest

from strategies.futures.mts.policy_j_telemetry_schema import (
    EligibilityReason,
    PolicyJShadowSnapshot,
    compute_policy_j_config_hash,
)


def test_policy_j_shadow_snapshot_immutability():
    """Verify PolicyJShadowSnapshot is frozen and immutable."""
    snapshot = PolicyJShadowSnapshot(
        trade_id="TRADE_20260726_001",
        event_time=datetime.now().isoformat(),
        would_trigger=False,
    )

    with pytest.raises(Exception):
        snapshot.would_trigger = True  # Must raise FrozenInstanceError


def test_policy_j_shadow_snapshot_serialization_roundtrip():
    """Verify JSON serialization and deserialization round-trip parity with enhanced fields."""
    config_hash = compute_policy_j_config_hash({"activation_net_pnl_twd": 300.0, "giveback_twd": 100.0})
    snapshot = PolicyJShadowSnapshot(
        snapshot_id="SNAP_20260726_0001",
        sequence_no=42,
        trade_id="TRADE_20260726_002",
        event_time="2026-07-26T10:42:18.327000",
        processed_at="2026-07-26T10:42:18.330000",
        mode="SHADOW_ONLY",
        eligible=True,
        eligibility_reason=EligibilityReason.HEDGED_PAIR_SPREAD.value,
        gross_liquidation_pnl_twd=520.0,
        estimated_friction_twd=100.0,
        estimated_net_exit_pnl_twd=420.0,
        peak_net_exit_pnl_twd=510.0,
        activation_net_pnl_twd=300.0,
        giveback_twd=100.0,
        would_trigger=False,
        execution_blocked=True,
        near_quote_age_ms=12,
        far_quote_age_ms=15,
        config_hash=config_hash,
    )

    json_str = snapshot.to_json()
    data_dict = json.loads(json_str)
    restored = PolicyJShadowSnapshot.from_dict(data_dict)

    assert restored == snapshot
    assert restored.execution_blocked is True
    assert restored.near_quote_age_ms == 12
    assert restored.far_quote_age_ms == 15
    assert restored.snapshot_id == "SNAP_20260726_0001"
    assert restored.sequence_no == 42
    assert restored.eligibility_reason == "HEDGED_PAIR_SPREAD"
    assert restored.compute_snapshot_hash() == snapshot.compute_snapshot_hash()
