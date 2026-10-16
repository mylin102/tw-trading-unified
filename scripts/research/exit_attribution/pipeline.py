"""Pipeline orchestrator (design §4/§9).

build_rows(): fills+ticks -> per-release rows with the design §4 schema.

Multi-fill support: ALL release fills and ALL sibling-exit fills are
aggregated per leg. actual_full_pnl is only produced when every expected
leg is FULLY closed (open qty == closed qty) and entries are coherent
(no conflicting sides, no zero/missing entry prices). Otherwise the row is
UNRECONCILED and actual_full/post_release_incremental are None — a trade
is never fabricated as settled.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

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


def _agg_fills(fills: List[dict]) -> Tuple[float, Optional[float], set, bool]:
    """(total_qty, weighted_avg_px, sides, has_zero_or_missing_price).

    Fills with qty<=0 are ignored for quantity. Fills with missing/zero
    price are flagged (never averaged in as zero).
    """
    qty = 0.0
    px_acc = 0.0
    sides = set()
    bad_price = False
    for f in fills:
        q = float(f.get("qty") or 0)
        p = f.get("price")
        sides.add(str(f.get("side") or "").strip().upper())
        if q <= 0:
            continue
        if p is None or float(p) <= 0:
            bad_price = True
            continue
        qty += q
        px_acc += q * float(p)
    avg = (px_acc / qty) if qty > 0 else None
    return qty, avg, sides, bad_price


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
    near_code = trade.get("near_code") or "TMFN"
    far_code = trade.get("far_code") or "TMF"
    released_leg = str(releases[0].get("leg") or "").upper()
    other_leg = "FAR" if released_leg == "NEAR" else "NEAR"
    rel_code = near_code if released_leg == "NEAR" else far_code
    other_code = far_code if released_leg == "NEAR" else near_code

    # per-leg contract multiplier
    rel_mult = float(contracts.get(rel_code, contracts.get("DEFAULT", 10.0)))
    other_mult = float(contracts.get(other_code, contracts.get("DEFAULT", 10.0)))

    rel_entries = [f for f in entries if str(f.get("leg") or "").upper() == released_leg]
    other_entries = [f for f in entries if str(f.get("leg") or "").upper() == other_leg]
    rel_open_qty, entry_avg_rel, rel_sides, rel_bad_px = _agg_fills(rel_entries)
    other_open_qty, entry_avg_other, other_sides, other_bad_px = _agg_fills(other_entries)

    # per-leg position sign; conflicting sides -> None (never default LONG)
    rel_sign = None
    if len(rel_sides) == 1 and position_side_sign(next(iter(rel_sides))) is not None:
        rel_sign = position_side_sign(next(iter(rel_sides)))
    other_sign = None
    if len(other_sides) == 1 and position_side_sign(next(iter(other_sides))) is not None:
        other_sign = position_side_sign(next(iter(other_sides)))

    # release-leg coherence: all releases must be the same permitted leg and
    # belong to one logical release event (codex: never silently filter)
    _rel_legs = {str(f.get("leg") or "").upper() for f in releases}
    _rel_leg_unknown = {l for l in _rel_legs if l not in ("NEAR", "FAR")}
    _rel_evts = {str(f.get("release_id") or f.get("group_id") or "") for f in releases}
    _rel_evts.discard("")

    # all release fills (released leg) + all sibling exit fills
    rel_fills = [f for f in releases if str(f.get("leg") or "").upper() == released_leg]
    sibling_fills = [f for f in exits if str(f.get("leg") or "").upper() == other_leg]
    rel_closed_qty = sum(float(f.get("qty") or 0) for f in rel_fills)
    other_closed_qty = sum(float(f.get("qty") or 0) for f in sibling_fills)

    release_ts = rel_fills[0].get("ts") or rel_fills[0].get("timestamp")

    # reconcile EVERY fill
    rel_results = [
        reconcile_fill(f, entry_avg_rel, rel_mult, fees=_fill_fee(f, fee_schedule),
                       stored_realized=f.get("realized_pnl"))
        for f in rel_fills
    ]
    other_results = [
        reconcile_fill(f, entry_avg_other, other_mult, fees=_fill_fee(f, fee_schedule),
                       stored_realized=f.get("realized_pnl"))
        for f in sibling_fills
    ]

    # quotes at release_ts (strictly <= release_ts)
    rel_tick, rel_age = select_quote(ticks_by_code.get(rel_code, []), release_ts, age_bound_s)
    other_tick, other_age = select_quote(ticks_by_code.get(other_code, []), release_ts, age_bound_s)
    tier, _tier_detail = tier_for_legs(
        {released_leg: (rel_tick, rel_age), other_leg: (other_tick, other_age)}, age_bound_s
    )

    # structural integrity checks (P0 codex blockers)
    flags: List[str] = []
    if len(rel_sides) > 1:
        flags.append("conflicting_entry_sides")
    if len(other_sides) > 1:
        flags.append("conflicting_entry_sides")
    if rel_bad_px:
        flags.append("zero_or_missing_entry_price")
    if other_bad_px:
        flags.append("zero_or_missing_entry_price")
    if rel_sign is None:
        flags.append("missing_position_side")
    if other_sign is None and other_open_qty > 0:
        flags.append("missing_position_side")
    if len(_rel_legs) > 1:
        flags.append("mixed_release_legs")
    if _rel_leg_unknown:
        flags.append("unknown_release_leg")
    if len(_rel_evts) > 1:
        flags.append("multiple_release_events")

    # full-close verification (P0 blocker #2): never fabricate a settled trade
    fully_closed = (
        rel_open_qty > 0 and other_open_qty > 0
        and abs(rel_closed_qty - rel_open_qty) < 1e-9
        and abs(other_closed_qty - other_open_qty) < 1e-9
        and rel_sign is not None and other_sign is not None
        and entry_avg_rel is not None and entry_avg_other is not None
        and not rel_bad_px and not other_bad_px
        and len(_rel_legs) == 1 and not _rel_leg_unknown and len(_rel_evts) <= 1
    )
    if not fully_closed:
        if rel_closed_qty < rel_open_qty - 1e-9:
            flags.append("partial_release")
        elif rel_closed_qty > rel_open_qty + 1e-9:
            flags.append("overclosed_release")
        if other_closed_qty < other_open_qty - 1e-9:
            flags.append("partial_sibling_exit")
        elif other_closed_qty > other_open_qty + 1e-9:
            flags.append("overclosed_sibling_exit")
        if other_open_qty > 0 and other_closed_qty < 1e-9:
            flags.append("missing_sibling_exit")

    # pre_release_paired_pnl (gross): released leg AGGREGATE actual + other marked
    pre_release_paired_pnl = None
    rel_actual = None
    if rel_sign is not None and entry_avg_rel is not None and rel_closed_qty > 0:
        rel_wavg = sum(
            float(f.get("price") or 0) * float(f.get("qty") or 0) for f in rel_fills
        ) / rel_closed_qty
        rel_actual = (rel_wavg - entry_avg_rel) * rel_closed_qty * rel_mult * rel_sign
        other_mark = None
        if other_tick is not None and other_sign is not None and entry_avg_other is not None:
            px, _src = valuation_price(other_tick, "BUY" if other_sign < 0 else "SELL")
            other_mark = px
        if other_mark:
            other_actual_mark = (other_mark - entry_avg_other) * other_open_qty * other_mult * other_sign
            pre_release_paired_pnl = round(rel_actual + other_actual_mark, 2)

    # release_time_combined_valuation_gross: both legs via quotes
    val = None
    if (tier != TIER_UNUSABLE and rel_tick is not None and other_tick is not None
            and rel_sign is not None and other_sign is not None
            and entry_avg_rel is not None and entry_avg_other is not None):
        r_px, _ = valuation_price(rel_tick, "BUY" if rel_sign < 0 else "SELL")
        o_px, _ = valuation_price(other_tick, "BUY" if other_sign < 0 else "SELL")
        if r_px and o_px:
            rv = (r_px - entry_avg_rel) * rel_open_qty * rel_mult * rel_sign
            ov = (o_px - entry_avg_other) * other_open_qty * other_mult * other_sign
            val = round(rv + ov, 2)
    immediate_exec = round(val, 2) if (tier == TIER_EXECUTABLE_BBO and val is not None) else None

    # actual_full (gross + net): only when fully closed AND every necessary
    # reconciliation result is trustworthy — CORRUPT/UNRECONCILED results (or
    # missing expected_realized) NEVER contribute a fabricated 0.0 (codex D3).
    _res_list = [r for r in rel_results + other_results if r is not None]
    actual_gross = None
    actual_net = None
    fee_uncertain = False
    _recon_trusted = (
        bool(_res_list)
        and all(
            r.expected_realized is not None
            and r.status not in ("CORRUPT", "UNRECONCILED")
            for r in _res_list
        )
    )
    if fully_closed and _recon_trusted:
        _g = 0.0
        _n = 0.0
        for _r in _res_list:
            if _r.expected_realized is None:
                continue
            fee = _fill_fee(_r.fill, fee_schedule)
            _g += _r.expected_realized + (fee or 0.0)
            _n += _r.expected_realized
            if _r.status == "FEE_UNCERTAIN":
                fee_uncertain = True
        actual_gross = round(_g, 2)
        actual_net = round(_n, 2) if not fee_uncertain else None

    # underlying diagnostics: every stored-vs-recomputed mismatch, exposed
    # even when a higher-severity structural flag dominates the status.
    _recon_diag = [
        {
            "fill_type": _r.fill.get("fill_type"),
            "leg": str(_r.fill.get("leg") or "").upper(),
            "expected": _r.expected_realized,
            "stored": _r.stored_realized,
            "delta": round(_r.delta, 2),
        }
        for _r in _res_list if _r.delta is not None
    ]

    post_inc = None
    if actual_gross is not None and pre_release_paired_pnl is not None:
        post_inc = round(actual_gross - pre_release_paired_pnl, 2)

    unhedged_s = None
    if sibling_fills:
        t1 = release_ts
        t2 = sibling_fills[-1].get("ts") or sibling_fills[-1].get("timestamp")
        if t1 and t2:
            unhedged_s = round((t2 - t1).total_seconds(), 2)

    # deterministic status aggregation: reconcile results + structural flags
    if flags:
        _res_list.append(ReconcileResult(
            fill={"trade_id": trade_id}, status=STATUS_UNRECONCILED, issue_flags=flags,
        ))
    status, agg_flags = aggregate_status(_res_list)
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
        "release_fill_count": len(rel_fills),
        "sibling_exit_fill_count": len(sibling_fills),
        "pre_release_paired_pnl": pre_release_paired_pnl,
        "release_time_combined_valuation_gross": val,
        "valuation_tier": tier,
        "immediate_executable_combined_pnl_gross": immediate_exec,
        "actual_full_pnl_gross": actual_gross,
        "actual_full_pnl_net": actual_net,
        "post_release_incremental_pnl": post_inc,
        "unhedged_seconds": unhedged_s,
        "status": status,
        "issue_flags": agg_flags,
        "reconcile_diagnostics": _recon_diag,
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
