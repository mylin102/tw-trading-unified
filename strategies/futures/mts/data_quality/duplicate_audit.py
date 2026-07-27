# 2026-07-27 Gemini CLI: Duplicate Event Audit (Exact-row & Semantic)
import json
from typing import Any, Dict, List, Tuple


class DuplicateAudit:
    """
    Evaluates:
    - Exact-row duplicates: Raw canonical serialization is identical.
    - Semantic duplicates: Keyed on (generation_id, contract_code, event_type, event_time, price, volume, bid_price, ask_price, bid_volume, ask_volume).
      Excludes transient fields (received_at, processed_at, sequence_no, file_offset).
    """

    def evaluate_duplicates(self, rows: List[Dict[str, Any]]) -> Tuple[int, int]:
        seen_exact = set()
        exact_dups = 0

        seen_semantic = set()
        semantic_dups = 0

        for row in rows:
            # Exact row check
            raw_str = json.dumps(row, sort_keys=True)
            if raw_str in seen_exact:
                exact_dups += 1
            else:
                seen_exact.add(raw_str)

            # Semantic key check
            sem_key = (
                row.get("generation_id"),
                row.get("contract_code"),
                row.get("event_type"),
                row.get("event_time"),
                row.get("price"),
                row.get("volume"),
                row.get("bid_price"),
                row.get("ask_price"),
                row.get("bid_volume"),
                row.get("ask_volume"),
            )
            if sem_key in seen_semantic:
                semantic_dups += 1
            else:
                seen_semantic.add(sem_key)

        return exact_dups, semantic_dups
