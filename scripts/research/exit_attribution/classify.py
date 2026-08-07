"""Attribution axes (design §5) — three independent axes, no forced one-of.

data_quality:       OK | PROXY | UNUSABLE | UNRECONCILED
entry_attribution:  BAD | NOT_BAD | UNKNOWN
release_attribution: HARMFUL | HELPFUL | NEUTRAL | UNKNOWN
"""
from __future__ import annotations

DQ_OK = "OK"
DQ_PROXY = "PROXY"
DQ_UNUSABLE = "UNUSABLE"
DQ_UNRECONCILED = "UNRECONCILED"

ENTRY_BAD = "BAD"
ENTRY_NOT_BAD = "NOT_BAD"
ENTRY_UNKNOWN = "UNKNOWN"

REL_HARMFUL = "HARMFUL"
REL_HELPFUL = "HELPFUL"
REL_NEUTRAL = "NEUTRAL"
REL_UNKNOWN = "UNKNOWN"


def classify_row(row: dict) -> dict:
    """Three independent axes from one output row (design §5)."""
    dq = str(row.get("data_quality") or DQ_UNRECONCILED)
    if dq != DQ_OK:
        return {
            "data_quality": dq,
            "entry_attribution": ENTRY_UNKNOWN,
            "release_attribution": REL_UNKNOWN,
        }
    pre = float(row.get("pre_release_paired_pnl") or 0.0)
    inc = float(row.get("post_release_incremental_pnl") or 0.0)
    entry = ENTRY_BAD if pre < 0 else ENTRY_NOT_BAD
    release = REL_HARMFUL if inc < 0 else (REL_HELPFUL if inc > 0 else REL_NEUTRAL)
    return {
        "data_quality": dq,
        "entry_attribution": entry,
        "release_attribution": release,
    }
