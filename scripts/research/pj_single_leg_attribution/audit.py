#!/usr/bin/env python3
"""Read-only historical single-leg Policy J attribution audit (design v6).

Design: .planning/pj_single_leg_attribution_design.md (commits 9a5b4b34 →
34ceca6d). Read-only: never writes ledgers/state/dashboard; the only output
is the artifact under {runtime}/exports/research/.

Causal positions (v6, codex-accepted):
- SUPPORTED = 0 by design: no trigger-named Policy J event exists in the
  current events schema (only PEAK_CONFIRMED / PEAK_REJECTED /
  TRIGGER_SUPPRESSED — none of which is a trigger/winner decision).
- CONTRADICTED = 0 by design: no same-trade final-decision cause marker
  exists (RELEASE_*_SUBMITTED carry trade_id=None and no cause field).
- INSUFFICIENT_EVIDENCE → INFERRED_ELIGIBLE only when: params resolvable
  (deployed config provenance) AND timestamps tz-clean AND eligibility
  conditions hold; everything else NOT_PROVABLE.

Schema contract (v6):
- Explicit finite fill_type allowlist (no wildcards): ENTRY, EXIT, RELEASE,
  COMBINED_EXIT, COMBINED_EXIT_NEAR, COMBINED_EXIT_FAR,
  COMBINED_EXIT_COMPLETED, COMBINED_EXIT_SETTLED, TEST.
- TEST is known test-contamination: excluded from candidates, counted; a
  TEST row with a nonempty trade_id excludes the WHOLE trade and counts
  TEST_TRADE_CONTAMINATION. TEST side enums are recorded, never unreadable.
- Any unknown fill_type / side / missing core key → whole snapshot
  UNREADABLE (fail closed). Any malformed/torn JSONL line → whole snapshot
  UNREADABLE.
- events: event+ts required globally; trade_id optional globally, required
  for per-trade evidence types (POLICY_J_PEAK_CONFIRMED / _REJECTED /
  TRIGGER_SUPPRESSED); global events without trade_id are counted, never
  UNREADABLE.
- Timestamps parsed timezone-aware; naive/mixed/missing/uncertain → the
  candidate is NOT_PROVABLE with eligibility_consistent=null (UTC+8
  assumption used for display/ordering only, never for eligibility).

Usage:
  python audit.py [--output-dir DIR]   (artifact default: runtime/exports/research/)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── v6 explicit allowlists (no wildcards) ──────────────────────────────────
FILL_TYPE_ALLOWLIST = {
    "ENTRY", "EXIT", "RELEASE",
    "COMBINED_EXIT", "COMBINED_EXIT_NEAR", "COMBINED_EXIT_FAR",
    "COMBINED_EXIT_COMPLETED", "COMBINED_EXIT_SETTLED",
    "TEST",
}
COMBINED_EXIT_TYPES = {
    "COMBINED_EXIT", "COMBINED_EXIT_NEAR", "COMBINED_EXIT_FAR",
    "COMBINED_EXIT_COMPLETED", "COMBINED_EXIT_SETTLED",
}
PER_TRADE_EVIDENCE_TYPES = {
    "POLICY_J_PEAK_CONFIRMED",
    "POLICY_J_PEAK_REJECTED",
    "POLICY_J_TRIGGER_SUPPRESSED",
}
# fill_type → allowed side set (None = record only, never unreadable)
SIDE_ALLOW: dict[str, Optional[set]] = {
    "ENTRY": {"LONG", "SHORT"},
    "EXIT": {"BUY", "SELL"},
    "RELEASE": {"BUY", "SELL"},
}
for _ct in COMBINED_EXIT_TYPES:
    SIDE_ALLOW[_ct] = {"BUY", "SELL", "NONE", ""}
SIDE_ALLOW["TEST"] = None

FILLS_REQUIRED_KEYS = {"trade_id", "timestamp", "leg", "contract", "side",
                       "fill_type", "qty", "price"}
EVENTS_REQUIRED_KEYS = {"event", "ts"}

DEFAULT_RUNTIME = "/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime"
TRIGGER_NAMED_EVENTS = {"POLICY_J_TRIGGERED", "POLICY_J_SINGLE_LEG_TRIGGERED"}
FINAL_DECISION_EVENT_TYPES = {"EXIT_SUBMITTED", "RELEASE_NEAR_SUBMITTED",
                              "RELEASE_FAR_SUBMITTED", "COMBINED_EXIT_SUBMITTED"}
NON_POLICY_J_CAUSES = ("trail", "release_threshold", "not_policy_j")
PARAMS_CURRENT = {"activation_twd": 200, "giveback_twd": 50, "mult": 10, "friction": 92}


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class SnapshotLoadError(Exception):
    """Whole-snapshot failure (malformed/torn/unknown schema)."""


def load_snapshot(path: Path) -> tuple[list[dict], dict]:
    """Read the input ONCE as immutable bytes; parse from memory.

    Raises SnapshotLoadError on any malformed/torn line. Returns
    (rows, meta) where meta carries the parsed-byte sha256, byte count,
    snapshot_read_ts, timestamp offset distribution and malformed counts.
    """
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise SnapshotLoadError(f"unreadable input {path}: {e}")
    meta = {
        "sha256": _sha256_bytes(raw),
        "bytes": len(raw),
        "snapshot_read_ts": datetime.now().isoformat(),
        "torn_lines": 0,
        "malformed_lines": 0,
        "timestamp_offsets": Counter(),
        "rows": 0,
    }
    rows: list[dict] = []
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise SnapshotLoadError(
            f"SNAPSHOT_MALFORMED: invalid UTF-8 bytes in {path} — "
            f"no classifications produced"
        )
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            meta["malformed_lines"] += 1
            continue
        if not isinstance(row, dict):
            meta["malformed_lines"] += 1
            continue
        if _bad_qty(row):
            meta["malformed_lines"] += 1
            continue
        rows.append(row)
        meta["rows"] += 1
        _record_ts_offset(row, meta["timestamp_offsets"])
    if meta["malformed_lines"]:
        err = SnapshotLoadError(
            f"SNAPSHOT_MALFORMED: {meta['malformed_lines']} malformed JSONL "
            f"line(s) in {path} — no classifications produced"
        )
        err.meta = meta
        raise err
    return rows, meta


def _bad_qty(row: dict) -> bool:
    """NaN / inf / fractional / missing qty → malformed (never int-truncated).
    Applied ONLY where qty semantics are defined (fills rows carry fill_type;
    event rows have no qty contract and are not checked here)."""
    if "fill_type" not in row or "qty" not in row:
        return False
    q = row.get("qty")
    if isinstance(q, bool):
        return True
    if isinstance(q, int):
        return False
    if isinstance(q, float):
        if q != q or q in (float("inf"), float("-inf")):   # NaN / inf
            return True
        return q != int(q)                                  # fractional (1.5)
    return True                                             # string/other


def _record_ts_offset(row: dict, counter: Counter) -> None:
    ts = row.get("ts") or row.get("timestamp")
    if not isinstance(ts, str) or not ts:
        counter["missing"] += 1
        return
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            counter["naive"] += 1
        else:
            off = dt.strftime("%z") or "other"
            counter[off if off else "other"] += 1
    except Exception:
        counter["unparseable"] += 1


# ── timestamp semantics (v6 §3) ────────────────────────────────────────────

def parse_ts(value) -> tuple[Optional[datetime], str]:
    """Return (dt, flag). flag ∈ ok|naive|missing|unparseable."""
    if not isinstance(value, str) or not value:
        return None, "missing"
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return None, "unparseable"
    if dt.tzinfo is None:
        return dt, "naive"
    return dt, "ok"


# ── schema validation (v6 §6) ──────────────────────────────────────────────

def validate_fills_schema(rows: list[dict]) -> dict:
    """Returns {} if OK, else a mismatch detail dict → UNREADABLE."""
    if not rows:
        return {"reason": "empty_fills_snapshot"}
    # required keys on EVERY row, not just the first
    for i, r in enumerate(rows):
        missing = FILLS_REQUIRED_KEYS - set(r.keys())
        if missing:
            return {"reason": "missing_core_keys", "row": i,
                    "missing": sorted(missing)}
    observed_by_type: dict[str, list] = defaultdict(list)
    for r in rows:
        ft = str(r.get("fill_type") or "")
        side = r.get("side")
        observed_by_type[ft].append(side)
    unknown_types = set(observed_by_type) - FILL_TYPE_ALLOWLIST
    if unknown_types:
        return {"reason": "unknown_fill_type", "types": sorted(unknown_types)}
    bad_sides = []
    for ft, sides in observed_by_type.items():
        allowed = SIDE_ALLOW.get(ft)
        if allowed is None:
            continue  # TEST: recorded, never unreadable
        for s in sides:
            if s not in allowed:
                bad_sides.append({"fill_type": ft, "side": s})
    if bad_sides:
        return {"reason": "side_out_of_allowlist", "cases": bad_sides[:20]}
    return {}


def validate_events_schema(rows: list[dict]) -> dict:
    """Returns {} if OK, else a mismatch detail dict → UNREADABLE."""
    if not rows:
        return {"reason": "empty_events_snapshot"}
    for i, r in enumerate(rows):
        missing = EVENTS_REQUIRED_KEYS - set(r.keys())
        if missing:
            return {"reason": "missing_core_keys", "row": i,
                    "missing": sorted(missing)}
    event_counts = Counter(str(r.get("event") or "") for r in rows)
    per_trade_missing = Counter()
    for r in rows:
        ev = str(r.get("event") or "")
        if ev in PER_TRADE_EVIDENCE_TYPES and not r.get("trade_id"):
            per_trade_missing[ev] += 1
    return {
        "event_types": dict(event_counts),
        "global_events_without_trade_id": sum(1 for r in rows if not r.get("trade_id")),
        "per_trade_evidence_missing_trade_id": dict(per_trade_missing),
    }


# ── candidate selection (v6 §2) ────────────────────────────────────────────

def select_candidates(fills: list[dict]) -> tuple[list[dict], dict]:
    """Group fills by trade_id and apply the integrity contract.

    Returns (candidates, rejected_counts). A trade is a candidate iff:
    both legs ENTRY qty=1 side∈{LONG,SHORT} price>0; exactly one RELEASE
    qty=1; opposite leg EXIT qty=1; quantities reconcile; no COMBINED_EXIT
    type; no TEST contamination.
    """
    trades: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        tid = f.get("trade_id")
        if tid:
            trades[str(tid)].append(f)

    candidates = []
    rejected = Counter()
    for tid, fs in trades.items():
        types = {str(f.get("fill_type") or "") for f in fs}
        if types & COMBINED_EXIT_TYPES:
            rejected["COMBINED_EXIT"] += 1
            continue
        if any(str(f.get("fill_type") or "") == "TEST" for f in fs):
            rejected["TEST_TRADE_CONTAMINATION"] += 1
            continue
        by_leg: dict[str, dict] = {}
        for f in fs:
            leg = str(f.get("leg") or "").upper()
            by_leg.setdefault(leg, []).append(f)
        if set(by_leg) != {"NEAR", "FAR"}:
            rejected["INCOMPLETE_LEGS"] += 1
            continue
        entries = {leg: [f for f in fs if str(f.get("leg") or "").upper() == leg
                         and str(f.get("fill_type") or "") == "ENTRY"]
                   for leg in ("NEAR", "FAR")}
        releases = [f for f in fs if str(f.get("fill_type") or "") == "RELEASE"]
        exits = [f for f in fs if str(f.get("fill_type") or "") == "EXIT"]
        if len(releases) != 1 or len(exits) != 1:
            rejected["MULTI_EVENT"] += 1
            continue
        rel = releases[0]
        ex = exits[0]
        rel_leg = str(rel.get("leg") or "").upper()
        ex_leg = str(ex.get("leg") or "").upper()
        if rel_leg == ex_leg or rel_leg not in ("NEAR", "FAR") or ex_leg not in ("NEAR", "FAR"):
            rejected["BAD_LEG_PAIR"] += 1
            continue
        rel_qty = _int_qty(rel)
        ex_qty = _int_qty(ex)
        ok = True
        for leg in ("NEAR", "FAR"):
            ens = entries[leg]
            if len(ens) != 1:
                rejected["MULTI_EVENT"] += 1
                ok = False
                break
            en = ens[0]
            if _int_qty(en) != 1 or str(en.get("side") or "") not in ("LONG", "SHORT"):
                rejected["BAD_SIDE" if str(en.get("side") or "") not in ("LONG", "SHORT")
                        else "QTY_MISMATCH"] += 1
                ok = False
                break
            try:
                if float(en.get("price") or 0) <= 0:
                    rejected["BAD_PRICE"] += 1
                    ok = False
                    break
            except (TypeError, ValueError):
                rejected["BAD_PRICE"] += 1
                ok = False
                break
        if not ok:
            continue
        if str(rel.get("side") or "") not in ("BUY", "SELL") or \
           str(ex.get("side") or "") not in ("BUY", "SELL"):
            rejected["BAD_SIDE"] += 1
            continue
        if rel_qty != 1 or ex_qty != 1 or rel_qty != ex_qty:
            rejected["QTY_MISMATCH"] += 1
            continue
        en_near = next(f for f in entries["NEAR"])
        en_far = next(f for f in entries["FAR"])
        candidates.append({
            "trade_id": tid,
            "released_leg": rel_leg,
            "remaining_leg": ex_leg,
            "fills": fs,
            "entry_near": en_near, "entry_far": en_far,
            "release": rel, "exit": ex,
        })
    return candidates, dict(rejected)


def _int_qty(fill) -> int:
    try:
        return int(fill.get("qty") or 0)
    except (TypeError, ValueError):
        return 0


# ── parameter provenance (v6 §7) ───────────────────────────────────────────

def resolve_params(repo_root: Path) -> dict:
    """Provenance-gap-only baseline (codex A5): per-trade deployed-config
    mapping is NOT implemented in this read-only audit, so parameters are
    always PARAMETER_VERSION_UNKNOWN. The script NEVER reports eligibility
    (eligibility_consistent stays null) — see design v7 §7."""
    return {"param_source": "PARAMETER_VERSION_UNKNOWN",
            **PARAMS_CURRENT, "params_assumed_from_current": True}


# ── classification (v6 §5) ─────────────────────────────────────────────────

def _last_decision_event(events_by_trade: dict, trade_id: str, exit_dt) -> dict | None:
    """Last Policy J evaluation event for the trade before the exit fill ts."""
    best = None
    for e in events_by_trade.get(trade_id, []):
        ev = str(e.get("event") or "")
        if ev not in PER_TRADE_EVIDENCE_TYPES:
            continue
        dt, flag = parse_ts(e.get("ts"))
        if flag != "ok":
            continue
        if exit_dt is not None and dt is not None and dt > exit_dt:
            continue
        if best is None or dt > best[0]:
            best = (dt, ev, e)
    return best


def classify_trade(cand: dict, events_by_trade: dict, params: dict,
                   tz_clean: bool) -> dict:
    tid = cand["trade_id"]
    fills = cand["fills"]
    exit_fill = cand["exit"]
    exit_dt, exit_flag = parse_ts(exit_fill.get("timestamp"))

    # v6/codex: per-candidate timestamp semantics — any row with naive /
    # mixed / missing / unparseable ts → NOT_PROVABLE + eligibility=null,
    # before any aware-vs-naive comparison (which would crash).
    ts_flags = {
        "entry_near": parse_ts(cand["entry_near"].get("timestamp"))[1],
        "entry_far": parse_ts(cand["entry_far"].get("timestamp"))[1],
        "release": parse_ts(cand["release"].get("timestamp"))[1],
        "exit": exit_flag,
    }
    bad_ts = [f"{k}={v}" for k, v in ts_flags.items() if v != "ok"]
    if bad_ts:
        return {
            "trade_id": tid, "classification": "INSUFFICIENT_EVIDENCE",
            "attribution_strength": "NOT_PROVABLE",
            "eligibility_consistent": None,
            "source_limits": ["TS_%s" % flag.split("=")[1].upper() for flag in bad_ts],
        }
    # timestamp contract ENTRY_A <= ENTRY_B < RELEASE_A < EXIT_B
    order_ok = _temporal_contract(cand)
    if not order_ok:
        return {
            "trade_id": tid, "classification": "INSUFFICIENT_EVIDENCE",
            "attribution_strength": "NOT_PROVABLE",
            "eligibility_consistent": None,
            "source_limits": ["ORDER_VIOLATION"],
        }

    # trigger-named evidence does not exist in the schema (v6) → SUPPORTED=0
    # final-decision cause marker does not exist (v6) → CONTRADICTED=0
    # codex A3: eligibility requires a timestamped same-trade decision record
    # with BOTH durable_peak and current_net_twd at that moment; the RELEASE
    # row PnL is a release-time proxy and never establishes eligibility.
    record = _decision_eval_record(events_by_trade, tid, exit_dt)
    if record is None or params.get("param_source") == "PARAMETER_VERSION_UNKNOWN" \
            or not tz_clean:
        limits = []
        if record is None:
            limits.append("NO_DECISION_TIME_MARK")
        if params.get("param_source") == "PARAMETER_VERSION_UNKNOWN":
            limits.append("PARAMETER_VERSION_UNKNOWN")
        if not tz_clean:
            limits.append("TS_SEMANTICS_UNKNOWN")
        return {
            "trade_id": tid, "classification": "INSUFFICIENT_EVIDENCE",
            "attribution_strength": "NOT_PROVABLE",
            "eligibility_consistent": None,
            "decision_event": None,
            "source_limits": limits,
        }
    peak = float(record[1].get("durable_peak") or 0.0)
    net = float(record[1].get("current_net_twd") or 0.0)
    activation = float(params.get("activation_twd", 200))
    giveback = float(params.get("giveback_twd", 50))
    eligible = peak >= activation and net <= peak - giveback
    return {
        "trade_id": tid, "classification": "INSUFFICIENT_EVIDENCE",
        "attribution_strength": "INFERRED_ELIGIBLE" if eligible else "NOT_PROVABLE",
        "eligibility_consistent": eligible,
        "decision_event": {"type": record[1].get("event"),
                           "ts": record[0].isoformat(),
                           "durable_peak": peak,
                           "current_net_twd": net},
        "source_limits": ["NO_DECISION_PROVENANCE"],
    }


def _temporal_contract(cand: dict) -> bool:
    """All timestamps are verified tz-clean by the caller; compare safely."""
    en_ts, _ = parse_ts(cand["entry_near"].get("timestamp"))
    ef_ts, _ = parse_ts(cand["entry_far"].get("timestamp"))
    r_ts, _ = parse_ts(cand["release"].get("timestamp"))
    x_ts, _ = parse_ts(cand["exit"].get("timestamp"))
    if not all([en_ts, ef_ts, r_ts, x_ts]):
        return False
    return en_ts <= ef_ts < r_ts < x_ts


def _decision_eval_record(events_by_trade: dict, trade_id: str, exit_dt):
    """A timestamped same-trade decision/evaluation record carrying BOTH
    durable_peak AND current combined net (current_net_twd) at that moment
    (codex A3). RELEASE-row PnL is a release-time proxy — it is NOT evidence
    that the giveback condition held later in [RELEASE, EXIT] — so it never
    counts here. Returns (dt, event) or None."""
    best = None
    for e in events_by_trade.get(trade_id, []):
        if str(e.get("event") or "") not in PER_TRADE_EVIDENCE_TYPES:
            continue
        dt, flag = parse_ts(e.get("ts"))
        if flag != "ok":
            continue
        if exit_dt is not None and dt > exit_dt:
            continue
        if e.get("durable_peak") is None or e.get("current_net_twd") is None:
            continue  # incomplete record — cannot establish decision-time net
        if best is None or dt > best[0]:
            best = (dt, e)
    return best


# ── main ───────────────────────────────────────────────────────────────────

def build_artifact(fills_path: Path, events_path: Path, output_dir: Path,
                   repo_root: Optional[Path] = None) -> dict:
    repo_root = repo_root or _repo_root()
    try:
        fills, fills_meta = load_snapshot(fills_path)
    except SnapshotLoadError as e:
        _m = getattr(e, "meta", {})
        return {"status": "SNAPSHOT_MALFORMED", "generated_at": datetime.now().isoformat(),
                "detail": str(e),
                "input_path": str(fills_path),
                "input_sha256": _m.get("sha256"),
                "input_bytes": _m.get("bytes"),
                "parser_error": str(e),
                "script_commit_sha": _git_sha(repo_root)}
    try:
        events, events_meta = load_snapshot(events_path)
    except SnapshotLoadError as e:
        _m = getattr(e, "meta", {})
        return {"status": "SNAPSHOT_MALFORMED", "generated_at": datetime.now().isoformat(),
                "detail": str(e),
                "input_path": str(events_path),
                "input_sha256": _m.get("sha256"),
                "input_bytes": _m.get("bytes"),
                "parser_error": str(e),
                "script_commit_sha": _git_sha(repo_root)}

    fills_schema = validate_fills_schema(fills)
    if fills_schema:
        return {"status": "UNREADABLE", "generated_at": datetime.now().isoformat(),
                "schema_mismatch": {"fills": fills_schema},
                "script_commit_sha": _git_sha(repo_root)}
    events_schema = validate_events_schema(events)
    if events_schema and "reason" in events_schema:
        return {"status": "UNREADABLE", "generated_at": datetime.now().isoformat(),
                "schema_mismatch": {"events": events_schema},
                "script_commit_sha": _git_sha(repo_root)}

    params = resolve_params(repo_root)
    candidates, rejected = select_candidates(fills)

    events_by_trade: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        tid = e.get("trade_id")
        if tid:
            events_by_trade[str(tid)].append(e)

    tz_clean = (fills_meta["timestamp_offsets"]["naive"] == 0
                and fills_meta["timestamp_offsets"]["unparseable"] == 0
                and events_meta["timestamp_offsets"]["naive"] == 0
                and events_meta["timestamp_offsets"]["unparseable"] == 0)

    trades = []
    for cand in candidates:
        rec = classify_trade(cand, events_by_trade, params, tz_clean)
        rec.update({
            "released_leg": cand["released_leg"],
            "remaining_leg": cand["remaining_leg"],
            "entry_ts": cand["entry_near"].get("timestamp"),
            "release_ts": cand["release"].get("timestamp"),
            "exit_ts": cand["exit"].get("timestamp"),
            "entry_prices": {"near": cand["entry_near"].get("price"),
                             "far": cand["entry_far"].get("price")},
            "release_price": cand["release"].get("price"),
            "exit_price": cand["exit"].get("price"),
            "evidence_keys": ["FILLS"],
            "tick_availability": "NONE",
        })
        trades.append(rec)

    counts = Counter(rec["attribution_strength"] for rec in trades)
    reasons = []
    if not _has_trigger_named_event(events, {c["trade_id"] for c in candidates}):
        reasons.append("NO_TRIGGER_NAMED_EVENT_IN_SCHEMA")
    if not _has_final_cause_event(events, {c["trade_id"] for c in candidates}):
        reasons.append("NO_FINAL_DECISION_CAUSE_EVENT")

    test_rows = sum(1 for f in fills if str(f.get("fill_type") or "") == "TEST")
    test_contaminated = sum(1 for f in fills
                            if str(f.get("fill_type") or "") == "TEST" and f.get("trade_id"))

    artifact = {
        "status": "OK",
        "generated_at": datetime.now().isoformat(),
        "script_commit_sha": _git_sha(repo_root),
        "script_file_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        "git_dirty": _git_dirty(repo_root),
        "manifest": {
            "inputs": {
                "fills": {"sha256": fills_meta["sha256"], "bytes": fills_meta["bytes"],
                          "snapshot_read_ts": fills_meta["snapshot_read_ts"],
                          "source_schema": {
                              "keys": sorted(fills[0].keys()),
                              "observed_enum_by_fill_type": {
                                  ft: sorted({str(s) for s in sides})
                                  for ft, sides in _observed_sides(fills).items()},
                              "allowlist_ok": True,
                              "test_rows": test_rows,
                              "test_trade_contamination": test_contaminated,
                              "timestamp_offsets": dict(fills_meta["timestamp_offsets"])}},
                "events": {"sha256": events_meta["sha256"], "bytes": events_meta["bytes"],
                           "snapshot_read_ts": events_meta["snapshot_read_ts"],
                           "source_schema": events_schema}
            },
            "parser_assumptions": ["jsonl", "utf-8", "iso8601_timezone_aware",
                                   "naive_ts_mean_utc8", "out_log_prefix_never_evidence"],
            "params": params,
        },
        "summary": {
            "SUPPORTED": {"PROVEN": counts.get("PROVEN", 0)},
            "CONTRADICTED": 0,
            "INSUFFICIENT_EVIDENCE": {
                "INFERRED_ELIGIBLE": counts.get("INFERRED_ELIGIBLE", 0),
                "NOT_PROVABLE": counts.get("NOT_PROVABLE", 0)},
            "reasons": reasons,
            "provenance_gap_only": True,   # codex A5: params never resolved
        },
        "candidates_considered": len(candidates),
        "rejected_candidates": dict(rejected),
        "trades": trades,
        "statistics_only": {
            "exit_log_count": sum(1 for e in events if str(e.get("event") or "") == "EXIT_LOG"),
            "peak_confirmed_total": sum(1 for e in events
                                        if str(e.get("event") or "") == "POLICY_J_PEAK_CONFIRMED"),
            "trigger_suppressed_total": sum(1 for e in events
                                            if str(e.get("event") or "") == "POLICY_J_TRIGGER_SUPPRESSED"),
        },
        "safety_note": "runtime/exports gitignore is a file-management convention, "
                       "NOT a security boundary; this artifact is externally readable.",
    }
    return artifact


def _observed_sides(fills: list[dict]) -> dict:
    out = defaultdict(set)
    for f in fills:
        out[str(f.get("fill_type") or "")].add(f.get("side"))
    return dict(out)


def _git_sha(repo_root: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return "unknown"


def _repo_root() -> Path:
    """The script's own repository root (audit.py lives at
    <repo>/scripts/research/pj_single_leg_attribution/audit.py)."""
    return Path(__file__).resolve().parents[3]


def _has_trigger_named_event(events: list[dict], candidate_trade_ids: set) -> bool:
    """Only explicit trigger/winner event types WITH a nonempty matching
    trade_id IN the audited candidate set count as trigger provenance
    (round-4 P0: an unrelated P1-B trigger event must not clear the gap for
    an old candidate set). TRIGGER_SUPPRESSED is NOT a trigger decision
    (per-tick suppression log) and never counts; unknown *TRIGGER* names
    never count either."""
    for e in events:
        ev = str(e.get("event") or "")
        if ev in TRIGGER_NAMED_EVENTS and e.get("trade_id") in candidate_trade_ids:
            return True
    return False


def _has_final_cause_event(events: list[dict], candidate_trade_ids: set) -> bool:
    """Only an explicit final-decision event type WITH trade_id and an
    allowed non-Policy-J cause counts, and the trade_id must be one of the
    audited candidates. Global/unrelated rows (no trade_id, non-candidate
    trade, or not a final-decision type) never clear the provenance gap —
    historical data stays no-cause by design."""
    for e in events:
        if str(e.get("event") or "") not in FINAL_DECISION_EVENT_TYPES:
            continue
        if e.get("trade_id") not in candidate_trade_ids:
            continue
        cause = e.get("cause")
        if isinstance(cause, str) and cause.lower() in NON_POLICY_J_CAUSES:
            return True
        reason = e.get("reason")
        if isinstance(reason, str) and any(
            kw in reason.lower() for kw in NON_POLICY_J_CAUSES
        ):
            return True
    return False


def _git_dirty(repo_root: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(repo_root), "status", "--porcelain"],
                             capture_output=True, text=True, timeout=10).stdout
        return out.strip() or "clean"
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--runtime", default=os.environ.get("TRADING_RUNTIME_DIR", DEFAULT_RUNTIME))
    ap.add_argument("--repo-root", default=None)
    args = ap.parse_args()

    runtime = Path(args.runtime)
    fills_path = runtime / "logs" / "mts_trade_fills.jsonl"
    events_path = runtime / "logs" / "mts_spread_events.jsonl"
    if not fills_path.exists() or not events_path.exists():
        print(f"inputs missing: {fills_path} / {events_path}", file=sys.stderr)
        return 2
    output_dir = Path(args.output_dir) if args.output_dir else runtime / "exports" / "research"
    output_dir.mkdir(parents=True, exist_ok=True)
    repo_root = Path(args.repo_root) if args.repo_root else _repo_root()

    artifact = build_artifact(fills_path, events_path, output_dir, repo_root)
    out_path = output_dir / f"pj_single_leg_attribution_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str))
    print(json.dumps({"status": artifact["status"], "artifact": str(out_path),
                      "summary": artifact.get("summary", {})}, ensure_ascii=False))
    return 0 if artifact["status"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
