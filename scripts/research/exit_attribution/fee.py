"""Versioned/effective-dated fee schedule selection (design §2/§4, T21)."""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional


def select_fee_schedule(schedules: List[dict], as_of) -> Optional[dict]:
    """Pick the latest schedule with effective_date <= as_of.

    schedules: [{"effective_date": "YYYY-MM-DD", "per_contract": float, ...}]
    Returns None when as_of precedes the earliest effective date (the
    schedule is then UNRECONCILED/FEE_UNCERTAIN — never an invented one).
    """
    if not schedules:
        return None
    if isinstance(as_of, str):
        as_of = datetime.strptime(as_of[:10], "%Y-%m-%d").date()
    if isinstance(as_of, datetime):
        as_of = as_of.date()
    eligible = []
    for s in schedules:
        eff = datetime.strptime(str(s["effective_date"])[:10], "%Y-%m-%d").date()
        if eff <= as_of:
            eligible.append((eff, s))
    if not eligible:
        return None
    eligible.sort(key=lambda x: x[0])
    return eligible[-1][1]
