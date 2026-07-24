# 2026-07-24 Gemini CLI: Independent Acceptance Verifier Unit Tests
from pathlib import Path
import pytest

from strategies.futures.mts.acceptance_verifier import IndependentAcceptanceVerifier
from strategies.futures.mts.soak_collector import ShadowSoakCollector
from strategies.futures.mts.telemetry import ParityStatus, ParityTelemetryRecord


def test_independent_acceptance_verifier_pass(tmp_path: Path):
    """Verify independent verifier evaluates PASS when all 9 gates are satisfied."""
    gen_dir = tmp_path / "gen-test-pass"
    collector = ShadowSoakCollector(generation_id="gen-test-pass", base_dir=tmp_path, override_git_clean=True)
    collector.git_commit = "09e73dbc"

    collector.record_market_callback()
    for _ in range(120):
        collector.record_coverage_scenario("no_op", session="DAY")
        collector.logger.record_cycle(ParityTelemetryRecord(record_type="MATCH", parity_status=ParityStatus.MATCH))
    for _ in range(120):
        collector.record_coverage_scenario("near_release", session="NIGHT")
        collector.logger.record_cycle(ParityTelemetryRecord(record_type="MATCH", parity_status=ParityStatus.MATCH))

    for _ in range(6):
        collector.record_coverage_scenario("single_leg", session="DAY")

    collector.record_coverage_scenario("restart", session="DAY")

    manifest = collector.close_and_export_manifest()

    verifier = IndependentAcceptanceVerifier(
        generation_dir=collector.base_dir,
        expected_rc_commit="09e73dbc",
        min_day_cycles=100,
        min_night_cycles=100,
        min_total_cycles=200,
        min_lifecycles=5,
    )

    report = verifier.verify()
    assert report.digest_verified is True
    assert report.runtime_to_disk_reconciled is True
    assert report.overall_status == "PASS"


def test_independent_acceptance_verifier_invalid_sha256(tmp_path: Path):
    """Verify independent verifier evaluates INVALID when SHA-256 digest is corrupted."""
    gen_dir = tmp_path / "gen-test-corrupt"
    collector = ShadowSoakCollector(generation_id="gen-test-corrupt", base_dir=tmp_path, override_git_clean=True)
    collector.close_and_export_manifest()

    # Corrupt manifest.json file after manifest export
    manifest_path = collector.base_dir / "manifest.json"
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + "\n// corrupted", encoding="utf-8")

    verifier = IndependentAcceptanceVerifier(generation_dir=collector.base_dir, expected_rc_commit=collector.git_commit)
    report = verifier.verify()
    assert report.overall_status == "INVALID"
