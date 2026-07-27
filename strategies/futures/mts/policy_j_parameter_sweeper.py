# 2026-07-26 Gemini CLI: Wave J2-C Policy J Parameter Sweeper & Trajectory Replay Engine
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Tuple

from strategies.futures.mts.counterfactual_evidence_schema import (
    CounterfactualTradeFact,
    ExclusionReason,
    FillModel,
    calculate_counterfactual_metrics,
)
from strategies.futures.mts.policy_j_evidence_manifest import PolicyJEvidenceManifest, compute_reproduction_hash
from strategies.futures.mts.policy_j_fill_model import LegQuote, PolicyJFillModel
from strategies.futures.mts.policy_j_shadow_evaluator import (
    PolicyJShadowEvaluator,
    PolicyJShadowObservation,
)
from strategies.futures.mts.policy_j_shadow_state import PolicyJShadowState
from strategies.futures.mts.policy_j_telemetry_schema import compute_policy_j_config_hash


# 11 Anchor Pairs specified in ADR-016 & Wave J2-C Spec
ANCHOR_PARAMETER_GRID: List[Tuple[float, float]] = [
    (200.0, 50.0),
    (200.0, 100.0),
    (300.0, 50.0),
    (300.0, 100.0),
    (300.0, 150.0),
    (400.0, 100.0),
    (400.0, 150.0),
    (400.0, 200.0),
    (500.0, 150.0),
    (500.0, 200.0),
    (600.0, 200.0),
]


@dataclass(frozen=True)
class DatasetCCell:
    """Dataset C Record: Fact per trade_id x candidate config_hash."""
    trade_id: str
    config_hash: str
    activation_twd: float
    giveback_twd: float
    giveback_ratio: float
    dataset_split: str                  # "DEVELOPMENT" / "VALIDATION" / "HOLDOUT"
    eligible_for_analysis: bool
    exclusion_reason: str
    triggered: bool
    first_trigger_time: str
    hypothetical_net_exit_pnl_twd: float
    actual_final_net_pnl_twd: float
    delta_net_pnl_twd: float
    actual_mfe_net_pnl_twd: float
    ped_improvement_twd: float
    fill_model: str = FillModel.EXECUTABLE.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConfigLandscapeSummary:
    """Summary Landscape Metrics per candidate config_hash."""
    config_hash: str
    activation_twd: float
    giveback_twd: float
    giveback_ratio: float
    eligible_trades: int
    triggered_trades: int
    trigger_rate: float
    mean_delta_net_pnl: float
    median_delta_net_pnl: float
    total_delta_net_pnl: float
    win_rate: float
    ped_improvement_total: float
    p10_delta_net_pnl: float
    p05_delta_net_pnl: float
    top1_trade_contribution_ratio: float
    leave_one_out_min_delta: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PolicyJParameterSweeper:
    """
    Parameter Sweeper & Trajectory Replay Engine (Wave J2-C).
    Replays full observation trajectories per trade x per candidate config pair.
    """

    def __init__(
        self,
        grid: List[Tuple[float, float]] = None,
        fill_model: str = FillModel.EXECUTABLE.value,
    ):
        self.grid = grid if grid is not None else ANCHOR_PARAMETER_GRID
        self.fill_model = fill_model

    def sweep_landscape(
        self,
        shadow_snapshots: List[Dict[str, Any]],
        actual_trade_outcomes: List[Dict[str, Any]],
    ) -> Tuple[List[DatasetCCell], List[ConfigLandscapeSummary]]:
        """
        Execute full trajectory replay per candidate pair.
        Returns Dataset C cells and Summary Landscape per candidate config.
        """
        # Group observation snapshots by trade_id
        snaps_by_trade: Dict[str, List[Dict[str, Any]]] = {}
        for s in shadow_snapshots:
            tid = s.get("trade_id")
            if tid:
                snaps_by_trade.setdefault(tid, []).append(s)

        outcomes_by_trade: Dict[str, Dict[str, Any]] = {
            t["trade_id"]: t for t in actual_trade_outcomes if "trade_id" in t
        }

        all_trade_ids = sorted(list(outcomes_by_trade.keys()))
        n_trades = len(all_trade_ids)

        # Chronological Dataset Split (Dev 40%, Val 30%, Holdout 30%)
        dev_cutoff = int(n_trades * 0.4)
        val_cutoff = int(n_trades * 0.7)

        dataset_c_cells: List[DatasetCCell] = []
        cells_by_config: Dict[str, List[DatasetCCell]] = {}

        for idx, tid in enumerate(all_trade_ids):
            if idx < dev_cutoff:
                split = "DEVELOPMENT"
            elif idx < val_cutoff:
                split = "VALIDATION"
            else:
                split = "HOLDOUT"

            snaps = snaps_by_trade.get(tid, [])
            outcome = outcomes_by_trade.get(tid, {})
            act_pnl = outcome.get("actual_final_net_pnl_twd", 0.0)
            mfe_pnl = outcome.get("actual_mfe_net_pnl_twd", 0.0)

            for act, gb in self.grid:
                cfg_hash = compute_policy_j_config_hash({
                    "enabled": True,
                    "activation_net_pnl_twd": act,
                    "giveback_twd": gb,
                })
                gb_ratio = round(gb / act, 4) if act > 0 else 0.0

                # Replay PolicyJShadowEvaluator over snaps for candidate act/gb
                candidate_config = {
                    "enabled": True,
                    "activation_net_pnl_twd": act,
                    "giveback_twd": gb,
                    "max_quote_age_ms": 1000.0,
                }
                state = PolicyJShadowState(trade_id=tid)

                first_trig_snap = None
                for snap in snaps:
                    obs = PolicyJShadowObservation(
                        trade_id=tid,
                        is_spread_phase=(snap.get("eligibility_reason") == "HEDGED_PAIR_SPREAD"),
                        is_hedged_pair=True,
                        exit_inflight=False,
                        gross_liquidation_pnl_twd=snap.get("gross_liquidation_pnl_twd", 0.0),
                        commission_twd=snap.get("estimated_friction_twd", 92.0),
                        near_quote_age_ms=snap.get("near_quote_age_ms", 10.0),
                        far_quote_age_ms=snap.get("far_quote_age_ms", 10.0),
                        event_time=snap.get("event_time", ""),
                    )
                    eval_snap, state = PolicyJShadowEvaluator.evaluate(obs, state, candidate_config)
                    if eval_snap.first_trigger_event and first_trig_snap is None:
                        first_trig_snap = eval_snap

                if first_trig_snap:
                    trig = True
                    trig_time = first_trig_snap.event_time
                    hyp_pnl = first_trig_snap.estimated_net_exit_pnl_twd
                    delta_pnl = round(hyp_pnl - act_pnl, 2)
                    ped_imp = round((mfe_pnl - act_pnl) - (mfe_pnl - hyp_pnl), 2) if mfe_pnl else 0.0
                else:
                    trig = False
                    trig_time = ""
                    hyp_pnl = None
                    delta_pnl = None
                    ped_imp = None

                cell = DatasetCCell(
                    trade_id=tid,
                    config_hash=cfg_hash,
                    activation_twd=act,
                    giveback_twd=gb,
                    giveback_ratio=gb_ratio,
                    dataset_split=split,
                    eligible_for_analysis=True,
                    exclusion_reason=ExclusionReason.NONE.value,
                    triggered=trig,
                    first_trigger_time=trig_time,
                    hypothetical_net_exit_pnl_twd=hyp_pnl,
                    actual_final_net_pnl_twd=act_pnl,
                    delta_net_pnl_twd=delta_pnl,
                    actual_mfe_net_pnl_twd=mfe_pnl,
                    ped_improvement_twd=ped_imp,
                    fill_model=self.fill_model,
                )
                dataset_c_cells.append(cell)
                cells_by_config.setdefault(cfg_hash, []).append(cell)

        # Aggregate Landscape Summaries per candidate config
        summaries: List[ConfigLandscapeSummary] = []
        for act, gb in self.grid:
            cfg_hash = compute_policy_j_config_hash({
                "enabled": True,
                "activation_net_pnl_twd": act,
                "giveback_twd": gb,
            })
            gb_ratio = round(gb / act, 4) if act > 0 else 0.0
            cells = cells_by_config.get(cfg_hash, [])
            if not cells:
                continue

            eligible_n = len(cells)
            trig_cells = [c for c in cells if c.triggered and c.delta_net_pnl_twd is not None]
            trig_n = len(trig_cells)
            trig_rate = round(trig_n / eligible_n, 4) if eligible_n > 0 else 0.0

            if trig_cells:
                deltas = [c.delta_net_pnl_twd for c in trig_cells]
                peds = [c.ped_improvement_twd for c in trig_cells if c.ped_improvement_twd is not None]

                sorted_deltas = sorted(deltas)
                total_delta = round(sum(deltas), 2)
                mean_delta = round(total_delta / trig_n, 2)
                median_delta = round(sorted_deltas[trig_n // 2], 2)
                win_n = sum(1 for d in deltas if d > 0)
                win_rate = round(win_n / trig_n, 4)
                ped_total = round(sum(peds), 2)

                p10_idx = int(trig_n * 0.1)
                p05_idx = int(trig_n * 0.05)
                p10_delta = sorted_deltas[p10_idx]
                p05_delta = sorted_deltas[p05_idx]

                max_single = max(deltas)
                top1_ratio = round(max_single / total_delta, 4) if total_delta > 0 else 0.0
                loto_min = round(total_delta - max_single, 2)
            else:
                mean_delta = median_delta = total_delta = win_rate = ped_total = 0.0
                p10_delta = p05_delta = top1_ratio = loto_min = 0.0

            summary = ConfigLandscapeSummary(
                config_hash=cfg_hash,
                activation_twd=act,
                giveback_twd=gb,
                giveback_ratio=gb_ratio,
                eligible_trades=eligible_n,
                triggered_trades=trig_n,
                trigger_rate=trig_rate,
                mean_delta_net_pnl=mean_delta,
                median_delta_net_pnl=median_delta,
                total_delta_net_pnl=total_delta,
                win_rate=win_rate,
                ped_improvement_total=ped_total,
                p10_delta_net_pnl=p10_delta,
                p05_delta_net_pnl=p05_delta,
                top1_trade_contribution_ratio=top1_ratio,
                leave_one_out_min_delta=loto_min,
            )
            summaries.append(summary)

        return dataset_c_cells, summaries
