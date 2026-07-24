# 2026-07-24 Gemini CLI: Wave 1D.3 Shadow Soak Collector & Manifest Tests
import json
from pathlib import Path
import pytest

from strategies.futures.mts.soak_collector import ShadowSoakCollector
from strategies.futures.mts.telemetry import ParityStatus, ParityTelemetryRecord


def test_soak_collector_generation_directory_isolation(tmp_path: Path):
    """Verify collector creates isolated directory generation-001/ under base dir."""
    collector = ShadowSoakCollector(generation_id="001", base_dir=tmp_path / "shadow-soak", deployment_id="deploy-v1")

    assert collector.base_dir.exists()
    assert collector.base_dir.name == "generation-001"
    assert collector.raw_dir.exists()

    collector.close_and_export_manifest()


def test_soak_collector_manifest_export_and_sha256(tmp_path: Path):
    """Verify manifest.json and manifest.sha256 signature are correctly generated."""
    collector = ShadowSoakCollector(generation_id="002", base_dir=tmp_path / "shadow-soak", deployment_id="deploy-v2")

    collector.record_market_callback()
    collector.record_coverage_scenario("no_op", session="DAY")
    collector.record_coverage_scenario("near_release", session="NIGHT")

    # Record 2 MATCH cycles
    collector.logger.record_cycle(ParityTelemetryRecord(record_type="MATCH", parity_status=ParityStatus.MATCH))
    collector.logger.record_cycle(ParityTelemetryRecord(record_type="MATCH", parity_status=ParityStatus.MATCH))

    manifest = collector.close_and_export_manifest()

    manifest_path = collector.base_dir / "manifest.json"
    sha256_path = collector.base_dir / "manifest.sha256"

    assert manifest_path.exists()
    assert sha256_path.exists()

    # Verify SHA256 checksum format
    sha256_content = sha256_path.read_text(encoding="utf-8").strip()
    assert manifest.manifest_hash in sha256_content
    assert manifest.evaluation_accounting.matches == 2
    assert manifest.coverage.total_market_callbacks == 1
    assert manifest.coverage.eligible_decision_cycles == 2
    assert manifest.evaluate_soak_status() == "PASS"


def test_soak_collector_fail_closed_validation(tmp_path: Path):
    """Verify manifest evaluates to FAIL when unexplained mismatches or shadow order attempts occur."""
    collector = ShadowSoakCollector(generation_id="003", base_dir=tmp_path / "shadow-soak")
    collector.shadow_caused_orders = 1  # Non-interference violation

    manifest = collector.close_and_export_manifest()
    assert manifest.evaluate_soak_status() == "FAIL"
