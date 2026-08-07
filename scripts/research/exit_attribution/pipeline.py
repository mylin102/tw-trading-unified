"""Pipeline orchestrator (design §4/§9).

build_rows(): fills+ticks -> per-release rows with the design §4 schema.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .reconcile import (
    STATUS_OK,
    ReconcileResult,
    reconcile_fill,
)
from .quoting import (
    TIER_BOUNDED_TICK_PROXY,
    TIER_EXECUTABLE_BBO,
    TIER_UNUSABLE,
    select_quote,
    tier_for_legs,
    valuation_price,
)
from .classify import DQ_OK, DQ_PROXY, DQ_UNRECONCILED, DQ_UNUSABLE, classify_row

SCHEMA_VERSION = "exit_attribution.v1"


def _entry_avg(fills: List[dict]) -> Optional[float]:
    vals = [(float(f.get("qty") or 0), float(f.get("price") or 0)) for f in fills]
    qty = sum(q for q, _ in vals)
    if qty <= 0:
        return None
    return sum(q * p for q, p in vals) / qty


def build_trade_row(
    trade: dict,
    ticks_by_code: Dict[str, List[dict]],
    contracts: Dict[str, float],
    fee_source_available: bool = True,
    age_bound_s: float = 5.0,
) -> dict:
    """One output row per release event (design §4 schema)."""
    trade_id = str(trade.get("trade_id") or "")
    entries = trade.get("entries", [])
    releases = trade.get("releases", [])
    exits = trade.get("exits", [])
    if not releases:
        return None
    rel = releases[0]
    near_code = trade.get("near_code") or "TMFN"
    far_code = trade.get("far_code") or "TMF"
    released_leg = str(rel.get("leg") or "").upper()
    other_leg = "FAR" if released_leg == "NEAR" else "NEAR"
    mult = contracts.get(released_leg, contracts.get("DEFAULT", 10.0))

    entry_avg_rel = _entry_avg([f for f in entries if str(f.get("leg") or "").upper() == released_leg])
    entry_avg_other = _entry_avg([f for f in entries if str(f.get("leg") or "").upper() == other_leg])

    release_ts = rel.get("ts") or rel.get("timestamp")

    # reconcile the release fill + remaining exit
    rel_res = reconcile_fill(rel, entry_avg_rel or 0.0, mult, fee_source_available,
                             stored_realized=rel.get("realized_pnl"))
    remaining_exit = next((f for f in exits if str(f.get("leg") or "").upper() == other_leg), None)
    other_res = None
    if remaining_exit is not None:
        other_res = reconcile_fill(remaining_exit, entry_avg_other or 0.0, mult, fee_source_available,
                                   stored_realized=remaining_exit.get("realized_pnl"))

    # quotes at release_ts (strictly <= release_ts)
    rel_code = near_code if released_leg == "NEAR" else far_code
    other_code = far_code if released_leg == "NEAR" else near_code
    rel_tick, rel_age = select_quote(ticks_by_code.get(rel_code, []), release_ts, age_bound_s)
    other_tick, other_age = select_quote(ticks_by_code.get(other_code, []), release_ts, age_bound_s)
    tier, tier_detail = tier_for_legs(
        {released_leg: (rel_tick, rel_age), other_leg: (other_tick, other_age)}, age_bound_s
    )

    # per-leg position sign from the leg's ENTRY side (LONG=+1/SHORT=-1)
    from .reconcile import position_side_sign
    rel_sign = position_side_sign(
        str(([f for f in entries if str(f.get("leg") or "").upper() == released_leg] or [{}])[0].get("side") or "")
    ) or 1
    other_sign = position_side_sign(
        str(([f for f in entries if str(f.get("leg") or "").upper() == other_leg] or [{}])[0].get("side") or "")
    ) or 1

    # pre_release_paired_pnl (gross): released leg ACTUAL fill + other leg marked
    rel_px = float(rel.get("price") or 0)
    rel_actual = 0.0
    if entry_avg_rel is not None and rel_px:
        rel_actual = (rel_px - entry_avg_rel) * float(rel.get("qty") or 1) * mult * rel_sign
    other_mark = None
    if other_tick is not None:
        px, _src = valuation_price(
            other_tick,
            "BUY" if other_sign < 0 else "SELL",
        )
        other_mark = px
    other_actual_mark = 0.0
    if entry_avg_other is not None and other_mark:
        other_actual_mark = (other_mark - entry_avg_other) * float(rel.get("qty") or 1) * mult * other_sign
    pre_release_paired_pnl = round(rel_actual + other_actual_mark, 2)

    # release_time_combined_valuation_gross: both legs via quotes
    val = None
    if tier != TIER_UNUSABLE and rel_tick is not None and other_tick is not None:
        r_px, _ = valuation_price(rel_tick, "BUY" if rel_sign < 0 else "SELL")
        o_px, _ = valuation_price(other_tick, "BUY" if other_sign < 0 else "SELL")
        if r_px and o_px:
            rv = (r_px - (entry_avg_rel or 0.0)) * float(rel.get("qty") or 1) * mult * rel_sign
            ov = (o_px - (entry_avg_other or 0.0)) * float(rel.get("qty") or 1) * mult * other_sign
            val = round(rv + ov, 2)
    immediate_exec = round(val, 2) if (tier == TIER_EXECUTABLE_BBO and val is not None) else None

    # actual_full (gross + net)
    actual_gross = 0.0
    actual_net = 0.0
    fee_uncertain = False
    for _r in (rel_res, other_res):
        if _r is None:
            continue
        if _r.status not in (STATUS_OK,) and _r.status not in ("CORRUPT",):
            pass
        if _r.expected_realized is not None:
            actual_gross += _r.expected_realized + float(_r.fill.get("fees") or 0.0)
            actual_net += _r.expected_realized
        if _r.status == "FEE_UNCERTAIN":
            fee_uncertain = True
    actual_gross = round(actual_gross, 2)
    actual_net = round(actual_net, 2) if not fee_uncertain else None

    post_inc = round(actual_gross - pre_release_paired_pnl, 2)

    unhedged_s = None
    if remaining_exit is not None:
        t1 = release_ts
        t2 = remaining_exit.get("ts") or remaining_exit.get("timestamp")
        if t1 and t2:
            unhedged_s = round((t2 - t1).total_seconds(), 2)

    status = "OK"
    flags = []
    for _r in (rel_res, other_res):
        if _r is None:
            continue
        if _r.status != STATUS_OK:
            status = _r.status
            flags.extend(_r.issue_flags)
    dq = DQ_OK
    if status != "OK":
        dq = DQ_UNRECONCILED
    elif tier == TIER_BOUNDED_TICK_PROXY:
        dq = DQ_PROXY
    elif tier == TIER_UNUSABLE:
        dq = DQ_UNUSABLE

    row = {
        "trade_id": trade_id,
        "release_ts": release_ts.isoformat() if hasattr(release_ts, "isoformat") else str(release_ts),
        "released_leg": released_leg,
        "pre_release_paired_pnl": pre_release_paired_pnl,
        "release_time_combined_valuation_gross": val,
        "valuation_tier": tier,
        "immediate_executable_combined_pnl_gross": immediate_exec,
        "actual_full_pnl_gross": actual_gross,
        "actual_full_pnl_net": actual_net,
        "post_release_incremental_pnl": post_inc,
        "unhedged_seconds": unhedged_s,
        "status": status,
        "issue_flags": flags,
        "data_quality": dq,
        "quote_age_s": {k: round(v, 3) if v is not None else None for k, v in
                        {released_leg: rel_age, other_leg: other_age}.items()},
        "schema_version": SCHEMA_VERSION,
    }
    row.update(classify_row(row))
    return row


def build_rows(trades, ticks_by_code, contracts, fee_source_available=True, age_bound_s=5.0):
    rows = []
    for t in trades:
        r = build_trade_row(t, ticks_by_code, contracts, fee_source_available, age_bound_s)
        if r is not None:
            rows.append(r)
    return rows
