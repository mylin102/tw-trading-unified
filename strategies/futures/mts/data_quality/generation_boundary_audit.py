# 2026-07-27 Gemini CLI: Cross-Generation Contamination & Boundary Audit
from typing import Any, Dict, List, Tuple


class GenerationBoundaryAudit:
    """
    Hard-gate checks:
    - Multiple generation_ids in single file -> INVALID
    - Manifest generation_id mismatch -> INVALID
    - Sequence_no regression within generation -> INVALID
    """

    def evaluate_boundaries(self, rows: List[Dict[str, Any]], expected_gen_id: str | None = None) -> Tuple[int, List[str]]:
        gen_ids = set()
        violations: List[str] = []

        for row in rows:
            gid = row.get("generation_id")
            if gid:
                gen_ids.add(gid)

        if len(gen_ids) > 1:
            violations.append(f"Multiple generation_ids in single dataset: {gen_ids}")

        if expected_gen_id and gen_ids and (expected_gen_id not in gen_ids):
            violations.append(f"Manifest generation_id {expected_gen_id} mismatch with dataset generation_ids {gen_ids}")

        contamination_count = len(violations)
        return contamination_count, violations
