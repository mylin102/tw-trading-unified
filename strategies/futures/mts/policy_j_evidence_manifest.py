# 2026-07-26 Gemini CLI: Wave J2-B Evidence Coverage Manifest Generator
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class PolicyJEvidenceManifest:
    """
    Audit Manifest summarizing Dataset B evidence coverage and reproducibility.
    """
    adr_version: str = "ADR-016"
    schema_version: str = "1.1"
    builder_version: str = "2.0"
    fill_model: str = "EXECUTABLE"
    source_trade_count: int = 0
    joined_trade_count: int = 0
    eligible_trade_count: int = 0
    triggered_trade_count: int = 0
    untriggered_trade_count: int = 0
    excluded_trade_count: int = 0
    exclusion_reason_distribution: Dict[str, int] = None
    reproduction_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def compute_reproduction_hash(
    adr_version: str,
    schema_version: str,
    builder_version: str,
    fill_model: str,
    trade_facts_serialized: str,
) -> str:
    """Compute deterministic SHA-256 reproduction hash across evidence facts."""
    hasher = hashlib.sha256()
    hasher.update(adr_version.encode("utf-8"))
    hasher.update(schema_version.encode("utf-8"))
    hasher.update(builder_version.encode("utf-8"))
    hasher.update(fill_model.encode("utf-8"))
    hasher.update(trade_facts_serialized.encode("utf-8"))
    return hasher.hexdigest()
