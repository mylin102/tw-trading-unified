# 2026-07-27 Gemini CLI: DTI-001C Data Quality Audit Engine Tests
import pytest

from strategies.futures.mts.data_quality.contracts import GenerationManifest, QualityRating
from strategies.futures.mts.data_quality.report_builder import DtiDataQualityReportBuilder


def test_dti_001c_stream_partitioned_monotonicity():
    # Near and Far ticks interleaved asynchronously
    rows = [
        {"generation_id": "GEN_001", "contract_code": "TMF", "event_time": "2026-07-27T10:00:00.500"},
        {"generation_id": "GEN_001", "contract_code": "TMFI6", "event_time": "2026-07-27T10:00:00.420"},  # Global regression, but stream monotonic!
        {"generation_id": "GEN_001", "contract_code": "TMF", "event_time": "2026-07-27T10:00:00.600"},
        {"generation_id": "GEN_001", "contract_code": "TMFI6", "event_time": "2026-07-27T10:00:00.550"},
    ]

    builder = DtiDataQualityReportBuilder()
    report = builder.build_quality_report(rows)

    assert report.near_tick_monotonic_rate == 1.0
    assert report.far_tick_monotonic_rate == 1.0
    assert report.ratings.overall_rating == QualityRating.CAPTURE_VALID.value


def test_dti_001c_out_of_order_classification():
    rows = [
        {"generation_id": "GEN_001", "contract_code": "TMF", "event_time": "2026-07-27T10:00:01.000"},
        {"generation_id": "GEN_001", "contract_code": "TMF", "event_time": "2026-07-27T10:00:00.950"},  # Micro: -50ms
        {"generation_id": "GEN_001", "contract_code": "TMF", "event_time": "2026-07-27T10:00:00.500"},  # Network: -450ms
        {"generation_id": "GEN_001", "contract_code": "TMF", "event_time": "2026-07-27T10:00:03.000"},
        {"generation_id": "GEN_001", "contract_code": "TMF", "event_time": "2026-07-27T10:00:01.000"},  # Severe: -2000ms
    ]

    builder = DtiDataQualityReportBuilder()
    report = builder.build_quality_report(rows)

    assert report.micro_reorder_count == 1
    assert report.network_reorder_count == 1
    assert report.severe_regression_count == 1
    assert report.ratings.ordering_rating == QualityRating.CAPTURE_DEGRADED.value


def test_dti_001c_cross_generation_contamination_hard_gate():
    rows = [
        {"generation_id": "GEN_001", "contract_code": "TMF", "event_time": "2026-07-27T10:00:01.000"},
        {"generation_id": "GEN_002", "contract_code": "TMF", "event_time": "2026-07-27T10:00:02.000"},  # Cross-generation contamination!
    ]

    builder = DtiDataQualityReportBuilder()
    report = builder.build_quality_report(rows)

    assert report.cross_generation_contamination_count == 1
    assert report.ratings.overall_rating == QualityRating.CAPTURE_INVALID.value


def test_dti_001c_isolation_no_order_manager():
    import sys
    # Verify data_quality package does NOT import OrderManager
    import strategies.futures.mts.data_quality.report_builder
    assert "core.order_manager" not in sys.modules
