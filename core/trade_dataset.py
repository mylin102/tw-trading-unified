"""
trade_dataset.py — Experimental Trade Dataset (v0.2)

Architecture:
  JSONL (audit log) → deterministic rebuild → Parquet (analytic view)
  Never the other direction.

Generation model:
  data/generations/<build_id>/
    trade_facts.parquet
    trade_snapshots.parquet
    trade_decisions.parquet
    trade_outcomes.parquet
    manifest.json
  data/current -> generations/<build_id>/  (atomic symlink)

Guarantees:
  - Semantic idempotency: same JSONL → same DataFrame content
  - Atomic generation: all 4 tables + manifest from one build
  - Immutable generations: old builds archived, never overwritten
  - Validated output: invariants checked before symlink publish

MFE/MAE sign convention:
  MFE >= 0  (maximum favorable excursion, in points)
  MAE <= 0  (maximum adverse excursion, in points)
  Higher MFE is better. More negative MAE is worse.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema version — bump minor for backward-compatible additions,
# bump major for breaking changes (renames, type changes, column removal)
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.2.0"

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(os.getenv("TRADE_DATASET_DIR", "data"))
FILLS_LOG = Path(os.getenv("MTS_FILL_LOG_PATH", "logs/mts_trade_fills.jsonl"))
EVENTS_LOG = Path(os.getenv("MTS_EVENT_LOG_PATH", "logs/mts_spread_events.jsonl"))

GENERATIONS_DIR = BASE_DIR / "generations"
CURRENT_SYMLINK = BASE_DIR / "current"


# ---------------------------------------------------------------------------
# 1. Layer 1: Facts — one row per trade (immutable)
# ---------------------------------------------------------------------------
# Primary key: trade_id
# Grain: one spread trade = one row (NEAR + FAR legs)

FACTS_COLUMNS = {
    "trade_id": "str",
    "dataset_build_id": "str",
    "schema_version": "str",
    "timestamp": "datetime64[ms]",              # entry timestamp
    "session": "str",                            # day / night
    "direction": "str",                          # SELL_NEAR_BUY_FAR or BUY_NEAR_SELL_FAR
    "trade_status": "str",                       # CLOSED (all exits filled) / OPEN (still has position) / PARTIAL (one leg released, other open)
    "near_contract": "str",                      # e.g. TMFG6
    "far_contract": "str",                       # e.g. TMFH6
    "near_entry_price": "float64",
    "far_entry_price": "float64",
    "near_exit_price": "float64",
    "far_exit_price": "float64",
    "entry_spread": "float64",                   # near_price - far_price at entry
    "release_leg": "str",                        # NEAR or FAR (which leg was released first)
    "release_price": "float64",                  # exit price of released leg
    "pnl_total": "float64",                      # sum of all realized PnL (TWD)
    "slippage_near": "float64",                  # pts (for margin impact analysis)
    "slippage_far": "float64",                   # pts
    "data_quality": "str",                       # ok | snapshot_incomplete | stale_quote | tick_gap
    "data_quality_detail": "str",                # free-text description of any issue
}

# ---------------------------------------------------------------------------
# 2. Snapshots — indicator values at each decision point (immutable)
# ---------------------------------------------------------------------------
# Primary key: (trade_id, snapshot_seq)
# Grain: one row per decision point per trade

SNAPSHOTS_COLUMNS = {
    "trade_id": "str",
    "dataset_build_id": "str",
    "schema_version": "str",
    "snapshot_seq": "int64",                     # 0-based monotonic within trade
    "snapshot_type": "str",                      # ENTRY | RELEASE | EXIT
    "timestamp": "datetime64[ms]",
    # Indicator snapshots (what the system saw at this moment)
    "z_score": "float64",
    "atr": "float64",
    "vwap_dist": "float64",
    "bb_position": "str",                       # upper / middle / lower / outside
    "bb_width": "float64",
    "sqz_on": "bool",
    "regime": "str",
    # Market state at this moment
    "price_near": "float64",
    "price_far": "float64",
    "spread": "float64",
    "spread_mean": "float64",
    "spread_std": "float64",
    # Provenance
    "snapshot_source": "str",                   # live | reconstructed
    "is_decision_point": "bool",               # True if this snapshot corresponds to a decision
}

# ---------------------------------------------------------------------------
# 3. Decisions — decision attribution log (variable-length per trade)
# ---------------------------------------------------------------------------
# Primary key: (trade_id, decision_seq)
# Grain: one row per discrete decision

DECISIONS_COLUMNS = {
    "trade_id": "str",
    "dataset_build_id": "str",
    "schema_version": "str",
    "decision_seq": "int64",                     # 0-based, monotonic within trade
    "decision_type": "str",                      # ENTRY | RELEASE_NEAR | RELEASE_FAR | EXIT_NEAR | EXIT_FAR | PROFIT_LOCK
    "reason": "str",                             # e.g. TMF_SPREAD_WIDE | RELEASE_STOP | TRAIL
    "params_json": "str",                        # JSON dict of relevant parameters at decision time
    "timestamp": "datetime64[ms]",
}

# ---------------------------------------------------------------------------
# 4. Outcomes — computed after trade close
# ---------------------------------------------------------------------------
# Primary key: trade_id (1:1 with facts)
# MFE/MAE sign convention: MFE >= 0, MAE <= 0

OUTCOMES_COLUMNS = {
    "trade_id": "str",
    "dataset_build_id": "str",
    "schema_version": "str",
    # MFE / MAE (in points)
    "mfe_released_leg": "float64",               # MFE of released leg post-release
    "mae_released_leg": "float64",               # MAE of released leg post-release
    "mfe_remaining_leg": "float64",              # MFE of remaining leg post-release
    "mae_remaining_leg": "float64",              # MAE of remaining leg post-release
    "mfe_combined": "float64",                   # Overall best-case excursion
    "mae_combined": "float64",                   # Overall worst-case excursion
    # Timing
    "holding_time_s": "float64",                 # seconds from entry to last exit
    "release_delay_s": "float64",                # seconds from entry to first release
    # Trail metrics
    "trail_distance": "float64",                 # pts between peak and trail exit
    "release_reason": "str",                     # reason for the release decision (e.g. RELEASE_STOP_ATR_DYNAMIC)
    "released_leg": "str",                       # NEAR or FAR
    "final_exit_reason": "str",                  # reason for the final (remaining leg) exit (e.g. TRAIL)
    "risk_mode": "str",                          # FIXED_FALLBACK | ATR_DYNAMIC
    "realized_pnl_total": "float64",             # TWD (duplicated from facts for convenience)
    # 2026-07-23 Gemini CLI: Work Package A & B - Instrumentation & Provenance
    "single_leg_peak_or_nadir": "float64",       # peak/nadir of remaining leg during single leg phase
    "effective_trail_dist": "float64",           # final effective trail distance used
    "calculated_retracement": "float64",         # exact retracement from peak/nadir at exit
    "trigger_price": "float64",                  # exit trigger price
    "warmup_elapsed_ms": "float64",              # warmup elapsed milliseconds at exit
    "warmup_tick_count": "int64",                # warmup tick count at exit
    "release_stop_mode": "str",                  # ATR_DYNAMIC | FIXED_FALLBACK for release
    "trail_distance_mode": "str",                # ATR_DYNAMIC | FIXED_FALLBACK for trail
    "risk_mode_at_entry": "str",                 # risk mode snapshot at trade entry
    "risk_mode_at_release": "str",               # risk mode snapshot at leg release
    "risk_mode_at_single_leg": "str",            # risk mode snapshot at single leg start
    "risk_mode_at_exit": "str",                  # risk mode snapshot at final exit
    "risk_mode_transition_count": "int64",       # number of risk mode transitions during trade
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode("ascii").strip()
    except Exception:
        return "unknown"


def _file_sha256(path: Path) -> str:
    """Compute SHA-256 of a file, reading in 64KB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _make_build_id() -> str:
    """UTC timestamp based build ID: YYYYMMDDTHHMMSSZ"""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_fills(fills_path: Path) -> dict[str, list[dict]]:
    """Parse fills JSONL → dict[trade_id, list[fill records]]."""
    trades: dict[str, list[dict]] = defaultdict(list)
    if not fills_path.exists():
        logger.warning("Fills log not found: %s", fills_path)
        return trades
    with open(fills_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                tid = rec.get("trade_id", "")
                if tid:
                    trades[tid].append(rec)
            except json.JSONDecodeError:
                logger.warning("Skipping unparseable fill line: %s", line[:80])
    return trades


def _parse_events(events_path: Path) -> list[dict]:
    events = []
    if not events_path.exists():
        logger.warning("Events log not found: %s", events_path)
        return events
    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping unparseable event line: %s", line[:80])
    return events


def _correlate_events_to_trade(
    events: list[dict],
    trade_id: str,
    trade_timestamps: list[str],
    max_window_ms: int = 5000,
) -> list[dict]:
    """
    Correlate events lacking trade_id (ENTRY_AUDIT, EXIT_LOG) to a trade
    by finding events whose 'ts' falls within max_window_ms of any fill
    record for this trade. Also returns events that already have the matching trade_id.
    """
    if not trade_timestamps:
        return []
    trade_times = sorted(
        datetime.fromisoformat(ts) for ts in trade_timestamps if ts
    )
    window = __import__("datetime").timedelta(milliseconds=max_window_ms)
    matched = []
    for ev in events:
        if ev.get("trade_id") == trade_id:
            matched.append(ev)
            continue
        try:
            ev_ts = datetime.fromisoformat(ev.get("ts", ""))
        except (ValueError, TypeError):
            continue
        for t in trade_times:
            if abs(ev_ts - t) <= window:
                matched.append(ev)
                break
    return matched


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _build_trade_facts(
    trades: dict[str, list[dict]],
    build_id: str,
) -> pd.DataFrame:
    """Build trade_facts from fills. Deterministic: sorts by trade_id."""
    rows = []
    for trade_id in sorted(trades.keys()):
        fills = trades[trade_id]
        entries = [f for f in fills if f.get("fill_type") == "ENTRY"]
        releases = [f for f in fills if f.get("fill_type") == "RELEASE"]
        exits_all = [f for f in fills if f.get("fill_type") == "EXIT"]

        near_entry = next((f for f in entries if f.get("leg") == "NEAR"), None)
        far_entry = next((f for f in entries if f.get("leg") == "FAR"), None)
        release = releases[0] if releases else None
        near_exit = next((f for f in exits_all if f.get("leg") == "NEAR"), None)
        far_exit = next((f for f in exits_all if f.get("leg") == "FAR"), None)

        # Trade status: CLOSED = both legs disposed (release + exit fills present)
        # PARTIAL = one leg released, other still open
        # OPEN = entered but not yet released
        has_release = release is not None
        has_exit = len(exits_all) >= 1
        if has_release and has_exit:
            trade_status = "CLOSED"
        elif has_release:
            trade_status = "PARTIAL"
        else:
            trade_status = "OPEN"

        if not near_entry or not far_entry:
            logger.debug("Trade %s: missing entry fills, skipping", trade_id)
            continue

        timestamp = entries[0].get("timestamp", "")
        session = entries[0].get("session", "unknown")

        # Determine direction
        near_side = (near_entry.get("side") or "").upper()
        if near_side == "SHORT" or near_side == "SELL":
            direction = "SELL_NEAR_BUY_FAR"
        elif near_side == "LONG" or near_side == "BUY":
            direction = "BUY_NEAR_SELL_FAR"
        else:
            direction = "UNKNOWN"

        near_contract = near_entry.get("contract", "")
        far_contract = far_entry.get("contract", "")

        # Entry spread = near - far price
        entry_spread = (near_entry["price"] - far_entry["price"]) if near_entry and far_entry else None

        # Release info
        release_leg = release.get("leg") if release else None
        release_price = release.get("price") if release else None

        # PnL
        pnl_total = sum(f.get("realized_pnl") or 0.0 for f in fills)

        row = {
            "trade_id": trade_id,
            "dataset_build_id": build_id,
            "schema_version": SCHEMA_VERSION,
            "timestamp": pd.Timestamp(timestamp) if timestamp else pd.NaT,
            "session": session,
            "direction": direction,
            "trade_status": trade_status,
            "near_contract": near_contract,
            "far_contract": far_contract,
            "near_entry_price": near_entry.get("price", 0.0),
            "far_entry_price": far_entry.get("price", 0.0),
            "near_exit_price": near_exit.get("price", 0.0) if near_exit else 0.0,
            "far_exit_price": far_exit.get("price", 0.0) if far_exit else 0.0,
            "entry_spread": round(entry_spread, 2) if entry_spread is not None else None,
            "release_leg": release_leg or "",
            "release_price": release_price or 0.0,
            "pnl_total": round(pnl_total, 2),
            "slippage_near": 0.0,
            "slippage_far": 0.0,
            "data_quality": "ok",
            "data_quality_detail": "",
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    _coerce_dtypes(df, FACTS_COLUMNS)
    return df


def _build_snapshots(
    trades: dict[str, list[dict]],
    events: list[dict],
    build_id: str,
) -> pd.DataFrame:
    """Build snapshots — deterministic sorted by (trade_id, snapshot_seq)."""
    rows = []
    for trade_id in sorted(trades.keys()):
        fills = trades[trade_id]
        timestamps = [f.get("timestamp", "") for f in fills if f.get("timestamp")]
        correlated = _correlate_events_to_trade(events, trade_id, timestamps)
        seq = 0

        # 1. ENTRY snapshot (from ENTRY_AUDIT)
        audit = next((e for e in correlated if e.get("event") == "ENTRY_AUDIT"), None)
        if audit:
            rows.append({
                "trade_id": trade_id,
                "dataset_build_id": build_id,
                "schema_version": SCHEMA_VERSION,
                "snapshot_seq": seq,
                "snapshot_type": "ENTRY",
                "timestamp": pd.Timestamp(audit.get("ts", "")),
                "z_score": audit.get("entry_z"),
                "atr": audit.get("atr"),
                "vwap_dist": None,
                "bb_position": None,
                "bb_width": None,
                "sqz_on": None,
                "regime": None,
                "price_near": audit.get("near_price"),
                "price_far": audit.get("far_price"),
                "spread": audit.get("spread_now"),
                "spread_mean": audit.get("spread_mean"),
                "spread_std": audit.get("spread_std"),
                "snapshot_source": "live",
                "is_decision_point": True,
            })
            seq += 1

        # 2. RELEASE snapshot (from RELEASE_*_SUBMITTED)
        release_events = sorted(
            [e for e in correlated if "SUBMITTED" in e.get("event", "") and "RELEASE" in e.get("event", "")],
            key=lambda e: e.get("ts", ""),
        )
        for rev in release_events:
            rows.append({
                "trade_id": trade_id,
                "dataset_build_id": build_id,
                "schema_version": SCHEMA_VERSION,
                "snapshot_seq": seq,
                "snapshot_type": f"RELEASE_{rev.get('released_leg', 'UNK')}",
                "timestamp": pd.Timestamp(rev.get("ts", "")),
                "z_score": None,
                "atr": rev.get("atr", 0.0) or 0.0,
                "vwap_dist": None,
                "bb_position": None,
                "bb_width": None,
                "sqz_on": None,
                "regime": None,
                "price_near": rev.get("exit_price") if rev.get("released_leg") == "NEAR" else None,
                "price_far": rev.get("exit_price") if rev.get("released_leg") == "FAR" else None,
                "spread": None,
                "spread_mean": None,
                "spread_std": None,
                "snapshot_source": "live",
                "is_decision_point": True,
            })
            seq += 1

        # 3. EXIT decision snapshots (from EXIT_REMAINING events)
        exit_remaining = sorted(
            [e for e in correlated if e.get("event") == "EXIT_REMAINING"],
            key=lambda e: e.get("ts", ""),
        )
        for rem in exit_remaining:
            rows.append({
                "trade_id": trade_id,
                "dataset_build_id": build_id,
                "schema_version": SCHEMA_VERSION,
                "snapshot_seq": seq,
                "snapshot_type": f"EXIT_{rem.get('remaining_leg', 'UNK')}",
                "timestamp": pd.Timestamp(rem.get("ts", "")),
                "z_score": None,
                "atr": rem.get("atr", 0.0) or 0.0,
                "vwap_dist": None,
                "bb_position": None,
                "bb_width": None,
                "sqz_on": None,
                "regime": None,
                "price_near": rem.get("exit_price") if rem.get("remaining_leg") == "NEAR" else None,
                "price_far": rem.get("exit_price") if rem.get("remaining_leg") == "FAR" else None,
                "spread": None,
                "spread_mean": None,
                "spread_std": None,
                "snapshot_source": "live",
                "is_decision_point": True,
            })
            seq += 1

        # 4. Observation snapshots (from EXIT_LOG events — not decision points)
        exit_logs = sorted(
            [e for e in correlated if e.get("event") == "EXIT_LOG"],
            key=lambda e: e.get("ts", ""),
        )
        for el in exit_logs:
            rows.append({
                "trade_id": trade_id,
                "dataset_build_id": build_id,
                "schema_version": SCHEMA_VERSION,
                "snapshot_seq": seq,
                "snapshot_type": f"OBSERVE_{el.get('exit_reason', 'UNK')}",
                "timestamp": pd.Timestamp(el.get("ts", "")),
                "z_score": None,
                "atr": el.get("atr", 0.0) or 0.0,
                "vwap_dist": None,
                "bb_position": None,
                "bb_width": None,
                "sqz_on": None,
                "regime": None,
                "price_near": None,
                "price_far": None,
                "spread": None,
                "spread_mean": None,
                "spread_std": None,
                "snapshot_source": "live",
                "is_decision_point": False,
            })
            seq += 1

    df = pd.DataFrame(rows)
    _coerce_dtypes(df, SNAPSHOTS_COLUMNS)
    return df


def _build_decisions(
    trades: dict[str, list[dict]],
    events: list[dict],
    build_id: str,
) -> pd.DataFrame:
    """Build decision log — deterministic sorted by (trade_id, decision_seq)."""
    rows = []
    for trade_id in sorted(trades.keys()):
        fills = trades[trade_id]
        timestamps = [f.get("timestamp", "") for f in fills if f.get("timestamp")]
        correlated = _correlate_events_to_trade(events, trade_id, timestamps)
        seq = 0

        # 1. ENTRY decision
        audit = next((e for e in correlated if e.get("event") == "ENTRY_AUDIT"), None)
        if audit:
            rows.append({
                "trade_id": trade_id,
                "dataset_build_id": build_id,
                "schema_version": SCHEMA_VERSION,
                "decision_seq": seq,
                "decision_type": "ENTRY",
                "reason": audit.get("reason", ""),
                "params_json": json.dumps({
                    "entry_z": audit.get("entry_z"),
                    "spread_z": audit.get("spread_z"),
                    "expected_reversion": audit.get("expected_reversion"),
                    "near_side": audit.get("near_side"),
                    "far_side": audit.get("far_side"),
                }, default=str),
                "timestamp": pd.Timestamp(audit.get("ts", "")),
            })
            seq += 1

        # 2. RELEASE decisions (sorted by ts)
        release_events = sorted(
            [e for e in correlated
             if e.get("event") in ("RELEASE_FAR_SUBMITTED", "RELEASE_NEAR_SUBMITTED")],
            key=lambda e: e.get("ts", ""),
        )
        for rev in release_events:
            rel_leg = rev.get("released_leg", "UNK")
            rows.append({
                "trade_id": trade_id,
                "dataset_build_id": build_id,
                "schema_version": SCHEMA_VERSION,
                "decision_seq": seq,
                "decision_type": f"RELEASE_{rel_leg}",
                "reason": f"RELEASE_STOP_{rev.get('risk_mode', 'UNK')}",
                "params_json": json.dumps({
                    "risk_mode": rev.get("risk_mode"),
                    "atr": rev.get("atr"),
                    "stop_mult": rev.get("stop_mult"),
                    "trail_mult": rev.get("trail_mult"),
                    "release_stop": rev.get("release_stop"),
                    "gross_points": rev.get("gross_points"),
                    "cost": rev.get("cost"),
                }, default=str),
                "timestamp": pd.Timestamp(rev.get("ts", "")),
            })
            seq += 1

        # 3. EXIT decisions (sorted by ts)
        remaining = sorted(
            [e for e in correlated if e.get("event") == "EXIT_REMAINING"],
            key=lambda e: e.get("ts", ""),
        )
        for rem in remaining:
            rows.append({
                "trade_id": trade_id,
                "dataset_build_id": build_id,
                "schema_version": SCHEMA_VERSION,
                "decision_seq": seq,
                "decision_type": f"EXIT_{rem.get('remaining_leg', 'UNK')}",
                "reason": rem.get("reason", ""),
                "params_json": json.dumps({
                    "risk_mode": rem.get("risk_mode"),
                    "atr": rem.get("atr"),
                    "stop_mult": rem.get("stop_mult"),
                    "trail_mult": rem.get("trail_mult"),
                    "release_stop": rem.get("release_stop"),
                    "exit_price": rem.get("exit_price"),
                    "gross_points": rem.get("gross_points"),
                    "cost": rem.get("cost"),
                }, default=str),
                "timestamp": pd.Timestamp(rem.get("ts", "")),
            })
            seq += 1

    df = pd.DataFrame(rows)
    _coerce_dtypes(df, DECISIONS_COLUMNS)
    return df


def _build_outcomes(
    trades: dict[str, list[dict]],
    events: list[dict],
    build_id: str,
) -> pd.DataFrame:
    """Build outcomes — deterministic sorted by trade_id.
    MFE/MAE sign convention: MFE >= 0, MAE <= 0.
    """
    rows = []
    for trade_id in sorted(trades.keys()):
        fills = trades[trade_id]
        timestamps = [f.get("timestamp", "") for f in fills if f.get("timestamp")]
        correlated = _correlate_events_to_trade(events, trade_id, timestamps)

        # Entry timestamp
        entries = [f for f in fills if f.get("fill_type") == "ENTRY"]
        if not entries:
            continue

        # Skip open trades (no EXIT fills) — outcomes require complete lifecycle
        exit_fills = [f for f in fills if f.get("fill_type") == "EXIT"]
        if not exit_fills:
            continue

        # Parse entry timestamp
        entry_ts = entries[0].get("timestamp", "")
        try:
            entry_dt = datetime.fromisoformat(entry_ts)
        except (ValueError, TypeError):
            entry_dt = None

        # Exit timestamp (last fill)
        all_fills_sorted = sorted(fills, key=lambda f: f.get("timestamp", ""))
        last_fill = all_fills_sorted[-1] if all_fills_sorted else None
        exit_ts = last_fill.get("timestamp", "") if last_fill else ""
        try:
            exit_dt = datetime.fromisoformat(exit_ts) if exit_ts else None
        except (ValueError, TypeError):
            exit_dt = None

        holding_time_s = None
        if entry_dt and exit_dt:
            holding_time_s = (exit_dt - entry_dt).total_seconds()

        # Release delay
        releases = [f for f in fills if f.get("fill_type") == "RELEASE"]
        release_ts = releases[0].get("timestamp", "") if releases else ""
        release_delay_s = None
        if entry_dt and release_ts:
            try:
                rd = datetime.fromisoformat(release_ts)
                release_delay_s = (rd - entry_dt).total_seconds()
            except (ValueError, TypeError):
                pass

        # MFE/MAE from EXIT_LOG events
        exit_logs = [e for e in correlated if e.get("event") == "EXIT_LOG"]
        mfe_values = [e.get("mfe", 0) or 0 for e in exit_logs]
        mae_values = [e.get("mae", 0) or 0 for e in exit_logs]

        # MFE/MAE from fills (per-leg)
        release_fills = [f for f in fills if f.get("fill_type") == "RELEASE"]
        exit_fills = [f for f in fills if f.get("fill_type") == "EXIT"]
        mfe_released = release_fills[0].get("leg_mfe") if release_fills else None
        mae_released = release_fills[0].get("leg_mae") if release_fills else None
        mfe_remaining = exit_fills[0].get("leg_mfe") if exit_fills else None
        mae_remaining = exit_fills[0].get("leg_mae") if exit_fills else None

        # Trail distance from TRAIL exit logs
        trail_logs = [e for e in exit_logs if e.get("exit_reason") == "TRAIL"]
        trail_distance = trail_logs[0].get("trail_dist") if trail_logs else None

        # MFE/MAE combined (points)
        mfe_combined = max(mfe_values) if mfe_values else 0.0
        mae_combined = min(mae_values) if mae_values else 0.0

        # Realized PnL total
        realized_pnl_total = sum(f.get("realized_pnl") or 0.0 for f in fills)

        # Release reason: from first RELEASE_SUBMITTED event
        release_submitted = [e for e in correlated
                             if e.get("event") in ("RELEASE_FAR_SUBMITTED", "RELEASE_NEAR_SUBMITTED")]
        release_submitted = sorted(release_submitted, key=lambda e: e.get("ts", ""))
        release_reason = release_submitted[0].get("risk_mode", "RELEASE_STOP") if release_submitted else ""
        released_leg = release_submitted[0].get("released_leg", "") if release_submitted else ""

        # Final exit reason: from last EXIT_LOG (remaining leg exit, usually TRAIL)
        if len(exit_logs) >= 2:
            final_exit_reason = exit_logs[-1].get("exit_reason", "")
        elif exit_logs:
            final_exit_reason = exit_logs[0].get("exit_reason", "")
        else:
            final_exit_reason = ""

        risk_mode = exit_logs[-1].get("risk_mode", "") if exit_logs else ""
        last_exit = exit_logs[-1] if exit_logs else {}

        row = {
            "trade_id": trade_id,
            "dataset_build_id": build_id,
            "schema_version": SCHEMA_VERSION,
            "mfe_released_leg": mfe_released,
            "mae_released_leg": mae_released,
            "mfe_remaining_leg": mfe_remaining,
            "mae_remaining_leg": mae_remaining,
            "mfe_combined": round(mfe_combined, 2) if mfe_values else None,
            "mae_combined": round(mae_combined, 2) if mae_values else None,
            "holding_time_s": round(holding_time_s, 3) if holding_time_s is not None else None,
            "release_delay_s": round(release_delay_s, 3) if release_delay_s is not None else None,
            "trail_distance": trail_distance,
            "release_reason": release_reason,
            "released_leg": released_leg,
            "final_exit_reason": final_exit_reason,
            "risk_mode": risk_mode,
            "realized_pnl_total": round(realized_pnl_total, 2),
            # Work Package A: Instrumentation
            "single_leg_peak_or_nadir": last_exit.get("single_leg_peak_or_nadir"),
            "effective_trail_dist": last_exit.get("effective_trail_dist") or trail_distance,
            "calculated_retracement": last_exit.get("calculated_retracement"),
            "trigger_price": last_exit.get("trigger_price") or last_exit.get("exit_price"),
            "warmup_elapsed_ms": last_exit.get("warmup_elapsed_ms"),
            "warmup_tick_count": last_exit.get("warmup_tick_count"),
            # Work Package B: Provenance & Separated Modes
            "release_stop_mode": last_exit.get("release_stop_mode") or (release_submitted[0].get("risk_mode") if release_submitted else None),
            "trail_distance_mode": last_exit.get("trail_distance_mode") or (trail_logs[0].get("risk_mode") if trail_logs else None),
            "risk_mode_at_entry": last_exit.get("risk_mode_at_entry"),
            "risk_mode_at_release": last_exit.get("risk_mode_at_release") or (release_submitted[0].get("risk_mode") if release_submitted else None),
            "risk_mode_at_single_leg": last_exit.get("risk_mode_at_single_leg"),
            "risk_mode_at_exit": last_exit.get("risk_mode_at_exit") or risk_mode,
            "risk_mode_transition_count": last_exit.get("risk_mode_transition_count", 0),
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    _coerce_dtypes(df, OUTCOMES_COLUMNS)
    return df


# ---------------------------------------------------------------------------
# Type coercion helper
# ---------------------------------------------------------------------------


def _coerce_dtypes(df: pd.DataFrame, schema: dict[str, str]):
    """Apply schema dtypes to DataFrame columns that exist."""
    for col, dtype in schema.items():
        if col in df.columns and not df.empty:
            try:
                df[col] = df[col].astype(dtype)
            except (ValueError, TypeError):
                pass


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class DatasetValidationError(ValueError):
    """Raised when dataset invariants are violated."""
    pass


def _canonical_content_hash(
    df_facts: pd.DataFrame,
    df_snapshots: pd.DataFrame,
    df_decisions: pd.DataFrame,
    df_outcomes: pd.DataFrame,
) -> str:
    """Compute a deterministic content hash over the canonical dataset.
    NOT a hash of parquet bytes — hashes the logical content.
    """
    h = hashlib.sha256()

    def _digest_frame(df: pd.DataFrame, label: str):
        if df.empty:
            h.update(f"{label}:empty\n".encode())
            return
        # Sort rows by primary key, drop build-specific metadata, write CSV to hash
        h.update(f"{label}:{len(df)}rows\n".encode())
        # Drop columns whose values change per build
        exclude = {"dataset_build_id", "schema_version"}
        content_cols = [c for c in df.columns if c not in exclude]
        csv_bytes = df[content_cols].to_csv(index=False).encode("utf-8")
        h.update(csv_bytes)

    _digest_frame(df_facts, "facts")
    _digest_frame(df_snapshots, "snapshots")
    _digest_frame(df_decisions, "decisions")
    _digest_frame(df_outcomes, "outcomes")
    return h.hexdigest()


def validate_dataset(
    df_facts: pd.DataFrame,
    df_snapshots: pd.DataFrame,
    df_decisions: pd.DataFrame,
    df_outcomes: pd.DataFrame,
) -> dict[str, Any]:
    """Run all schema invariants. Returns a quality report dict.
    Raises DatasetValidationError on hard violations.
    """
    quality: dict[str, Any] = {
        "facts_rows": len(df_facts),
        "snapshots_rows": len(df_snapshots),
        "decisions_rows": len(df_decisions),
        "outcomes_rows": len(df_outcomes),
        "errors": [],
        "warnings": [],
    }

    # --- trade_facts ---
    if not df_facts.empty:
        if df_facts["trade_id"].isna().any():
            quality["errors"].append("facts: trade_id has nulls")
        if not df_facts["trade_id"].is_unique:
            quality["errors"].append("facts: trade_id is not unique")
        if df_facts["timestamp"].isna().any():
            quality["errors"].append("facts: timestamp has nulls")
        # Count data quality issues
        quality["complete"] = int((df_facts["data_quality"] == "ok").sum())
        quality["partial"] = int((df_facts["data_quality"] == "snapshot_incomplete").sum())
        quality["invalid"] = int(len(df_facts) - quality["complete"] - quality["partial"])

    # --- trade_snapshots ---
    if not df_snapshots.empty:
        dup_snap = df_snapshots.duplicated(subset=["trade_id", "snapshot_seq"])
        if dup_snap.any():
            quality["errors"].append(
                f"snapshots: {dup_snap.sum()} duplicate (trade_id, snapshot_seq) keys"
            )
        if df_snapshots["snapshot_seq"].isna().any():
            quality["errors"].append("snapshots: snapshot_seq has nulls")

    # --- trade_decisions ---
    if not df_decisions.empty:
        dup_dec = df_decisions.duplicated(subset=["trade_id", "decision_seq"])
        if dup_dec.any():
            quality["errors"].append(
                f"decisions: {dup_dec.sum()} duplicate (trade_id, decision_seq) keys"
            )
        # Verify monotonic sequences
        for tid, grp in df_decisions.groupby("trade_id"):
            seqs = grp["decision_seq"].sort_values().values
            expected = list(range(len(seqs)))
            if list(seqs) != expected:
                quality["warnings"].append(
                    f"decisions: trade {tid} has non-monotonic decision_seq "
                    f"(expected 0..{len(seqs)-1}, got {list(seqs)})"
                )

    # --- Cross-table ---
    fact_ids = set(df_facts["trade_id"]) if not df_facts.empty else set()
    outcome_ids = set(df_outcomes["trade_id"]) if not df_outcomes.empty else set()

    missing_outcomes = fact_ids - outcome_ids
    if missing_outcomes:
        quality["warnings"].append(
            f"outcomes: {len(missing_outcomes)} trades in facts missing from outcomes"
        )

    # Decision-level view invariants: join must not expand rows
    if not df_decisions.empty:
        dv_len = len(df_decisions)
        # Simulate the join decision_level_view() does (only decision-point snapshots)
        if not df_snapshots.empty:
            decision_snaps = df_snapshots[df_snapshots["is_decision_point"] == True]
            snap_join = decision_snaps.rename(
                columns={"snapshot_seq": "decision_seq", "snapshot_type": "snap_type"}
            )
            join_cols = ["trade_id", "decision_seq"]
            merged = df_decisions.merge(
                snap_join[join_cols + ["snap_type"]],
                on=join_cols, how="left", validate="one_to_one",
            )
            joined_len = len(merged)
            if dv_len != joined_len:
                quality["warnings"].append(
                    f"decisions: decision merge with snapshots expanded "
                    f"(decisions={dv_len} → joined={joined_len}); "
                    f"non-unique (trade_id, snapshot_seq) in snapshots"
                )

    # MFE/MAE sign convention
    if not df_outcomes.empty:
        pos_mfe = (df_outcomes["mfe_combined"].dropna() >= 0).all()
        neg_mae = (df_outcomes["mae_combined"].dropna() <= 0).all()
        if not pos_mfe:
            quality["warnings"].append(
                "outcomes: some mfe_combined values are negative (violates MFE >= 0 convention)"
            )
        if not neg_mae:
            quality["warnings"].append(
                "outcomes: some mae_combined values are positive (violates MAE <= 0 convention)"
            )

    # Hard errors
    if quality["errors"]:
        raise DatasetValidationError(
            f"Dataset validation failed with {len(quality['errors'])} error(s):\n"
            + "\n".join(f"  - {e}" for e in quality["errors"])
        )

    return quality


# ---------------------------------------------------------------------------
# Generation directory management
# ---------------------------------------------------------------------------


def _write_generation(
    results: dict[str, pd.DataFrame],
    build_id: str,
    source_fingerprints: list[dict],
    output_dir: Path,
) -> Path:
    """
    Write a complete generation to a staging directory, then atomically
    publish by updating the 'current' symlink.
    """
    gen_dir = output_dir / "generations" / build_id
    gen_dir.mkdir(parents=True, exist_ok=True)

    # Write parquet files
    table_map = {
        "trade_facts": results.get("trade_facts", pd.DataFrame()),
        "trade_snapshots": results.get("trade_snapshots", pd.DataFrame()),
        "trade_decisions": results.get("trade_decisions", pd.DataFrame()),
        "trade_outcomes": results.get("trade_outcomes", pd.DataFrame()),
    }
    for name, df in table_map.items():
        path = gen_dir / f"{name}.parquet"
        if not df.empty:
            df.to_parquet(path, compression="snappy", index=False)
        else:
            # Write minimal schema-only parquet
            empty_df = pd.DataFrame()
            empty_df.to_parquet(path, compression="snappy", index=False)

    # Compute content hash
    content_hash = _canonical_content_hash(
        results.get("trade_facts", pd.DataFrame()),
        results.get("trade_snapshots", pd.DataFrame()),
        results.get("trade_decisions", pd.DataFrame()),
        results.get("trade_outcomes", pd.DataFrame()),
    )

    # Build manifest
    manifest = {
        "dataset_build_id": build_id,
        "schema_version": SCHEMA_VERSION,
        "builder_version": SCHEMA_VERSION,
        "git_commit": _get_git_commit(),
        "build_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_content_hash": content_hash,
        "source_files": source_fingerprints,
        "row_counts": {
            name: len(df) for name, df in table_map.items()
        },
    }

    # Add trade status counts if facts are available
    facts_df = results.get("trade_facts", pd.DataFrame())
    if not facts_df.empty and "trade_status" in facts_df.columns:
        status_counts = facts_df["trade_status"].value_counts().to_dict()
        manifest["trade_counts"] = {
            "all": len(facts_df),
            "closed": status_counts.get("CLOSED", 0),
            "open": status_counts.get("OPEN", 0),
            "partial": status_counts.get("PARTIAL", 0),
            "outcome_complete": len(results.get("trade_outcomes", pd.DataFrame())),
        }

    # Add timestamps range if facts exist
    if not results.get("trade_facts", pd.DataFrame()).empty:
        ts = results["trade_facts"]["timestamp"]
        manifest["min_timestamp"] = ts.min().isoformat() if pd.notna(ts.min()) else None
        manifest["max_timestamp"] = ts.max().isoformat() if pd.notna(ts.max()) else None

    manifest_path = gen_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, default=str)

    logger.info("  Generation written: %s", gen_dir)
    return gen_dir


def _publish_generation(gen_dir: Path, current_symlink: Path):
    """Atomically flip the 'current' symlink to point to the new generation."""
    tmp_link = current_symlink.with_name("current_tmp")
    try:
        # Remove any existing tmp link
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        # Create new symlink pointing to gen_dir (relative)
        rel = os.path.relpath(gen_dir, current_symlink.parent)
        tmp_link.symlink_to(rel)
        # Atomic rename (POSIX guarantee: rename is atomic on same filesystem)
        tmp_link.rename(current_symlink)
        logger.info("  Published generation: %s -> %s", current_symlink, gen_dir)
    except Exception:
        # Clean up tmp link on failure
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rebuild(
    fills_path: Optional[Path] = None,
    events_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    build_id: Optional[str] = None,
    publish: bool = True,
) -> dict[str, Any]:
    """
    Rebuild the trade dataset from JSONL audit logs.
    Validates, writes generation directory, and optionally publishes 'current' symlink.

    When publish=True (default): writes generation to output_dir/generations/<build_id>/
      and atomically flips the 'current' symlink.
    When publish=False: writes generation to output_dir/generations/<build_id>/
      but does NOT touch the 'current' symlink. Useful for staging/inspection.

    Returns generation metadata dict (manifest content).
    """
    fills_path = fills_path or FILLS_LOG
    events_path = events_path or EVENTS_LOG
    output_dir = output_dir or BASE_DIR
    build_id = build_id or _make_build_id()

    logger.info("Rebuilding trade dataset (schema %s)...", SCHEMA_VERSION)
    logger.info("  Build ID:  %s", build_id)
    logger.info("  Fills:     %s", fills_path)
    logger.info("  Events:    %s", events_path)
    logger.info("  Output:    %s", output_dir)

    # Fingerprint source files
    source_fingerprints = []
    for p in [fills_path, events_path]:
        if p.exists():
            source_fingerprints.append({
                "path": str(p),
                "size": p.stat().st_size,
                "sha256": _file_sha256(p),
            })
        else:
            source_fingerprints.append({
                "path": str(p),
                "size": None,
                "sha256": None,
            })

    # Parse
    trades = _parse_fills(fills_path)
    events = _parse_events(events_path)
    logger.info("  Parsed %d trades, %d events", len(trades), len(events))

    # Build each layer
    df_facts = _build_trade_facts(trades, build_id)
    df_snapshots = _build_snapshots(trades, events, build_id)
    df_decisions = _build_decisions(trades, events, build_id)
    df_outcomes = _build_outcomes(trades, events, build_id)

    logger.info("  Facts:     %d rows", len(df_facts))
    logger.info("  Snapshots: %d rows", len(df_snapshots))
    logger.info("  Decisions: %d rows", len(df_decisions))
    logger.info("  Outcomes:  %d rows", len(df_outcomes))

    # Validate
    quality = validate_dataset(df_facts, df_snapshots, df_decisions, df_outcomes)
    logger.info("  Quality: %d complete, %d partial, %d invalid",
                quality.get("complete", 0), quality.get("partial", 0), quality.get("invalid", 0))

    # Write generation
    results = {
        "trade_facts": df_facts,
        "trade_snapshots": df_snapshots,
        "trade_decisions": df_decisions,
        "trade_outcomes": df_outcomes,
    }
    gen_dir = _write_generation(results, build_id, source_fingerprints, output_dir)

    # Publish (if requested)
    if publish:
        current_symlink = output_dir / "current"
        _publish_generation(gen_dir, current_symlink)
    else:
        logger.info("  Generation written (no publish): %s", gen_dir)

    # Read back manifest for return
    manifest_path = gen_dir / "manifest.json"
    with open(manifest_path) as f:
        manifest = json.load(f)

    return manifest


# ---------------------------------------------------------------------------
# Load helpers
# ---------------------------------------------------------------------------


def load_dataset(
    path: Optional[Path] = None,
) -> dict[str, pd.DataFrame]:
    """
    Load the currently published dataset via the 'current' symlink.
    Falls back to direct path loading if 'current' does not exist.
    Returns dict of {table_name: DataFrame}.
    """
    path = path or BASE_DIR
    current = path / "current"
    if current.exists() and current.is_symlink():
        gen_dir = current.resolve()
    else:
        gen_dir = path

    result: dict[str, pd.DataFrame] = {}
    for table in ["trade_facts", "trade_snapshots", "trade_decisions", "trade_outcomes"]:
        p = gen_dir / f"{table}.parquet"
        if p.exists():
            result[table] = pd.read_parquet(p)
        else:
            result[table] = pd.DataFrame()
    return result


def load_manifest(path: Optional[Path] = None) -> dict[str, Any]:
    """Load the manifest of the currently published generation."""
    path = path or BASE_DIR
    current = path / "current"
    if current.exists() and current.is_symlink():
        gen_dir = current.resolve()
    else:
        gen_dir = path

    manifest_path = gen_dir / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path) as f:
            return json.load(f)
    return {}


def current_manifest() -> dict[str, Any]:
    """Alias for load_manifest(). Returns current dataset manifest."""
    return load_manifest()


def trade_level_view(
    path: Optional[Path] = None,
    closed_only: bool = True,
) -> pd.DataFrame:
    """
    One row per trade: facts + outcomes (1:1 merge).

    Parameters
    ----------
    closed_only : bool, default True
        If True, inner join (only closed trades with complete outcomes).
        If False, left join (all trades, outcome fields null for open/partial).
    """
    ds = load_dataset(path)
    facts = ds.get("trade_facts")
    outcomes = ds.get("trade_outcomes")
    if facts is None or facts.empty:
        return pd.DataFrame()
    how = "inner" if closed_only else "left"
    if outcomes is not None and not outcomes.empty:
        return facts.merge(outcomes, on="trade_id", how=how, validate="one_to_one",
                           suffixes=("", "_outcome"))
    return facts


def decision_level_view(path: Optional[Path] = None) -> pd.DataFrame:
    """
    One row per decision: decisions + snapshots + facts + outcomes.
    Use for: decision context analysis, counterfactual evaluation.
    """
    ds = load_dataset(path)
    decisions = ds.get("trade_decisions")
    snapshots = ds.get("trade_snapshots")
    facts = ds.get("trade_facts")
    outcomes = ds.get("trade_outcomes")

    if decisions is None or decisions.empty:
        return pd.DataFrame()

    # Merge decisions with snapshots on decision_seq <-> snapshot_seq
    # (they share the same 0-based sequence order within a trade)
    result = decisions.copy()
    if snapshots is not None and not snapshots.empty:
        # Only merge decision-point snapshots (observations like EXIT_LOG are excluded)
        decision_snapshots = snapshots[snapshots["is_decision_point"] == True].copy()
        # Map snapshot_seq -> decision_seq (same ordering for decision snapshots)
        snap_map = decision_snapshots.rename(columns={"snapshot_seq": "decision_seq",
                                                      "snapshot_type": "snap_type"})
        snap_cols = ["trade_id", "decision_seq", "snap_type",
                      "z_score", "atr", "bb_position", "bb_width",
                      "sqz_on", "regime", "price_near", "price_far",
                      "spread", "spread_mean", "spread_std"]
        existing_snap_cols = [c for c in snap_cols if c in snap_map.columns]
        result = result.merge(
            snap_map[existing_snap_cols],
            on=["trade_id", "decision_seq"],
            how="left",
            validate="one_to_one",
        )

    if facts is not None and not facts.empty:
        result = result.merge(
            facts[["trade_id", "session", "direction", "pnl_total", "release_leg"]],
            on="trade_id",
            how="left",
            validate="many_to_one",
        )
    if outcomes is not None and not outcomes.empty:
        result = result.merge(
            outcomes[["trade_id", "mfe_combined", "mae_combined",
                       "holding_time_s", "final_exit_reason", "release_reason",
                       "released_leg", "realized_pnl_total"]],
            on="trade_id",
            how="left",
            validate="many_to_one",
        )
    return result


def summary(path: Optional[Path] = None) -> str:
    """Human-readable summary of the currently published dataset."""
    path = path or BASE_DIR
    current = path / "current"
    manifest = load_manifest(path)
    lines = []

    if manifest:
        # Current generation info
        current_gen = current.resolve().name if current.is_symlink() else "?"
        lines.append(f"Trade Dataset (schema {manifest.get('schema_version', '?')})")
        lines.append(f"  Current generation: {current_gen}")
        lines.append(f"  Build ID:   {manifest.get('dataset_build_id', '?')}")
        lines.append(f"  Git commit: {manifest.get('git_commit', '?')}")
        lines.append(f"  Built:      {manifest.get('build_timestamp_utc', '?')}")
        lines.append(f"  Content:    {manifest.get('dataset_content_hash', '?')[:16]}...")
        lines.append("-" * 50)
        for tbl, cnt in manifest.get("row_counts", {}).items():
            lines.append(f"  {tbl}: {cnt} rows")

        # Trade status counts
        tc = manifest.get("trade_counts", {})
        if tc:
            lines.append(f"  Trades: {tc.get('all', '?')} total, "
                         f"{tc.get('closed', '?')} closed, "
                         f"{tc.get('open', '?')} open, "
                         f"{tc.get('outcome_complete', '?')} with outcomes")

        src = manifest.get("source_files", [])
        for s in src:
            if s.get("sha256"):
                lines.append(f"  Source: {s['path']} ({s['sha256'][:12]}...)")
        lines.append("")

        # Validation status
        ds = load_dataset(path)
        if ds.get("trade_facts") is not None and not ds["trade_facts"].empty:
            qc = ds["trade_facts"]["data_quality"].value_counts()
            lines.append("  Data quality:")
            for k, v in qc.items():
                lines.append(f"    {k}: {v}")

        # Quick validation: current gen matches manifest
        gen_match = current_gen == manifest.get("dataset_build_id", "")
        lines.append(f"  Validation: {'PASS' if gen_match else 'FAIL — symlink mismatch'}")
    else:
        lines.append("No published dataset found.")

    return "\n".join(lines)
