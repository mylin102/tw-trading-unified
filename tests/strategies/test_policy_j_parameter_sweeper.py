# 2026-07-26 Gemini CLI: Unit tests for PolicyJParameterSweeper
import pytest

from strategies.futures.mts.policy_j_parameter_sweeper import ANCHOR_PARAMETER_GRID, PolicyJParameterSweeper


def test_parameter_sweeper_trajectory_replay():
    # Observation snapshots for a trade whose Net PnL reaches 350 TWD then drops to 220 TWD
    shadow_snaps = [
        {
            "mode": "SHADOW_ONLY",
            "trade_id": "TRADE_SWEEP_001",
            "event_time": "2026-07-26T09:00:00",
            "eligible": True,
            "eligibility_reason": "HEDGED_PAIR_SPREAD",
            "gross_liquidation_pnl_twd": 442.0,
            "estimated_friction_twd": 92.0,
            "estimated_net_exit_pnl_twd": 350.0,
            "near_quote_age_ms": 10.0,
            "far_quote_age_ms": 10.0,
        },
        {
            "mode": "SHADOW_ONLY",
            "trade_id": "TRADE_SWEEP_001",
            "event_time": "2026-07-26T09:05:00",
            "eligible": True,
            "eligibility_reason": "HEDGED_PAIR_SPREAD",
            "gross_liquidation_pnl_twd": 312.0,
            "estimated_friction_twd": 92.0,
            "estimated_net_exit_pnl_twd": 220.0,
            "near_quote_age_ms": 10.0,
            "far_quote_age_ms": 10.0,
        },
    ]

    outcomes = [
        {
            "trade_id": "TRADE_SWEEP_001",
            "session_date": "20260726",
            "session": "DAY",
            "direction": "BUY_NEAR_SELL_FAR",
            "entry_time": "2026-07-26T09:00:00",
            "actual_final_net_pnl_twd": 100.0,
            "actual_mfe_net_pnl_twd": 350.0,
        }
    ]

    sweeper = PolicyJParameterSweeper(grid=[(300.0, 100.0), (400.0, 100.0)])
    cells, summaries = sweeper.sweep_landscape(shadow_snaps, outcomes)

    # For candidate (300, 100): Armed at 350 (>=300). Drops to 220 (Peak 350 - 100 = 250 -> Triggered at 220).
    cell_300 = next(c for c in cells if c.activation_twd == 300.0)
    assert cell_300.triggered is True
    assert cell_300.hypothetical_net_exit_pnl_twd == 220.0
    assert cell_300.delta_net_pnl_twd == 120.0  # 220 - 100

    # For candidate (400, 100): Peak is 350 (<400 -> Never Armed -> Never Triggered).
    cell_400 = next(c for c in cells if c.activation_twd == 400.0)
    assert cell_400.triggered is False
    assert cell_400.hypothetical_net_exit_pnl_twd is None
    assert cell_400.delta_net_pnl_twd is None


def test_parameter_sweeper_landscape_summary_and_determinism():
    # 10 trade outcomes
    outcomes = [
        {
            "trade_id": f"TRADE_{i:03d}",
            "session_date": "20260726",
            "session": "DAY",
            "direction": "BUY_NEAR_SELL_FAR",
            "entry_time": "2026-07-26T09:00:00",
            "actual_final_net_pnl_twd": 100.0,
            "actual_mfe_net_pnl_twd": 400.0,
        }
        for i in range(10)
    ]
    shadow_snaps = [
        {
            "mode": "SHADOW_ONLY",
            "trade_id": f"TRADE_{i:03d}",
            "event_time": "2026-07-26T09:00:00",
            "eligible": True,
            "eligibility_reason": "HEDGED_PAIR_SPREAD",
            "estimated_net_exit_pnl_twd": 350.0,
            "near_quote_age_ms": 10.0,
            "far_quote_age_ms": 10.0,
        }
        for i in range(10)
    ] + [
        {
            "mode": "SHADOW_ONLY",
            "trade_id": f"TRADE_{i:03d}",
            "event_time": "2026-07-26T09:05:00",
            "eligible": True,
            "eligibility_reason": "HEDGED_PAIR_SPREAD",
            "estimated_net_exit_pnl_twd": 220.0,
            "near_quote_age_ms": 10.0,
            "far_quote_age_ms": 10.0,
        }
        for i in range(10)
    ]

    sweeper = PolicyJParameterSweeper(grid=ANCHOR_PARAMETER_GRID)
    cells1, sum1 = sweeper.sweep_landscape(shadow_snaps, outcomes)
    cells2, sum2 = sweeper.sweep_landscape(shadow_snaps, outcomes)

    # Check anchor grid length = 11
    assert len(sum1) == 11
    # Check dataset split distribution across 10 trades
    splits = set(c.dataset_split for c in cells1)
    assert splits == {"DEVELOPMENT", "VALIDATION", "HOLDOUT"}

    # Check determinism across runs
    assert [s.to_dict() for s in sum1] == [s.to_dict() for s in sum2]
