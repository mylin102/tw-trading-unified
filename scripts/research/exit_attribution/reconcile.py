"""Per-fill realized PnL reconciliation (design §2).

Single mutually-exclusive status axis + additive issue flags.
Deterministic severity aggregation (design: CORRUPT > UNRECONCILED >
MISMATCH > FEE_UNCERTAIN > OK).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# Single status axis (mutually exclusive)
STATUS_OK = "OK"
STATUS_MISMATCH = "MISMATCH"
STATUS_FEE_UNCERTAIN = "FEE_UNCERTAIN"
STATUS_UNRECONCILED = "UNRECONCILED"
STATUS_CORRUPT = "CORRUPT"

_SEVERITY = {
    STATUS_OK: 0,
    STATUS_FEE_UNCERTAIN: 1,
    STATUS_MISMATCH: 2,
    STATUS_UNRECONCILED: 3,
    STATUS_CORRUPT: 4,
}

# Known corrupt ledger trades (see design §2; source ledger retained).
CORRUPT_TRADES = frozenset({"mts-auto-222204-082"})

RECONCILE_TOLERANCE_TWD = 1.0


@dataclass
class ReconcileResult:
    fill: dict
    status: str = STATUS_OK
    expected_realized: Optional[float] = None
    stored_realized: Optional[float] = None
    delta: Optional[float] = None
    issue_flags: List[str] = field(default_factory=list)


def position_side_sign(side: str) -> Optional[int]:
    """LONG=+1 / SHORT=-1 (equivalently close SELL=+1 / close BUY=-1).

    Closing a LONG is a SELL (+1); closing a SHORT is a BUY (-1).
    Missing/invalid side -> None (NEVER defaults to LONG).
    """
    s = (side or "").strip().upper()
    if s in ("LONG", "SELL"):
        return +1
    if s in ("SHORT", "BUY"):
        return -1
    return None


def expected_close_realized(
    close_px: float,
    entry_avg_px: float,
    qty: float,
    contract_multiplier: float,
    side: str,
    fees: float = 0.0,
) -> Optional[float]:
    """Recomputed realized PnL for one closing fill (design §2).

    expected = (close_px - entry_avg) * qty * multiplier * side_sign - fees
    Returns None when the position side is missing/invalid.
    """
    sign = position_side_sign(side)
    if sign is None:
        return None
    return (close_px - entry_avg_px) * qty * contract_multiplier * sign - fees


def aggregate_status(results: List[ReconcileResult]) -> tuple:
    """Deterministic worst-status aggregation; ALL flags preserved.

    Returns (status, flags) — status by fixed severity order (not loop
    order); flags collected from every result, deduped, order preserved.
    """
    if not results:
        return STATUS_OK, []
    worst = max((r.status for r in results), key=lambda s: _SEVERITY.get(s, 0))
    flags = []
    for r in results:
        for f in r.issue_flags:
            if f not in flags:
                flags.append(f)
    return worst, flags


def _fill_side(fill: dict) -> Optional[str]:
    return str(fill.get("side") or fill.get("position_side") or "").strip() or None


def _fill_fees(fill: dict, fee_schedule: Optional[dict] = None) -> Optional[float]:
    """Fee for a fill: explicit fill fees, else schedule-based, else None."""
    if "fees" in fill and fill.get("fees") is not None:
        return float(fill.get("fees"))
    if fee_schedule:
        per = float(fee_schedule.get("per_contract") or 0.0)
        return float(fill.get("qty") or 0) * per
    return None


def reconcile_fill(
    fill: dict,
    entry_avg_px: Optional[float],
    contract_multiplier: float,
    fees: Optional[float] = None,
    fee_schedule: Optional[dict] = None,
    stored_realized: Optional[float] = None,
    expected_closed_qty: Optional[float] = None,
) -> ReconcileResult:
    """Reconcile one closing fill; returns status + flags (design §2).

    Never defaults an invalid side to LONG; never treats a missing entry
    average as 0.0 (would fabricate PnL).
    """
    res = ReconcileResult(fill=fill)
    trade_id = str(fill.get("trade_id") or "")
    if trade_id in CORRUPT_TRADES:
        res.status = STATUS_CORRUPT
        res.issue_flags.append("corrupt_realized_pnl")
        return res

    if entry_avg_px is None:
        res.status = STATUS_UNRECONCILED
        res.issue_flags.append("missing_entry_avg")
        return res

    side = _fill_side(fill)
    if side is None or position_side_sign(side) is None:
        res.status = STATUS_UNRECONCILED
        res.issue_flags.append("missing_position_side")
        return res

    qty = fill.get("qty")
    price = fill.get("price")
    if qty is None or price is None:
        res.status = STATUS_UNRECONCILED
        res.issue_flags.append("missing_qty_or_price")
        return res

    if expected_closed_qty is not None and abs(float(qty) - float(expected_closed_qty)) > 1e-9:
        res.status = STATUS_MISMATCH
        res.issue_flags.append("leg_qty_mismatch")
        return res

    fee = float(fees) if fees is not None else _fill_fees(fill, fee_schedule)
    if fee is None:
        res.status = STATUS_FEE_UNCERTAIN
        res.issue_flags.append("fee_missing")
        res.expected_realized = expected_close_realized(
            float(price), entry_avg_px, float(qty), contract_multiplier, side, 0.0
        )
        res.stored_realized = stored_realized
        return res

    expected = expected_close_realized(
        float(price), entry_avg_px, float(qty), contract_multiplier, side, fee
    )
    res.expected_realized = expected
    res.stored_realized = stored_realized
    if stored_realized is not None and abs(expected - stored_realized) > RECONCILE_TOLERANCE_TWD:
        res.status = STATUS_MISMATCH
        res.delta = expected - stored_realized
        res.issue_flags.append("stored_realized_mismatch")  # survives severity aggregation
    return res
