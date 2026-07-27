# 2026-07-26 Gemini CLI: Wave J2-B Policy J Evidence Builder Engine
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from strategies.futures.mts.counterfactual_evidence_schema import (
    CounterfactualTradeFact,
    ExclusionReason,
    FillModel,
    calculate_counterfactual_metrics,
)
from strategies.futures.mts.policy_j_evidence_manifest import PolicyJEvidenceManifest, compute_reproduction_hash
from strategies.futures.mts.policy_j_fill_model import LegQuote, PolicyJFillModel


class PolicyJEvidenceBuilder:
    """
    Pipeline Builder for Policy J Evidence Dataset B & Coverage Manifest.
    Converts Dataset A (Shadow JSONL snapshots) and actual broker fills into Dataset B (CounterfactualTradeFact list).
    Enforces EXACTLY 1 record per trade_id (extracting first_trigger_event==True or triggered=False).
    """

    def __init__(self, fill_model: str = FillModel.EXECUTABLE.value):
        self.fill_model = fill_model

    def build_evidence(
        self,
        shadow_snapshots: List[Dict[str, Any]],
        actual_trade_outcomes: List[Dict[str, Any]],
    ) -> Tuple[List[CounterfactualTradeFact], PolicyJEvidenceManifest]:
        """
        Build canonical Dataset B facts and coverage manifest.
        """
        # Group snapshots by trade_id
        snapshots_by_trade: Dict[str, List[Dict[str, Any]]] = {}
        for s in shadow_snapshots:
            tid = s.get("trade_id")
            if tid:
                snapshots_by_trade.setdefault(tid, []).append(s)

        outcomes_by_trade: Dict[str, Dict[str, Any]] = {
            t["trade_id"]: t for t in actual_trade_outcomes if "trade_id" in t
        }

        all_trade_ids = sorted(list(set(list(snapshots_by_trade.keys()) + list(outcomes_by_trade.keys()))))

        trade_facts: List[CounterfactualTradeFact] = []
        exclusion_counts: Dict[str, int] = {reason.value: 0 for reason in ExclusionReason}

        source_count = len(all_trade_ids)
        joined_count = 0
        eligible_count = 0
        triggered_count = 0
        untriggered_count = 0
        excluded_count = 0

        for tid in all_trade_ids:
            snaps = snapshots_by_trade.get(tid, [])
            outcome = outcomes_by_trade.get(tid)

            if not snaps or not outcome:
                # Incomplete join
                reason = ExclusionReason.TELEMETRY_GAP.value if not snaps else ExclusionReason.RESTART_INCOMPLETE.value
                exclusion_counts[reason] += 1
                excluded_count += 1
                tf = CounterfactualTradeFact(
                    trade_id=tid,
                    session_date=snaps[0].get("session_date", "UNKNOWN") if snaps else outcome.get("session_date", "UNKNOWN"),
                    session=outcome.get("session", "DAY") if outcome else "DAY",
                    direction=outcome.get("direction", "BUY_NEAR_SELL_FAR") if outcome else "BUY_NEAR_SELL_FAR",
                    entry_time=snaps[0].get("event_time") if snaps else outcome.get("entry_time", ""),
                    first_trigger_time=None,
                    eligible_for_analysis=False,
                    exclusion_reason=reason,
                    actual_final_net_pnl_twd=outcome.get("actual_final_net_pnl_twd", 0.0) if outcome else 0.0,
                )
                trade_facts.append(tf)
                continue

            joined_count += 1

            # Check if any snapshot had stale quotes or single leg
            stale_snaps = [s for s in snaps if s.get("eligibility_reason") in ("NEAR_QUOTE_STALE", "FAR_QUOTE_STALE", "BOTH_QUOTES_STALE")]
            single_leg_snaps = [s for s in snaps if s.get("eligibility_reason") == "SINGLE_LEG_ONLY"]

            if single_leg_snaps:
                reason = ExclusionReason.SINGLE_LEG_ONLY.value
            elif stale_snaps:
                reason = ExclusionReason.QUOTE_STALE.value
            else:
                reason = ExclusionReason.NONE.value

            if reason != ExclusionReason.NONE.value:
                exclusion_counts[reason] += 1
                excluded_count += 1
                tf = CounterfactualTradeFact(
                    trade_id=tid,
                    session_date=outcome.get("session_date", "UNKNOWN"),
                    session=outcome.get("session", "DAY"),
                    direction=outcome.get("direction", "BUY_NEAR_SELL_FAR"),
                    entry_time=outcome.get("entry_time", snaps[0].get("event_time", "")),
                    first_trigger_time=None,
                    eligible_for_analysis=False,
                    exclusion_reason=reason,
                    actual_final_net_pnl_twd=outcome.get("actual_final_net_pnl_twd", 0.0),
                )
                trade_facts.append(tf)
                continue

            # Eligible trade
            eligible_count += 1

            # Find first_trigger_event == True snapshot
            first_trigger_snap = next((s for s in snaps if s.get("first_trigger_event") is True), None)

            act_pnl = outcome.get("actual_final_net_pnl_twd", 0.0)
            mfe_pnl = outcome.get("actual_mfe_net_pnl_twd")

            if first_trigger_snap:
                triggered_count += 1
                exclusion_counts[ExclusionReason.NONE.value] += 1
                trigger_time = first_trigger_snap.get("event_time")
                hyp_net_pnl = first_trigger_snap.get("estimated_net_exit_pnl_twd")

                # Compute fill prices using FillModel
                dir_str = outcome.get("direction", "BUY_NEAR_SELL_FAR")
                near_p = first_trigger_snap.get("near_executable_price", 22000.0)
                far_p = first_trigger_snap.get("far_executable_price", 22050.0)
                near_q = LegQuote(bid=near_p, ask=near_p + 1.0)
                far_q = LegQuote(bid=far_p, ask=far_p + 1.0)
                fill_res = PolicyJFillModel.compute_fill_prices(dir_str, near_q, far_q, fill_model=self.fill_model)

                metrics = calculate_counterfactual_metrics(hyp_net_pnl, act_pnl, mfe_pnl)

                tf = CounterfactualTradeFact(
                    trade_id=tid,
                    session_date=outcome.get("session_date", "UNKNOWN"),
                    session=outcome.get("session", "DAY"),
                    direction=dir_str,
                    entry_time=outcome.get("entry_time", snaps[0].get("event_time", "")),
                    first_trigger_time=trigger_time,
                    activation_twd=first_trigger_snap.get("activation_net_pnl_twd", 300.0),
                    giveback_twd=first_trigger_snap.get("giveback_twd", 100.0),
                    hypothetical_exit_price_near=fill_res.near_fill_price,
                    hypothetical_exit_price_far=fill_res.far_fill_price,
                    hypothetical_net_exit_pnl_twd=hyp_net_pnl,
                    actual_final_net_pnl_twd=act_pnl,
                    delta_net_pnl_twd=metrics["delta_net_pnl_twd"],
                    actual_mfe_net_pnl_twd=mfe_pnl,
                    ped_actual_twd=metrics["ped_actual_twd"],
                    ped_policy_j_twd=metrics["ped_policy_j_twd"],
                    ped_improvement_twd=metrics["ped_improvement_twd"],
                    fill_model=self.fill_model,
                    eligible_for_analysis=True,
                    exclusion_reason=ExclusionReason.NONE.value,
                    config_hash=first_trigger_snap.get("config_hash", ""),
                )
            else:
                untriggered_count += 1
                exclusion_counts[ExclusionReason.NONE.value] += 1
                tf = CounterfactualTradeFact(
                    trade_id=tid,
                    session_date=outcome.get("session_date", "UNKNOWN"),
                    session=outcome.get("session", "DAY"),
                    direction=outcome.get("direction", "BUY_NEAR_SELL_FAR"),
                    entry_time=outcome.get("entry_time", snaps[0].get("event_time", "")),
                    first_trigger_time=None,
                    activation_twd=snaps[0].get("activation_net_pnl_twd", 300.0),
                    giveback_twd=snaps[0].get("giveback_twd", 100.0),
                    hypothetical_exit_price_near=None,
                    hypothetical_exit_price_far=None,
                    hypothetical_net_exit_pnl_twd=None,
                    actual_final_net_pnl_twd=act_pnl,
                    delta_net_pnl_twd=None,
                    actual_mfe_net_pnl_twd=mfe_pnl,
                    ped_actual_twd=None,
                    ped_policy_j_twd=None,
                    ped_improvement_twd=None,
                    fill_model=self.fill_model,
                    eligible_for_analysis=True,
                    exclusion_reason=ExclusionReason.NONE.value,
                    config_hash=snaps[0].get("config_hash", ""),
                )
            trade_facts.append(tf)

        # Sort trade_facts deterministically by trade_id
        trade_facts.sort(key=lambda x: x.trade_id)

        facts_json = json.dumps([tf.to_dict() for tf in trade_facts], sort_keys=True)
        repro_hash = compute_reproduction_hash("ADR-016", "1.1", "2.0", self.fill_model, facts_json)

        manifest = PolicyJEvidenceManifest(
            adr_version="ADR-016",
            schema_version="1.1",
            builder_version="2.0",
            fill_model=self.fill_model,
            source_trade_count=source_count,
            joined_trade_count=joined_count,
            eligible_trade_count=eligible_count,
            triggered_trade_count=triggered_count,
            untriggered_trade_count=untriggered_count,
            excluded_trade_count=excluded_count,
            exclusion_reason_distribution=exclusion_counts,
            reproduction_hash=repro_hash,
        )

        return trade_facts, manifest
