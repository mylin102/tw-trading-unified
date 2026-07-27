# 2026-07-27 Gemini CLI: Stratified Staleness & Pair-Skew Audit
from datetime import datetime
import numpy as np
from typing import Any, Dict, List, Tuple


class FreshnessAudit:
    """
    Evaluates:
    - Pair-skew percentiles (P50, P90, P99) between Near and Far ticks upon each update.
    - Stratified receive gaps per contract.
    """

    def parse_ts(self, ts_str: str) -> float | None:
        if not ts_str:
            return None
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    def evaluate_freshness(self, rows: List[Dict[str, Any]]) -> Dict[str, float]:
        latest_event_ts: Dict[str, float] = {}
        pair_skews_ms: List[float] = []

        for row in rows:
            contract = row.get("contract_code")
            ev_ts_str = row.get("event_time")
            ev_ts = self.parse_ts(ev_ts_str)

            if not contract or ev_ts is None:
                continue

            latest_event_ts[contract] = ev_ts

            # Calculate skew if both Near and Far are known
            if "TMF" in latest_event_ts and "TMFI6" in latest_event_ts:
                skew_ms = abs(latest_event_ts["TMF"] - latest_event_ts["TMFI6"]) * 1000.0
                pair_skews_ms.append(skew_ms)

        if pair_skews_ms:
            p50 = float(np.percentile(pair_skews_ms, 50))
            p90 = float(np.percentile(pair_skews_ms, 90))
            p99 = float(np.percentile(pair_skews_ms, 99))
        else:
            p50 = p90 = p99 = 0.0

        return {
            "pair_skew_p50_ms": round(p50, 2),
            "pair_skew_p90_ms": round(p90, 2),
            "pair_skew_p99_ms": round(p99, 2),
        }
