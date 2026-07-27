# 2026-07-27 Gemini CLI: DTI-001C Data Quality Report Builder
from typing import Any, Dict, List, Optional

from strategies.futures.mts.data_quality.contracts import GenerationManifest, QualityAuditSummary
from strategies.futures.mts.data_quality.duplicate_audit import DuplicateAudit
from strategies.futures.mts.data_quality.freshness_audit import FreshnessAudit
from strategies.futures.mts.data_quality.generation_boundary_audit import GenerationBoundaryAudit
from strategies.futures.mts.data_quality.ordering_audit import OrderingAudit
from strategies.futures.mts.data_quality.rating_engine import RatingEngine


class DtiDataQualityReportBuilder:
    """
    Orchestrates DTI-001C Quality Audits over captured JSONL rows.
    """

    def build_quality_report(
        self,
        rows: List[Dict[str, Any]],
        manifest: Optional[GenerationManifest] = None,
    ) -> QualityAuditSummary:
        gen_id = manifest.generation_id if manifest else (rows[0].get("generation_id", "UNKNOWN") if rows else "UNKNOWN")

        # 1. Ordering Audit
        ordering_auditor = OrderingAudit()
        rates, ordering_counts = ordering_auditor.evaluate_ordering(rows)

        # 2. Duplicate Audit
        dup_auditor = DuplicateAudit()
        exact_dups, sem_dups = dup_auditor.evaluate_duplicates(rows)

        # 3. Freshness Audit
        freshness_auditor = FreshnessAudit()
        freshness_metrics = freshness_auditor.evaluate_freshness(rows)

        # 4. Generation Boundary Audit
        gen_auditor = GenerationBoundaryAudit()
        cross_gen_count, _ = gen_auditor.evaluate_boundaries(rows, expected_gen_id=gen_id if manifest else None)

        # 5. Rating Engine
        rating_engine = RatingEngine()
        ratings = rating_engine.synthesize_ratings(
            exact_dups=exact_dups,
            cross_gen_contamination=cross_gen_count,
            severe_regressions=ordering_counts["severe_regression_count"],
            near_mon_rate=rates.get("tmf_tick_monotonic_rate", 1.0),
            far_mon_rate=rates.get("tmfi6_tick_monotonic_rate", 1.0),
            pair_skew_p99=freshness_metrics["pair_skew_p99_ms"],
            unexpected_writer_gaps=0,
            cumulative_drops=0,
        )

        return QualityAuditSummary(
            generation_id=gen_id,
            ratings=ratings,
            total_rows_inspected=len(rows),
            exact_duplicates_count=exact_dups,
            semantic_duplicates_count=sem_dups,
            near_tick_monotonic_rate=rates.get("tmf_tick_monotonic_rate", 1.0),
            far_tick_monotonic_rate=rates.get("tmfi6_tick_monotonic_rate", 1.0),
            global_event_time_regression_rate=rates.get("global_event_time_regression_rate", 0.0),
            micro_reorder_count=ordering_counts["micro_reorder_count"],
            network_reorder_count=ordering_counts["network_reorder_count"],
            severe_regression_count=ordering_counts["severe_regression_count"],
            pair_skew_p50_ms=freshness_metrics["pair_skew_p50_ms"],
            pair_skew_p90_ms=freshness_metrics["pair_skew_p90_ms"],
            pair_skew_p99_ms=freshness_metrics["pair_skew_p99_ms"],
            unexpected_writer_gaps_count=0,
            cross_generation_contamination_count=cross_gen_count,
            cumulative_drops_count=0,
        )
