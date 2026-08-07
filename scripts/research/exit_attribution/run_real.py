"""Run the committed exit-attribution pipeline against REAL runtime data.

Produces (committed, research-only):
  reports/<run_id>/exit_attribution_per_release.csv
  reports/<run_id>/manifest.json            (immutable run manifest)
  reports/<run_id>/stats_report.json        (sign test / coverage / sensitivity)

Scope: research only. NO production edits, deploy, or GO claims.
"""
from __future__ import annotations

import csv
import glob
import json
import os
from datetime import datetime, timedelta
from statistics import median
from typing import Dict, List

from .manifest import build_manifest, write_manifest
from .pipeline import build_rows, SCHEMA_VERSION
from .stats import (
    apply_adverse_tick,
    exact_sign_test_p,
    split_nonzero,
)

RUNTIME_DIR = os.environ.get(
    "TRADING_RUNTIME_DIR",
    "/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime",
)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

FILLS_PATH = os.path.join(RUNTIME_DIR, "logs", "mts_trade_fills.jsonl")
RAW_TICKS_DIR = os.path.join(RUNTIME_DIR, "logs", "raw_ticks")
DATA_DIR = os.path.join(RUNTIME_DIR, "data")

CONTRACTS = {"TMFN": 10.0, "TMF": 10.0, "DEFAULT": 10.0}  # verified from fills
TICK_SIZE = 1.0
AGE_BOUND_S = 5.0


def load_fills() -> List[dict]:
    rows = []
    with open(FILLS_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            f = json.loads(line)
            if f.get("fill_type") in ("ENTRY", "RELEASE", "EXIT"):
                rows.append(f)
    return rows


def group_trades(fills: List[dict]) -> List[dict]:
    by_trade: Dict[str, dict] = {}
    for f in fills:
        tid = str(f.get("trade_id") or "")
        if not tid:
            continue
        t = by_trade.setdefault(tid, {"trade_id": tid, "entries": [], "releases": [], "exits": []})
        rec = {
            "leg": str(f.get("leg") or f.get("contract") or "").upper(),
            "side": str(f.get("side") or "").upper(),
            "qty": float(f.get("qty") or 0),
            "price": float(f.get("price") or 0),
            "ts": datetime.fromisoformat(f["timestamp"]),
            "realized_pnl": f.get("realized_pnl"),
            "fill_type": f.get("fill_type"),
            "ticker": f.get("ticker"),
            "trade_id": tid,
        }
        ft = f.get("fill_type")
        if ft == "ENTRY":
            t["entries"].append(rec)
        elif ft == "RELEASE":
            t["releases"].append(rec)
        elif ft == "EXIT":
            t["exits"].append(rec)
    trades = [t for t in by_trade.values() if t["releases"]]
    for t in trades:
        t["near_code"] = "TMFN"
        t["far_code"] = "TMF"
    return trades


def _load_csv_ticks(paths: List[str]) -> List[dict]:
    ticks = []
    for p in paths:
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                try:
                    ts = datetime.fromisoformat(r["ts"] if "ts" in r else r["timestamp"])
                except (KeyError, ValueError):
                    continue
                try:
                    px = float(r.get("price") or r.get("Close") or 0)
                except (TypeError, ValueError):
                    continue
                if px <= 0:
                    continue
                bid = float(r.get("bid_price") or 0)
                ask = float(r.get("ask_price") or 0)
                ticks.append({"ts": ts, "bid": bid, "ask": ask, "price": px})
    ticks.sort(key=lambda x: x["ts"])
    return ticks


def load_ticks() -> Dict[str, List[dict]]:
    raw_near = glob.glob(os.path.join(RAW_TICKS_DIR, "MXFH6_*_ticks.csv"))
    raw_far = glob.glob(os.path.join(RAW_TICKS_DIR, "TMFH6_*_ticks.csv"))
    ohlc_near = glob.glob(os.path.join(DATA_DIR, "tmf_near_*.csv"))
    ohlc_far = glob.glob(os.path.join(DATA_DIR, "tmf_far_*.csv"))
    return {
        "TMFN": _load_csv_ticks(raw_near + ohlc_near),
        "TMF": _load_csv_ticks(raw_far + ohlc_far),
    }


def loaded_tick_paths() -> Dict[str, str]:
    """Every raw/OHLC tick file actually loaded, for manifest hashing."""
    raw_near = glob.glob(os.path.join(RAW_TICKS_DIR, "MXFH6_*_ticks.csv"))
    raw_far = glob.glob(os.path.join(RAW_TICKS_DIR, "TMFH6_*_ticks.csv"))
    ohlc_near = glob.glob(os.path.join(DATA_DIR, "tmf_near_*.csv"))
    ohlc_far = glob.glob(os.path.join(DATA_DIR, "tmf_far_*.csv"))
    out = {}
    for p in sorted(raw_near + raw_far):
        out["tick_raw_" + os.path.basename(p)] = p
    for p in sorted(ohlc_near + ohlc_far):
        out["tick_ohlc_" + os.path.basename(p)] = p
    return out


def derive_fee_schedule(trades: List[dict]) -> dict:
    """Infer per-close fee from the ledger (LEDGER_INFERRED — NOT validated).

    Not circular when reported honestly: the fee is INFERRED from stored
    realized values, so reconciliation against the same values cannot
    validate it. Excludes CORRUPT trades; reports per-contract/qty
    consistency so the inference is auditable.
    """
    diffs = []
    per_contract: Dict[str, list] = {}
    per_qty: Dict[str, list] = {}
    for t in trades:
        tid = str(t.get("trade_id") or "")
        if tid in {"mts-auto-222204-082"}:
            continue  # CORRUPT — excluded from inference
        rel = t["releases"][0]
        leg = str(rel["leg"]).upper()
        entries = [e for e in t["entries"] if e["leg"] == leg]
        if not entries or rel.get("realized_pnl") is None:
            continue
        qty = sum(e["qty"] for e in entries)
        if qty <= 0:
            continue
        avg = sum(e["qty"] * e["price"] for e in entries) / qty
        sign = -1 if rel["side"] == "BUY" else 1
        gross = (rel["price"] - avg) * qty * CONTRACTS.get("DEFAULT", 10.0) * sign
        diff = gross - float(rel["realized_pnl"])
        diffs.append(diff)
        per_contract.setdefault(str(rel.get("ticker") or "TMF"), []).append(diff)
        per_qty.setdefault(str(int(qty)), []).append(diff)
    per_contract_stats = {k: {"n": len(v), "median": round(median(v), 2),
                              "min": round(min(v), 2), "max": round(max(v), 2)}
                          for k, v in per_contract.items()}
    per_qty_stats = {k: {"n": len(v), "median": round(median(v), 2)}
                     for k, v in per_qty.items()}
    per_contract_val = round(median(diffs), 2) if diffs else None
    return {
        "per_contract": per_contract_val,
        "provenance": "LEDGER_INFERRED/UNVERIFIED",
        "effective_date": "2026-07-01",
        "note": "inferred from stored realized_pnl vs recomputed gross on "
                "release fills (CORRUPT trades excluded). Reconciliation "
                "against the same values is NOT validation — see "
                "per_contract/per_qty consistency.",
        "sample_size": len(diffs),
        "per_contract_consistency": per_contract_stats,
        "per_qty_consistency": per_qty_stats,
    }


def write_csv(rows: List[dict], path: str) -> None:
    keys = [
        "trade_id", "release_ts", "released_leg", "release_fill_count",
        "sibling_exit_fill_count", "pre_release_paired_pnl",
        "release_time_combined_valuation_gross", "valuation_tier",
        "immediate_executable_combined_pnl_gross", "actual_full_pnl_gross",
        "actual_full_pnl_net", "post_release_incremental_pnl",
        "unhedged_seconds", "status", "issue_flags", "data_quality",
        "entry_attribution", "release_attribution", "quote_age_s",
        "schema_version",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def stats_report(rows: List[dict], fee_schedule: dict, run_id: str) -> dict:
    ok = [r for r in rows if r["data_quality"] == "OK"]
    deltas = [float(r["post_release_incremental_pnl"]) for r in ok
              if r.get("post_release_incremental_pnl") is not None]
    tiers = {}
    for r in rows:
        tiers[r["valuation_tier"]] = tiers.get(r["valuation_tier"], 0) + 1
    statuses = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    flag_counts: Dict[str, int] = {}
    for r in rows:
        for f in r.get("issue_flags") or []:
            flag_counts[f] = flag_counts.get(f, 0) + 1
    mismatches = [
        {"trade_id": r["trade_id"], "expected": r["actual_full_pnl_gross"],
         "status": r["status"]}
        for r in rows if r["status"] == "MISMATCH"
    ]
    ages = [v for r in rows for v in (r.get("quote_age_s") or {}).values()
            if isinstance(v, (int, float))]
    # pair skew: |age_near - age_far| per row where both legs quoted
    skews = []
    for r in rows:
        qa = r.get("quote_age_s") or {}
        if len(qa) == 2 and all(isinstance(v, (int, float)) for v in qa.values()):
            a, b = qa.values()
            skews.append(abs(a - b))
    p = exact_sign_test_p(deltas)

    def pctiles(vals):
        if not vals:
            return {"p50": None, "p95": None, "p99": None, "max": None}
        s = sorted(vals)
        def q(p_):
            i = min(len(s) - 1, int(p_ * len(s)))
            return round(s[i], 3)
        return {"p50": q(0.50), "p95": q(0.95), "p99": q(0.99), "max": round(s[-1], 3)}

    return {
        "run_id": run_id,
        "n_trades": len(rows),
        "n_ok": len(ok),
        "sign_test": {
            "deltas_used": len(deltas),
            "positive": split_nonzero(deltas)["positive"],
            "negative": split_nonzero(deltas)["negative"],
            "zero": split_nonzero(deltas)["zero"],
            "exact_two_sided_p": p,
            "interpretation": ("insufficient data" if p is None else
                               ("significant" if p < 0.05 else "not significant")),
        },
        "quote_coverage": {
            "EXECUTABLE_BBO": tiers.get("EXECUTABLE_BBO", 0),
            "BOUNDED_TICK_PROXY": tiers.get("BOUNDED_TICK_PROXY", 0),
            "UNUSABLE": tiers.get("UNUSABLE", 0),
            "note": "raw last-price (bid/ask <= 0) is PROXY by design; "
                    "1-min OHLC Close used as last-price proxy only when "
                    "within age bound",
            "quote_age_s": pctiles(ages),
            "pair_skew_ms_pctiles": pctiles([s * 1000 for s in skews]) if skews
                                    else {"p50": None, "p95": None, "p99": None, "max": None},
        },
        "reconciliation": {
            "status_counts": statuses,
            "flag_counts": flag_counts,
            "mismatch_list": mismatches,
            "corrupt_trades_excluded_from_sign_stats": [
                r["trade_id"] for r in rows if "corrupt_realized_pnl" in (r.get("issue_flags") or [])
            ],
        },
        "sensitivity": {
            "note": "tick adverse re-valuation NOT AVAILABLE for this run "
                    "(quote tiers are proxy/unusable; see coverage)",
            "available": False,
        },
        "fee_model": fee_schedule,
        "gate_notes": [
            "research-only; no production change, no GO claim",
            "immediate_executable_combined_pnl_gross populated ONLY for EXECUTABLE_BBO",
            "fee provenance: LEDGER_INFERRED/UNVERIFIED (not validated)",
        ],
    }


def main() -> None:
    fills = load_fills()
    trades = group_trades(fills)
    ticks = load_ticks()
    fee_schedule = derive_fee_schedule(trades)
    rows = build_rows(trades, ticks, CONTRACTS, fee_schedule=fee_schedule,
                      age_bound_s=AGE_BOUND_S)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(REPORTS_DIR, run_id)
    os.makedirs(out_dir, exist_ok=True)

    csv_path = os.path.join(out_dir, "exit_attribution_per_release.csv")
    write_csv(rows, csv_path)

    manifest = build_manifest(
        run_id=run_id,
        input_paths={"fills": FILLS_PATH, **loaded_tick_paths()},
        schema_version=SCHEMA_VERSION,
        repo_root=REPO_ROOT,
        fee_source_path="",
        fee_effective_date=fee_schedule.get("effective_date", ""),
        config={"contracts": CONTRACTS, "age_bound_s": AGE_BOUND_S,
                "tick_size": TICK_SIZE, "fee_schedule": fee_schedule,
                "tick_file_retention": "raw_ticks (7/23-7/28) + 1-min OHLC"},
        dirty_exclude=(f"reports/{run_id}/",),
    )
    manifest_path = write_manifest(manifest, out_dir)

    report = stats_report(rows, fee_schedule, run_id)
    report_path = os.path.join(out_dir, "stats_report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print(json.dumps({
        "run_id": run_id,
        "trades_with_release": len(trades),
        "rows": len(rows),
        "csv": csv_path,
        "manifest": manifest_path,
        "stats": report_path,
        "sign_test_p": report["sign_test"]["exact_two_sided_p"],
        "coverage": report["quote_coverage"],
        "status_counts": report["reconciliation"]["status_counts"],
        "fee_derived": fee_schedule["per_contract"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
