"""Pipeline orchestrator (design §4/§9).

build_rows(): fills+ticks -> per-release rows with the design §4 schema.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .reconcile import (
    STATUS_OK,
    STATUS_UNRECONCILED,
    ReconcileResult,
    aggregate_status,
    position_side_sign,
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


def _leg_qty(fills: List[dict]) -> float:
    return sum(float(f.get("qty") or 0) for f in fills)


def _leg_sign(fills: List[dict]) -> Optional[int]:
    """Position side sign from ENTRY fills; None if missing/invalid (never LONG)."""
    sides = [str(f.get("side") or "") for f in fills]
    signs = [position_side_sign(s) for s in sides]
    valid = [s for s in signs if s is not None]
    if not valid:
        return None
    return valid[0]


def _fill_fee(fill: dict, fee_schedule: Optional[dict]) -> Optional[float]:
    if "fees" in fill and fill.get("fees") is not None:
        return float(fill.get("fees"))
    if fee_schedule:
        return float(fill.get("qty") or 0) * float(fee_schedule.get("per_contract") or 0.0)
    return None


def build_trade_row(
    trade: dict,
    ticks_by_code: Dict[str, List[dict]],
    contracts: Dict[str, float],
    fee_schedule: Optional[dict] = None,
    age_bound_s: float = 5.0,
) -> Optional[dict]:
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
    rel_code = near_code if released_leg == "NEAR" else far_code
    other_code = far_code if released_leg == "NEAR" else near_code

    # per-leg contract multiplier (codex blocker #1)
    rel_mult = float(contracts.get(rel_code, contracts.get("DEFAULT", 10.0)))
    other_mult = float(contracts.get(other_code, contracts.get("DEFAULT", 10.0)))

    rel_entries = [f for f in entries if str(f.get("leg") or "").upper() == released_leg]
    other_entries = [f for f in entries if str(f.get("leg") or "").upper() == other_leg]
    entry_avg_rel = _entry_avg(rel_entries)
    entry_avg_other = _entry_avg(other_entries)
    rel_qty = _leg_qty(rel_entries)
    other_qty = _leg_qty(other_entries)

    # per-leg position sign; None -> UNRECONCILED (never default LONG, blocker #3)
    rel_sign = _leg_sign(rel_entries)
    other_sign = _leg_sign(other_entries)

    release_ts = rel.get("ts") or rel.get("timestamp")

    # reconcile release fill + remaining exit (fee wiring, blocker #4)
    rel_res = reconcile_fill(
        rel, entry_avg_rel, rel_mult,
        fees=_fill_fee(rel, fee_schedule),
        stored_realized=rel.get("realized_pnl"),
        expected_closed_qty=rel_qty,
    )
    remaining_exit = next((f for f in exits if str(f.get("leg") or "").upper() == other_leg), None)
    other_res = None
    if remaining_exit is not None:
        other_res = reconcile_fill(
            remaining_exit, entry_avg_other, other_mult,
            fees=_fill_fee(remaining_exit, fee_schedule),
            stored_realized=remaining_exit.get("realized_pnl"),
            expected_closed_qty=other_qty,
        )

    # quotes at release_ts (strictly <= release_ts)
    rel_tick, rel_age = select_quote(ticks_by_code.get(rel_code, []), release_ts, age_bound_s)
    other_tick, other_age = select_quote(ticks_by_code.get(other_code, []), release_ts, age_bound_s)
    tier, tier_detail = tier_for_legs(
        {released_leg: (rel_tick, rel_age), other_leg: (other_tick, other_age)}, age_bound_s
    )

    # pre_release_paired_pnl (gross): released leg ACTUAL fill + other leg marked
    pre_release_paired_pnl = None
    rel_actual = None
    if rel_sign is not None and entry_avg_rel is not None and rel_qty:
        rel_actual = (float(rel.get("price") or 0) - entry_avg_rel) * rel_qty * rel_mult * rel_sign
        other_mark = None
        if other_tick is not None and other_sign is not None:
            px, _src = valuation_price(other_tick, "BUY" if other_sign < 0 else "SELL")
            other_mark = px
        if other_sign is not None and entry_avg_other is not None and other_mark:
            other_actual_mark = (other_mark - entry_avg_other) * other_qty * other_mult * other_sign
            pre_release_paired_pnl = round(rel_actual + other_actual_mark, 2)

    # release_time_combined_valuation_gross: both legs via quotes
    val = None
    if (tier != TIER_UNUSABLE and rel_tick is not None and other_tick is not None
            and rel_sign is not None and other_sign is not None
            and entry_avg_rel is not None and entry_avg_other is not None):
        r_px, _ = valuation_price(rel_tick, "BUY" if rel_sign < 0 else "SELL")
        o_px, _ = valuation_price(other_tick, "BUY" if other_sign < 0 else "SELL")
        if r_px and o_px:
            rv = (r_px - entry_avg_rel) * rel_qty * rel_mult * rel_sign
            ov = (o_px - entry_avg_other) * other_qty * other_mult * other_sign
            val = round(rv + ov, 2)
    immediate_exec = round(val, 2) if (tier == TIER_EXECUTABLE_BBO and val is not None) else None

    # actual_full (gross + net): only when BOTH legs reconcile to a value
    actual_gross = 0.0
    actual_net = 0.0
    fee_uncertain = False
    _res_list = [r for r in (rel_res, other_res) if r is not None]
    has_expected = bool(_res_list) and all(
        r.expected_realized is not None for r in _res_list
    )
    for _r in _res_list:
        if _r.expected_realized is None:
            continue
        fee = _fill_fee(_r.fill, fee_schedule)
        actual_gross += _r.expected_realized + (fee or 0.0)
        actual_net += _r.expected_realized
        if _r.status == "FEE_UNCERTAIN":
            fee_uncertain = True
    actual_gross = round(actual_gross, 2) if has_expected else None
    actual_net = round(actual_net, 2) if (has_expected and not fee_uncertain) else None

    post_inc = None
    if actual_gross is not None and pre_release_paired_pnl is not None:
        post_inc = round(actual_gross - pre_release_paired_pnl, 2)

    unhedged_s = None
    if remaining_exit is not None:
        t1 = release_ts
        t2 = remaining_exit.get("ts") or remaining_exit.get("timestamp")
        if t1 and t2:
            unhedged_s = round((t2 - t1).total_seconds(), 2)

    # deterministic status aggregation (blocker #6): severity order, all flags
    _res_list = [r for r in (rel_res, other_res) if r is not None]
    # missing/invalid ENTRY side must never default to LONG (blocker #3):
    # synthesize an UNRECONCILED result so it participates in aggregation.
    _entry_sign_issues = []
    if rel_sign is None:
        _entry_sign_issues.append("missing_position_side")
    if remaining_exit is not None and other_sign is None:
        _entry_sign_issues.append("missing_position_side")
    if _entry_sign_issues:
        _res_list.append(ReconcileResult(
            fill={"trade_id": trade_id},
            status=STATUS_UNRECONCILED,
            issue_flags=list(dict.fromkeys(_entry_sign_issues)),
        ))
    status, flags = aggregate_status(_res_list)
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


def build_rows(trades, ticks_by_code, contracts, fee_schedule=None, age_bound_s=5.0):
    rows = []
    for t in trades:
        r = build_trade_row(t, ticks_by_code, contracts, fee_schedule, age_bound_s)
        if r is not None:
            rows.append(r)
    return rows
