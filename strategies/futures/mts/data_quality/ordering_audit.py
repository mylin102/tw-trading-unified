# 2026-07-27 Gemini CLI: Stream-Partitioned Monotonicity & Ordering Audit
from datetime import datetime
from typing import Any, Dict, List, Tuple


class OrderingAudit:
    """
    Evaluates event-time monotonicity partitioned by contract_code.
    Categorizes regressions into:
    - MICRO_REORDER (<= 100ms)
    - NETWORK_REORDER (100ms - 1000ms)
    - SEVERE_REGRESSION (> 1000ms)
    """

    def parse_ts(self, ts_str: str) -> float | None:
        if not ts_str:
            return None
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    def evaluate_ordering(self, rows: List[Dict[str, Any]]) -> Tuple[Dict[str, float], Dict[str, int]]:
        contract_last_ts: Dict[str, float] = {}
        contract_total: Dict[str, int] = {}
        contract_monotonic: Dict[str, int] = {}

        global_last_event_ts: float | None = None
        global_total = 0
        global_regressions = 0

        micro_reorder = 0
        network_reorder = 0
        severe_regression = 0

        for row in rows:
            contract = row.get("contract_code", "UNKNOWN")
            ev_ts_str = row.get("event_time")
            ev_ts = self.parse_ts(ev_ts_str)

            if ev_ts is None:
                continue

            # Per-contract monotonicity
            contract_total[contract] = contract_total.get(contract, 0) + 1
            if contract in contract_last_ts:
                prev_ts = contract_last_ts[contract]
                if ev_ts >= prev_ts:
                    contract_monotonic[contract] = contract_monotonic.get(contract, 0) + 1
                else:
                    reg_ms = (prev_ts - ev_ts) * 1000.0
                    if reg_ms <= 100.0:
                        micro_reorder += 1
                    elif reg_ms <= 1000.0:
                        network_reorder += 1
                    else:
                        severe_regression += 1
            else:
                contract_monotonic[contract] = 1
            contract_last_ts[contract] = ev_ts

            # Global arrival order monotonicity (diagnostic)
            global_total += 1
            if global_last_event_ts is not None and ev_ts < global_last_event_ts:
                global_regressions += 1
            global_last_event_ts = ev_ts

        # Calculate Rates
        rates: Dict[str, float] = {}
        for c in ["TMF", "TMFI6"]:
            tot = contract_total.get(c, 0)
            mon = contract_monotonic.get(c, 0)
            rates[f"{c.lower()}_tick_monotonic_rate"] = (mon / tot) if tot > 0 else 1.0

        rates["global_event_time_regression_rate"] = (global_regressions / global_total) if global_total > 0 else 0.0

        counts = {
            "micro_reorder_count": micro_reorder,
            "network_reorder_count": network_reorder,
            "severe_regression_count": severe_regression,
        }

        return rates, counts
