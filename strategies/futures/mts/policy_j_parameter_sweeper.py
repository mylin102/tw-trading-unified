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
    activation_event_time: str = ""
    activation_net_pnl_twd: float = 0.0
    peak_net_pnl_twd: float = 0.0
    trigger_event_time: str = ""
    trigger_net_pnl_twd: float = 0.0
    hypothetical_fill_time: str = ""
    hypothetical_exit_price_near: float = 0.0
    hypothetical_exit_price_far: float = 0.0
    counterfactual_net_pnl_twd: float = 0.0
    actual_net_pnl_twd: float = 0.0
    delta_net_pnl_twd: float = 0.0
    actual_mfe_net_pnl_twd: float = 0.0
    ped_improvement_twd: float = 0.0
    fill_model: str = FillModel.EXECUTABLE.value

    @property
    def hypothetical_net_exit_pnl_twd(self) -> float | None:
        return self.counterfactual_net_pnl_twd if self.triggered else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConfigLandscapeSummary:
    """Summary Landscape Metrics per candidate config_hash."""
    config_hash: str
    activation_twd: float
    giveback_twd: float
    giveback_ratio: float
    source_trades: int
    eligible_trades: int
    triggered_trades: int
    trigger_rate: float
    total_delta_net_pnl: float
    mean_delta_per_source_trade: float   # PRIMARY ENDPOINT: Total Delta / Source Trades
    mean_delta_per_triggered_trade: float
    median_delta_net_pnl: float
    worst_delta_net_pnl: float
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
                    "shadow_enabled": True,
                    "activation_net_pnl_twd": act,
                    "giveback_twd": gb,
                    "max_quote_age_ms": 1000.0,
                }
                state = PolicyJShadowState(trade_id=tid)

                first_trig_snap = None
                act_time = ""
                act_pnl_val = 0.0
                peak_pnl_val = 0.0

                for snap in snaps:
                    obs = PolicyJShadowObservation(
                        trade_id=tid,
                        is_spread_phase=(snap.get("eligibility_reason") == "HEDGED_PAIR_SPREAD" or snap.get("eligibility_reason") is None or snap.get("is_spread_phase", True)),
                        is_hedged_pair=True,
                        exit_inflight=False,
                        gross_liquidation_pnl_twd=snap.get("gross_liquidation_pnl_twd", 0.0),
                        commission_twd=0.0,  # Pure net pnl passed in gross
                        near_quote_age_ms=snap.get("near_quote_age_ms", 10.0),
                        far_quote_age_ms=snap.get("far_quote_age_ms", 10.0),
                        event_time=snap.get("event_time", ""),
                    )
                    eval_snap, state = PolicyJShadowEvaluator.evaluate(obs, state, candidate_config)

                    if state.armed and not act_time:
                        act_time = eval_snap.event_time
                        act_pnl_val = eval_snap.estimated_net_exit_pnl_twd

                    if eval_snap.peak_net_exit_pnl_twd:
                        peak_pnl_val = eval_snap.peak_net_exit_pnl_twd

                    if eval_snap.would_trigger and first_trig_snap is None:
                        first_trig_snap = eval_snap

                if first_trig_snap:
                    trig = True
                    trig_time = first_trig_snap.event_time
                    trig_pnl = first_trig_snap.estimated_net_exit_pnl_twd
                    counterfactual_pnl = trig_pnl
                    delta_pnl = round(counterfactual_pnl - act_pnl, 2)
                    ped_imp = round((mfe_pnl - act_pnl) - (mfe_pnl - counterfactual_pnl), 2) if mfe_pnl else 0.0
                else:
                    trig = False
                    trig_time = ""
                    trig_pnl = 0.0
                    counterfactual_pnl = act_pnl
                    delta_pnl = 0.0
                    ped_imp = 0.0

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
                    activation_event_time=act_time,
                    activation_net_pnl_twd=act_pnl_val,
                    peak_net_pnl_twd=peak_pnl_val,
                    trigger_event_time=trig_time,
                    trigger_net_pnl_twd=trig_pnl,
                    hypothetical_fill_time=trig_time,
                    hypothetical_exit_price_near=0.0,
                    hypothetical_exit_price_far=0.0,
                    counterfactual_net_pnl_twd=counterfactual_pnl,
                    actual_net_pnl_twd=act_pnl,
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
            trig_cells = [c for c in cells if c.triggered]
            trig_n = len(trig_cells)
            trig_rate = round(trig_n / eligible_n, 4) if eligible_n > 0 else 0.0

            deltas = [c.delta_net_pnl_twd for c in cells]
            peds = [c.ped_improvement_twd for c in cells]

            sorted_deltas = sorted(deltas)
            total_delta = round(sum(deltas), 2)
            mean_delta_source = round(total_delta / n_trades, 2) if n_trades > 0 else 0.0
            mean_delta_trig = round(total_delta / trig_n, 2) if trig_n > 0 else 0.0
            median_delta = round(sorted_deltas[eligible_n // 2], 2) if eligible_n > 0 else 0.0
            worst_delta = sorted_deltas[0] if sorted_deltas else 0.0

            win_n = sum(1 for d in deltas if d > 0)
            win_rate = round(win_n / eligible_n, 4) if eligible_n > 0 else 0.0
            ped_total = round(sum(peds), 2)

            p10_idx = int(eligible_n * 0.1)
            p05_idx = int(eligible_n * 0.05)
            p10_delta = sorted_deltas[p10_idx] if eligible_n > 0 else 0.0
            p05_delta = sorted_deltas[p05_idx] if eligible_n > 0 else 0.0

            max_single = max(deltas) if deltas else 0.0
            top1_ratio = round(max_single / total_delta, 4) if total_delta > 0 else 0.0
            loto_min = round(total_delta - max_single, 2)

            summary = ConfigLandscapeSummary(
                config_hash=cfg_hash,
                activation_twd=act,
                giveback_twd=gb,
                giveback_ratio=gb_ratio,
                source_trades=n_trades,
                eligible_trades=eligible_n,
                triggered_trades=trig_n,
                trigger_rate=trig_rate,
                total_delta_net_pnl=total_delta,
                mean_delta_per_source_trade=mean_delta_source,
                mean_delta_per_triggered_trade=mean_delta_trig,
                median_delta_net_pnl=median_delta,
                worst_delta_net_pnl=worst_delta,
                win_rate=win_rate,
                ped_improvement_total=ped_total,
                p10_delta_net_pnl=p10_delta,
                p05_delta_net_pnl=p05_delta,
                top1_trade_contribution_ratio=top1_ratio,
                leave_one_out_min_delta=loto_min,
            )
            summaries.append(summary)

        return dataset_c_cells, summaries

        return dataset_c_cells, summaries
