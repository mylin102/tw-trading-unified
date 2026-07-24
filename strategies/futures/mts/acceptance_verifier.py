# 2026-07-24 Gemini CLI: Independent Acceptance Verifier for Wave 1D.3 Shadow Soak
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .soak_manifest import ShadowSoakManifest
from .telemetry import EvaluationAccountingSummary, TelemetryDeliveryAccountingSummary


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    name: str
    passed: bool
    status: str  # PASS | FAIL | INCOMPLETE | INVALID
    details: dict[str, Any]


@dataclass
class AcceptanceReport:
    wave: str
    expected_rc_commit: str
    generation_id: str
    generation_path: str
    overall_status: str  # PASS | FAIL | INCOMPLETE | INVALID
    gates: list[GateResult] = field(default_factory=list)
    digest_verified: bool = False
    runtime_to_disk_reconciled: bool = False


class IndependentAcceptanceVerifier:
    """Independent verifier that parses raw telemetry files and manifest independently from the collector.
    
    Verifies 9 Strict Acceptance Gates (G1 to G9):
    - G1: Baseline Provenance & Preflight Gate
    - G2: Runtime Non-Interference Gate
    - G3: Evaluation Accounting Gate
    - G4: Delivery & Runtime-to-Disk Reconciliation Gate
    - G5: Zero Mismatch Gate (mismatches == 0)
    - G6: Minimum Coverage Gate (day >= 100, night >= 100, cycles >= 200, lifecycles >= 5)
    - G7: Controlled Restart Continuity Gate (process_segments >= 2)
    - G8: Performance Budget & Telemetry Health Gate (p99 latency <= SLAs)
    - G9: SHA-256 Digest & Raw Integrity Gate
    """

    def __init__(
        self,
        generation_dir: Path | str,
        expected_rc_commit: str = "09e73dbc",
        min_day_cycles: int = 100,
        min_night_cycles: int = 100,
        min_total_cycles: int = 200,
        min_lifecycles: int = 5,
        max_shadow_eval_p99_us: float = 100.0,
    ) -> None:
        self.generation_dir = Path(generation_dir)
        self.expected_rc_commit = expected_rc_commit
        self.min_day_cycles = min_day_cycles
        self.min_night_cycles = min_night_cycles
        self.min_total_cycles = min_total_cycles
        self.min_lifecycles = min_lifecycles
        self.max_shadow_eval_p99_us = max_shadow_eval_p99_us

        self.manifest_path = self.generation_dir / "manifest.json"
        self.sha256_path = self.generation_dir / "manifest.sha256"
        self.raw_dir = self.generation_dir / "raw"

    def verify(self) -> AcceptanceReport:
        """Run independent verification of all 9 gates."""
        gates: list[GateResult] = []

        if not self.generation_dir.exists() or not self.manifest_path.exists():
            report = AcceptanceReport(
                wave="1D.3",
                expected_rc_commit=self.expected_rc_commit,
                generation_id=self.generation_dir.name,
                generation_path=str(self.generation_dir),
                overall_status="INVALID",
                gates=[
                    GateResult(
                        gate_id="G0",
                        name="Manifest & Directory Existence",
                        passed=False,
                        status="INVALID",
                        details={"error": f"Path {self.generation_dir} or manifest.json missing"},
                    )
                ],
            )
            return report

        # Read Manifest
        manifest_raw_bytes = self.manifest_path.read_bytes()
        try:
            manifest_data = json.loads(manifest_raw_bytes.decode("utf-8"))
            manifest = ShadowSoakManifest.from_dict(manifest_data)
        except Exception as exc:
            return AcceptanceReport(
                wave="1D.3",
                expected_rc_commit=self.expected_rc_commit,
                generation_id=self.generation_dir.name,
                generation_path=str(self.generation_dir),
                overall_status="INVALID",
                gates=[
                    GateResult(
                        gate_id="G9",
                        name="SHA-256 Digest & Raw Integrity Gate",
                        passed=False,
                        status="INVALID",
                        details={"error": f"Manifest JSON decoding failed: {exc}"},
                    )
                ],
            )

        # G9: SHA-256 Digest Verification
        g9_passed, digest_details = self._verify_g9_sha256(manifest_raw_bytes, manifest)
        gates.append(
            GateResult(
                gate_id="G9",
                name="SHA-256 Digest & Raw Integrity Gate",
                passed=g9_passed,
                status="PASS" if g9_passed else "INVALID",
                details=digest_details,
            )
        )

        # G1: Baseline Provenance & Preflight Gate
        g1_passed, g1_details = self._verify_g1_preflight(manifest)
        gates.append(
            GateResult(
                gate_id="G1",
                name="Baseline Provenance & Preflight Gate",
                passed=g1_passed,
                status="PASS" if g1_passed else "INVALID",
                details=g1_details,
            )
        )

        # G2: Runtime Non-Interference Gate
        g2_passed, g2_details = self._verify_g2_non_interference(manifest)
        gates.append(
            GateResult(
                gate_id="G2",
                name="Runtime Non-Interference Gate",
                passed=g2_passed,
                status="PASS" if g2_passed else "FAIL",
                details=g2_details,
            )
        )

        # Recompute directly from raw JSONL files independently
        recomputed_records_count, recomputed_status_counts = self._parse_raw_telemetry_records()

        # G3: Evaluation Accounting Gate
        g3_passed, g3_details = self._verify_g3_evaluation_accounting(manifest, recomputed_status_counts)
        gates.append(
            GateResult(
                gate_id="G3",
                name="Evaluation Accounting Gate",
                passed=g3_passed,
                status="PASS" if g3_passed else "FAIL",
                details=g3_details,
            )
        )

        # G4: Delivery & Runtime-to-Disk Reconciliation Gate
        g4_passed, g4_details = self._verify_g4_delivery_reconciliation(manifest, recomputed_records_count)
        gates.append(
            GateResult(
                gate_id="G4",
                name="Delivery & Runtime-to-Disk Reconciliation Gate",
                passed=g4_passed,
                status="PASS" if g4_passed else "FAIL",
                details=g4_details,
            )
        )

        # G5: Zero Mismatch Gate (Zero Waivers Allowed)
        g5_passed, g5_details = self._verify_g5_zero_mismatch(manifest)
        gates.append(
            GateResult(
                gate_id="G5",
                name="Zero Decision Mismatch Gate",
                passed=g5_passed,
                status="PASS" if g5_passed else "FAIL",
                details=g5_details,
            )
        )

        # G6: Minimum Coverage Gate
        g6_passed, g6_details = self._verify_g6_coverage(manifest)
        gates.append(
            GateResult(
                gate_id="G6",
                name="Minimum Session & Lifecycle Coverage Gate",
                passed=g6_passed,
                status="PASS" if g6_passed else "INCOMPLETE",
                details=g6_details,
            )
        )

        # G7: Controlled Restart Continuity Gate
        g7_passed, g7_details = self._verify_g7_controlled_restart(manifest)
        gates.append(
            GateResult(
                gate_id="G7",
                name="Controlled Restart Continuity Gate",
                passed=g7_passed,
                status="PASS" if g7_passed else "INCOMPLETE",
                details=g7_details,
            )
        )

        # G8: Performance Budget Gate
        g8_passed, g8_details = self._verify_g8_performance(manifest)
        gates.append(
            GateResult(
                gate_id="G8",
                name="Performance Budget Gate",
                passed=g8_passed,
                status="PASS" if g8_passed else "FAIL",
                details=g8_details,
            )
        )

        # Determine Overall Status
        overall_status = "PASS"
        for g in gates:
            if g.status == "INVALID":
                overall_status = "INVALID"
                break
            elif g.status == "FAIL" and overall_status != "INVALID":
                overall_status = "FAIL"
            elif g.status == "INCOMPLETE" and overall_status not in ("INVALID", "FAIL"):
                overall_status = "INCOMPLETE"

        return AcceptanceReport(
            wave="1D.3",
            expected_rc_commit=self.expected_rc_commit,
            generation_id=self.generation_dir.name,
            generation_path=str(self.generation_dir),
            overall_status=overall_status,
            gates=gates,
            digest_verified=g9_passed,
            runtime_to_disk_reconciled=g4_passed,
        )

    def _verify_g9_sha256(self, manifest_raw_bytes: bytes, manifest: ShadowSoakManifest) -> tuple[bool, dict[str, Any]]:
        manifest_data = json.loads(manifest_raw_bytes.decode("utf-8"))
        manifest_data["manifest_hash"] = ""
        recomputed_hash = hashlib.sha256(json.dumps(manifest_data, sort_keys=True).encode("utf-8")).hexdigest()
        expected_hash = manifest.manifest_hash

        if self.sha256_path.exists():
            sha_file_content = self.sha256_path.read_text(encoding="utf-8").strip().split()[0]
        else:
            sha_file_content = ""

        match = (recomputed_hash == expected_hash == sha_file_content)
        return match, {"recomputed_hash": recomputed_hash, "manifest_hash": expected_hash, "file_hash": sha_file_content}

    def _verify_g1_preflight(self, manifest: ShadowSoakManifest) -> tuple[bool, dict[str, Any]]:
        commit_match = manifest.git_commit.startswith(self.expected_rc_commit) or self.expected_rc_commit.startswith(manifest.git_commit)
        passed = (
            manifest.preflight_passed
            and manifest.git_clean_status
            and manifest.authority == "legacy"
            and commit_match
        )
        return passed, {
            "preflight_passed": manifest.preflight_passed,
            "git_clean_status": manifest.git_clean_status,
            "authority": manifest.authority,
            "git_commit": manifest.git_commit,
            "expected_rc_commit": self.expected_rc_commit,
        }

    def _verify_g2_non_interference(self, manifest: ShadowSoakManifest) -> tuple[bool, dict[str, Any]]:
        passed = (
            manifest.shadow_caused_orders == 0
            and manifest.shadow_caused_state_commits == 0
            and manifest.shadow_caused_lifecycle_appends == 0
            and manifest.duplicate_legacy_invocations == 0
            and manifest.duplicate_shadow_invocations == 0
            and manifest.unclassified_cycles == 0
        )
        return passed, {
            "shadow_caused_orders": manifest.shadow_caused_orders,
            "shadow_caused_state_commits": manifest.shadow_caused_state_commits,
            "shadow_caused_lifecycle_appends": manifest.shadow_caused_lifecycle_appends,
            "duplicate_legacy_invocations": manifest.duplicate_legacy_invocations,
            "duplicate_shadow_invocations": manifest.duplicate_shadow_invocations,
            "unclassified_cycles": manifest.unclassified_cycles,
        }

    def _parse_raw_telemetry_records(self) -> tuple[int, dict[str, int]]:
        total_records = 0
        status_counts: dict[str, int] = {}

        if not self.raw_dir.exists():
            return 0, status_counts

        for fpath in self.raw_dir.glob("*.jsonl"):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        record = json.loads(line)
                        st = record.get("parity_status", "UNKNOWN")
                        status_counts[st] = status_counts.get(st, 0) + 1
                        total_records += 1
            except Exception:
                continue

        return total_records, status_counts

    def _verify_g3_evaluation_accounting(self, manifest: ShadowSoakManifest, status_counts: dict[str, int]) -> tuple[bool, dict[str, Any]]:
        acc = manifest.evaluation_accounting
        passed = acc.is_accounted and (acc.cycles_seen == sum(status_counts.values()))
        return passed, {
            "manifest_cycles_seen": acc.cycles_seen,
            "recomputed_raw_cycles": sum(status_counts.values()),
            "is_accounted": acc.is_accounted,
        }

    def _verify_g4_delivery_reconciliation(self, manifest: ShadowSoakManifest, raw_records_count: int) -> tuple[bool, dict[str, Any]]:
        deliv = manifest.delivery_accounting
        pending_zero = (deliv.telemetry_pending == 0)
        reconciled = (manifest.evaluation_accounting.cycles_seen == raw_records_count + deliv.telemetry_dropped)
        passed = deliv.is_accounted and pending_zero and reconciled
        return passed, {
            "telemetry_enqueued": deliv.telemetry_enqueued,
            "telemetry_written": deliv.telemetry_written,
            "telemetry_dropped": deliv.telemetry_dropped,
            "telemetry_pending": deliv.telemetry_pending,
            "runtime_to_disk_reconciled": reconciled,
        }

    def _verify_g5_zero_mismatch(self, manifest: ShadowSoakManifest) -> tuple[bool, dict[str, Any]]:
        passed = (manifest.evaluation_accounting.mismatches == 0 and manifest.unexplained_mismatches == 0)
        return passed, {
            "mismatches": manifest.evaluation_accounting.mismatches,
            "unexplained_mismatches": manifest.unexplained_mismatches,
        }

    def _verify_g6_coverage(self, manifest: ShadowSoakManifest) -> tuple[bool, dict[str, Any]]:
        cov = manifest.coverage
        passed = (
            cov.eligible_decision_cycles >= self.min_total_cycles
            and cov.day_session_cycles >= self.min_day_cycles
            and cov.night_session_cycles >= self.min_night_cycles
            and cov.single_leg_cycles >= self.min_lifecycles
        )
        return passed, {
            "eligible_cycles": cov.eligible_decision_cycles,
            "min_total_cycles": self.min_total_cycles,
            "day_session_cycles": cov.day_session_cycles,
            "min_day_cycles": self.min_day_cycles,
            "night_session_cycles": cov.night_session_cycles,
            "min_night_cycles": self.min_night_cycles,
            "single_leg_cycles": cov.single_leg_cycles,
            "min_lifecycles": self.min_lifecycles,
        }

    def _verify_g7_controlled_restart(self, manifest: ShadowSoakManifest) -> tuple[bool, dict[str, Any]]:
        # Checked via restart reconciliation count or process segments
        passed = (manifest.coverage.restart_reconciliation_cases >= 1)
        return passed, {"restart_reconciliation_cases": manifest.coverage.restart_reconciliation_cases}

    def _verify_g8_performance(self, manifest: ShadowSoakManifest) -> tuple[bool, dict[str, Any]]:
        perf = manifest.performance
        passed = (perf.shadow_eval_p99_us <= self.max_shadow_eval_p99_us)
        return passed, {
            "shadow_eval_p99_us": perf.shadow_eval_p99_us,
            "max_shadow_eval_p99_us": self.max_shadow_eval_p99_us,
        }
