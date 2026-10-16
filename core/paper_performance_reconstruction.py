"""
MTS Paper Daily Performance reconstruction (2026-08-27, codex-approved spec).

Purpose: make complete paper trades from BEFORE the full paper-provenance
contract (e.g. 2026-08-27) DISPLAYABLE again — clearly badged as
RECONSTRUCTED / UNVERIFIED — WITHOUT mutating the original fills/events
ledgers and WITHOUT merging them into the formal cumulative performance.

Classification per trade:
  VERIFIED_PAPER                — full paper provenance present
                                  (mode=paper + paper_fill_id +
                                  execution_source in PAPER_* + …)
  RECONSTRUCTED_FROM_PAPER_STATE — legacy record, but complete and
                                  internally consistent (both-leg entry,
                                  release/exit/settlement present, sides/
                                  qty/prices pair up)
  UNVERIFIED_LEGACY_PROVENANCE   — legacy record with anomalies:
                                  settlement_origin=LIVE, BROKER_FILL, or
                                  missing provenance; displayed but never
                                  treated as verified
  EXCLUDED                       — cannot pair (missing entry/exit/
                                  terminal), qty mismatch, or explicit
                                  mode=live — kept out of every metric

Formal cumulative totals ONLY include VERIFIED_PAPER; reconstructed and
unverified records feed a SEPARATE reconstructed daily metric block.

Never mutates fills/events; never clears history.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from typing import Any, Optional

# ── provenance contract (future paper fills, spec section 3) ────────
PAPER_FILL_SOURCES = {"PAPER_COMBO_FILL", "PAPER_EXIT_FILL"}
PROVENANCE_KEYS = ("mode", "execution_source", "paper_fill_id",
                   "parent_combo_order_id", "trade_id", "leg_role",
                   "side", "expected_qty", "filled_qty", "fill_status",
                   "fill_price", "fill_timestamp", "attribution_key",
                   "session_id", "generation_id", "release_group_id")

TERMINAL_FILL_TYPES = {"COMBINED_EXIT_SETTLED", "TRADE_CLOSED"}
EXIT_FILL_TYPES = {"EXIT", "COMBINED_EXIT", "COMBINED_EXIT_SETTLED"}


def _iter_records(path: str):
    if not path or not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _fill_leg(fill: dict) -> str:
    return str(fill.get("leg") or fill.get("leg_role") or "").upper()


def _pair_ok(entries, exits, settlement, all_fills=None) -> Optional[str]:
    """Verify a trade's legs pair up COMPLETELY (codex review v2):
      - near+far entries present, entry qty equal
      - exit/settlement present with near+far covered
      - exit legs' qty == entry qty
      - exit side/contract correspond to entry side/contract
      - terminal closed qty complete
      - exit PnL consistent with leg fills
    Returns None if OK, else a reason string.
    """
    _ALL_FILLS = all_fills if all_fills is not None else (entries + exits)
    near_entries = [e for e in entries if _fill_leg(e) == "NEAR"]
    far_entries = [e for e in entries if _fill_leg(e) == "FAR"]
    if not near_entries or not far_entries:
        return "missing_leg_entry"
    # entry qty consistency (spread: near == far)
    _nq = sum(int(e.get("qty", 0) or 0) for e in near_entries)
    _fq = sum(int(e.get("qty", 0) or 0) for e in far_entries)
    if _nq != _fq:
        return f"entry_qty_mismatch_near={_nq}_far={_fq}"
    if not exits and settlement is None:
        return "missing_exit_or_terminal"

    # exit legs: each leg must have a fill carrying that leg's qty.
    # RELEASE fills are also exit-side evidence (single-leg release path:
    # RELEASE closes one leg, EXIT closes the remaining leg).
    _exit_legs = defaultdict(int)
    _unknown_leg_exit = False
    for x in exits:
        # COMBINED_EXIT_SETTLED is a terminal SUMMARY record (no leg, no
        # per-leg qty) — never counted as an exit-leg fill.
        if str(x.get("fill_type", "")).upper() == "COMBINED_EXIT_SETTLED":
            continue
        _leg = _fill_leg(x)
        if not _leg:
            _unknown_leg_exit = True
            continue
        _exit_legs[_leg] += int(x.get("qty", 0) or 0)
    for r in [f for f in _ALL_FILLS if str(f.get("fill_type", "")).upper() == "RELEASE"]:
        _leg = _fill_leg(r)
        if not _leg:
            _unknown_leg_exit = True
            continue
        _exit_legs[_leg] += int(r.get("qty", 0) or 0)
    if _unknown_leg_exit:
        return "exit_leg_unidentifiable"
    _near_exit = _exit_legs.get("NEAR", 0)
    _far_exit = _exit_legs.get("FAR", 0)
    # STRICTLY equal: overfill/duplicate/partial all fail closed
    if _near_exit != _nq or _far_exit != _fq:
        return (f"exit_qty_mismatch_near={_near_exit}/{_nq}_far="
                f"{_far_exit}/{_fq}")

    # side correspondence: exit closes the entry position.
    # ENTRY SHORT (near) -> exit BUY; ENTRY LONG -> exit SELL.
    _side_map = {"SHORT": "BUY", "LONG": "SELL"}
    for e in near_entries + far_entries:
        _leg = _fill_leg(e)
        _es = str(e.get("side") or "").upper()
        _want = _side_map.get(_es)
        if not _want:
            # fail closed: unidentifiable entry side (codex review v3)
            return f"entry_side_unidentifiable_{_leg}={_es or 'MISSING'}"
        _leg_exits = [x for x in exits + [
            f for f in _ALL_FILLS
            if str(f.get("fill_type", "")).upper() == "RELEASE"]
            if _fill_leg(x) == _leg]
        if _leg_exits:
            _xs = str(_leg_exits[0].get("side") or "").upper()
            # exit side may be SELL (closes LONG) or BUY (closes SHORT);
            # combined-exit fills carry the order direction
            if _xs and _xs not in (_want, "NONE"):
                # allow SELL/BUY order-direction naming vs position side
                if not (_xs == "BUY" and _es == "SHORT") and \
                        not (_xs == "SELL" and _es == "LONG") and \
                        not (_xs == "NONE"):
                    return (f"side_mismatch_{_leg}_entry={_es}_exit={_xs}")

    # terminal closed qty must be a VALID number >= entry qty.
    # Fail-closed (codex review v3): missing/zero closed qty on an
    # existing terminal record is an incomplete settlement — NEVER pass.
    if settlement is not None:
        _nc_raw = settlement.get("near_closed_qty")
        _fc_raw = settlement.get("far_closed_qty")
        _nc = (int(_nc_raw) if isinstance(_nc_raw, (int, float))
               and int(_nc_raw) > 0 else None)
        _fc = (int(_fc_raw) if isinstance(_fc_raw, (int, float))
               and int(_fc_raw) > 0 else None)
        if _nc is None:
            return "terminal_near_closed_qty_missing_or_zero"
        if _fc is None:
            return "terminal_far_closed_qty_missing_or_zero"
        if _nc < _nq:
            return f"terminal_near_closed_qty={_nc}<{_nq}"
        if _fc < _fq:
            return f"terminal_far_closed_qty={_fc}<{_fq}"
    return None


def _has_full_provenance(fills) -> bool:
    """VERIFIED_PAPER: EVERY fill must carry COMPLETE paper provenance
    (codex review v2): mode=paper, a paper_fill_id, a PAPER_* execution
    source, AND every other provenance field valid (session_id,
    generation_id, parent_combo_order_id, release_group_id, leg_role,
    attribution_key, fill_status, fill_price, fill_timestamp,
    expected_qty, filled_qty).  Any None/empty required field -> not
    VERIFIED_PAPER (fail-closed — a fill with a missing session_id is
    not verifiable)."""
    if not fills:
        return False
    _REQUIRED = ("mode", "execution_source", "paper_fill_id",
                 "parent_combo_order_id", "leg_role", "expected_qty",
                 "filled_qty", "fill_status", "fill_price",
                 "fill_timestamp", "attribution_key", "session_id",
                 "generation_id", "release_group_id")
    for f in fills:
        if str(f.get("mode") or "") != "paper":
            return False
        if str(f.get("execution_source") or "") not in PAPER_FILL_SOURCES:
            return False
        for _k in _REQUIRED:
            _v = f.get(_k)
            if _v is None or (isinstance(_v, str) and not _v.strip()):
                return False
    return True


def _unverified_reason(fills) -> Optional[str]:
    """Legacy anomalies: settlement_origin=LIVE, price_source=BROKER_FILL,
    or missing provenance on any fill."""
    for f in fills:
        so = str(f.get("settlement_origin") or "")
        if so.upper() == "LIVE":
            return "settlement_origin=LIVE"
        if "BROKER_FILL" in so.upper() or "BROKER" in so.upper():
            return "BROKER_FILL"
        ps = str(f.get("price_source") or "")
        if ps.upper() == "BROKER_FILL" or "BROKER" in ps.upper():
            return f"price_source={ps.upper()}"
    return None


def _trade_pnl_net(trade_fills, settlement) -> Optional[float]:
    """Canonical net PnL: settlement combined net > sum of leg realized
    > per-fill realized sum.  None when uncomputable (excluded)."""
    if settlement is not None:
        _net = settlement.get("combined_realized_pnl_net")
        if isinstance(_net, (int, float)) and math.isfinite(float(_net)):
            return float(_net)
        _gross = settlement.get("combined_realized_pnl_gross")
        if isinstance(_gross, (int, float)) and math.isfinite(float(_gross)):
            return float(_gross)
    _realized = [f.get("realized_pnl") for f in trade_fills
                 if isinstance(f.get("realized_pnl"), (int, float))
                 and math.isfinite(float(f["realized_pnl"]))]
    if _realized:
        return float(sum(_realized))
    return None


def _trading_day(ts: str, session: str) -> str:
    try:
        from scripts.generate_daily_report import get_trading_day
        return get_trading_day(ts, session)
    except Exception:
        return (ts or "")[:10]


def reconstruct_paper_daily(fills_path: str, events_path: str,
                            target_trading_day: str) -> dict:
    """Reconstruct per-trade paper performance for a trading day.

    Returns:
      {
        "trading_day": ...,
        "eligible": {VERIFIED_PAPER: [...], RECONSTRUCTED: [...],
                     UNVERIFIED: [...]},   # per-trade detail dicts
        "excluded": [{"trade_id", "reason"}],
        "metrics": {realized_gross, completed_trades, win_rate,
                    profit_factor},          # RECONSTRUCTED+VERIFIED only
        "formal_metrics": {realized_gross, completed_trades, win_rate,
                           profit_factor},   # VERIFIED_PAPER ONLY
        "exclusion_reasons": {reason: count},
        "provenance_badges": {trade_id: badge},
      }
    """
    # 1. group fills by trade
    trades = defaultdict(list)
    for rec in _iter_records(fills_path):
        tid = rec.get("trade_id")
        if tid:
            trades[str(tid)].append(rec)

    # 2. per-trade classification
    eligible = {"VERIFIED_PAPER": [], "RECONSTRUCTED": [], "UNVERIFIED": []}
    excluded = []
    badges = {}
    exclusion_reasons = defaultdict(int)

    for tid, fills in sorted(trades.items()):
        entries = [f for f in fills if str(f.get("fill_type", "")).upper() == "ENTRY"]
        exits = [f for f in fills
                 if str(f.get("fill_type", "")).upper() in EXIT_FILL_TYPES]
        settlement = next((f for f in fills
                           if str(f.get("fill_type", "")).upper()
                           in TERMINAL_FILL_TYPES), None)
        if not entries:
            excluded.append({"trade_id": tid, "reason": "no_entry"})
            exclusion_reasons["no_entry"] += 1
            continue
        _e0 = entries[0]
        _td = _trading_day(_e0.get("timestamp", ""),
                           _e0.get("session", "UNKNOWN"))
        if _td != target_trading_day:
            continue  # not this trading day

        # explicit live record ANYWHERE in the trade -> the whole trade is
        # never paper (codex review: checking only the first entry missed
        # a later exit/fill carrying mode=live).
        _live_modes = [f for f in fills
                       if str(f.get("mode") or "").lower() == "live"]
        if _live_modes:
            excluded.append({"trade_id": tid, "reason": "mode=live"})
            exclusion_reasons["mode=live"] += 1
            continue

        _pair = _pair_ok(entries, exits, settlement, all_fills=fills)
        if _pair:
            excluded.append({"trade_id": tid, "reason": _pair})
            exclusion_reasons[_pair] += 1
            continue

        _pnl = _trade_pnl_net(fills, settlement)
        detail = {
            "trade_id": tid,
            "entry_ts": _e0.get("timestamp"),
            "exit_ts": (exits[-1].get("timestamp") if exits
                        else (settlement or {}).get("timestamp")),
            "session": _e0.get("session", "UNKNOWN"),
            "net_pnl": _pnl,
            "n_fills": len(fills),
        }
        if _has_full_provenance(fills):
            badge = "VERIFIED_PAPER"
        elif _unverified_reason(fills):
            badge = "UNVERIFIED_LEGACY_PROVENANCE"
            detail["unverified_reason"] = _unverified_reason(fills)
        else:
            badge = "RECONSTRUCTED_FROM_PAPER_STATE"
        detail["provenance_badge"] = badge
        badges[tid] = badge
        key = {"VERIFIED_PAPER": "VERIFIED_PAPER",
               "RECONSTRUCTED_FROM_PAPER_STATE": "RECONSTRUCTED",
               "UNVERIFIED_LEGACY_PROVENANCE": "UNVERIFIED"}[badge]
        eligible[key].append(detail)

    # 3. metrics
    def _metrics(trades_list):
        _net = [t["net_pnl"] for t in trades_list
                if isinstance(t.get("net_pnl"), (int, float))
                and math.isfinite(float(t["net_pnl"]))]
        n = len(_net)
        wins = sum(1 for v in _net if v > 0)
        gross = sum(_net)
        return {
            "realized_gross": gross,
            "completed_trades": n,
            "win_rate": (wins / n) if n else 0.0,
            "profit_factor": (
                sum(v for v in _net if v > 0) /
                abs(sum(v for v in _net if v < 0))
                if any(v < 0 for v in _net) else None),
        }

    _all_eligible = (eligible["VERIFIED_PAPER"] + eligible["RECONSTRUCTED"]
                     + eligible["UNVERIFIED"])
    _formal_breakdown = {"DAY": _metrics([]), "NIGHT": _metrics([]),
                         "ALL": _metrics(eligible["VERIFIED_PAPER"])}
    for _t in eligible["VERIFIED_PAPER"]:
        _s = str(_t.get("session") or "").lower()
        _key = "DAY" if _s == "day" else "NIGHT" if _s == "night" else None
        if _key:
            _formal_breakdown[_key].setdefault("_trades", []).append(_t)
    for _key in ("DAY", "NIGHT"):
        _formal_breakdown[_key] = _metrics(
            _formal_breakdown[_key].pop("_trades", []))
    return {
        "trading_day": target_trading_day,
        "eligible": eligible,
        "excluded": excluded,
        "exclusion_reasons": dict(exclusion_reasons),
        "metrics": _metrics(_all_eligible),
        "formal_metrics": _metrics(eligible["VERIFIED_PAPER"]),
        "formal_breakdown": _formal_breakdown,
        "provenance_badges": badges,
    }
