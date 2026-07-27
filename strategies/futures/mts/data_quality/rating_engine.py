# 2026-07-27 Gemini CLI: Multi-dimensional Quality Rating Engine
from strategies.futures.mts.data_quality.contracts import QualityRating, SubsystemRatings


class RatingEngine:
    """
    Synthesizes subsystem ratings:
    - integrity_rating
    - ordering_rating
    - freshness_rating
    - writer_rating
    - generation_rating
    - overall_rating
    """

    def synthesize_ratings(
        self,
        exact_dups: int,
        cross_gen_contamination: int,
        severe_regressions: int,
        near_mon_rate: float,
        far_mon_rate: float,
        pair_skew_p99: float,
        unexpected_writer_gaps: int,
        cumulative_drops: int,
    ) -> SubsystemRatings:
        # Integrity Rating: Hard fail if cross gen contamination > 0 or drops > 100
        if cross_gen_contamination > 0:
            integrity = QualityRating.CAPTURE_INVALID.value
        elif exact_dups > 10 or cumulative_drops > 0:
            integrity = QualityRating.CAPTURE_DEGRADED.value
        else:
            integrity = QualityRating.CAPTURE_VALID.value

        # Ordering Rating: Hard fail if severe regressions > 5
        if severe_regressions > 5:
            ordering = QualityRating.CAPTURE_INVALID.value
        elif near_mon_rate < 0.99 or far_mon_rate < 0.99 or severe_regressions > 0:
            ordering = QualityRating.CAPTURE_DEGRADED.value
        else:
            ordering = QualityRating.CAPTURE_VALID.value

        # Freshness Rating: Degraded if P99 skew > 1000ms
        if pair_skew_p99 > 5000.0:
            freshness = QualityRating.CAPTURE_INVALID.value
        elif pair_skew_p99 > 1000.0:
            freshness = QualityRating.CAPTURE_DEGRADED.value
        else:
            freshness = QualityRating.CAPTURE_VALID.value

        # Writer Rating
        if unexpected_writer_gaps > 5:
            writer = QualityRating.CAPTURE_INVALID.value
        elif unexpected_writer_gaps > 0:
            writer = QualityRating.CAPTURE_DEGRADED.value
        else:
            writer = QualityRating.CAPTURE_VALID.value

        # Generation Rating
        generation = QualityRating.CAPTURE_INVALID.value if cross_gen_contamination > 0 else QualityRating.CAPTURE_VALID.value

        # Overall Rating
        sub_list = [integrity, ordering, freshness, writer, generation]
        if QualityRating.CAPTURE_INVALID.value in sub_list:
            overall = QualityRating.CAPTURE_INVALID.value
        elif QualityRating.CAPTURE_DEGRADED.value in sub_list:
            overall = QualityRating.CAPTURE_DEGRADED.value
        else:
            overall = QualityRating.CAPTURE_VALID.value

        return SubsystemRatings(
            overall_rating=overall,
            integrity_rating=integrity,
            ordering_rating=ordering,
            freshness_rating=freshness,
            writer_rating=writer,
            generation_rating=generation,
        )
