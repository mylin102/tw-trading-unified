"""Non-blocking SQLite shadow store for MTS entry research.

This module is deliberately outside the order/gateway authority.  It records
what the strategy knew at an entry decision so policies A-D can be compared
offline later.  A database failure is telemetry loss only: callers must never
use it as a trading gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from core.runtime_paths import runtime_path


SCHEMA_VERSION = 1
DEFAULT_DB_NAME = "mts_entry_research.sqlite3"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entry_observations (
    event_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    event_time TEXT NOT NULL,
    mode TEXT,
    session_id TEXT,
    config_hash TEXT,
    release_sha TEXT,
    run_id TEXT,
    source TEXT NOT NULL,
    near_contract TEXT,
    far_contract TEXT,
    spread REAL,
    rolling_mean REAL,
    rolling_std REAL,
    spread_z REAL,
    dz REAL,
    spread_slope REAL,
    velocity_ema REAL,
    near_bid REAL,
    near_ask REAL,
    far_bid REAL,
    far_ask REAL,
    quote_age_ms REAL,
    pair_skew_ms REAL,
    entry_z_threshold REAL,
    atr REAL,
    regime TEXT,
    gross_expected_reversion REAL,
    estimated_total_cost REAL,
    expected_net_edge REAL,
    candidate_direction TEXT,
    decision TEXT NOT NULL,
    rejection_reason TEXT,
    actual_fill_prices_json TEXT,
    mfe REAL,
    mae REAL,
    final_net_pnl REAL,
    payload_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entry_observations_event_time
    ON entry_observations(event_time);
CREATE INDEX IF NOT EXISTS idx_entry_observations_mode
    ON entry_observations(mode);
CREATE INDEX IF NOT EXISTS idx_entry_observations_direction
    ON entry_observations(candidate_direction);
"""


def _finite_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _db_path(path: str | os.PathLike[str] | None = None) -> Path:
    configured = path or os.environ.get("MTS_ENTRY_RESEARCH_DB")
    return Path(configured or runtime_path("exports", "research", DEFAULT_DB_NAME))


def _event_id(audit: Mapping[str, Any], *, mode: str | None, session_id: str | None) -> str:
    material = {
        "schema_version": SCHEMA_VERSION,
        "event_time": audit.get("event_time") or audit.get("ts"),
        "trade_id": audit.get("trade_id"),
        "action": audit.get("action"),
        "near_price": audit.get("near_price"),
        "far_price": audit.get("far_price"),
        "spread_z": audit.get("spread_z"),
        "decision": audit.get("decision"),
        "mode": mode,
        "session_id": session_id,
    }
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_entry_observation(
    audit: Mapping[str, Any],
    *,
    mode: str | None = None,
    session_id: str | None = None,
    config_hash: str | None = None,
    release_sha: str | None = None,
    run_id: str | None = None,
    source: str = "entry_audit",
    db_path: str | os.PathLike[str] | None = None,
) -> bool:
    """Best-effort append of one entry decision; never raises to a caller."""
    try:
        path = _db_path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=0.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=0")
            conn.executescript(_SCHEMA)
            payload = dict(audit)
            event_time = str(payload.get("event_time") or payload.get("ts") or "")
            event_id = _event_id(payload, mode=mode, session_id=session_id)
            near = payload.get("near") if isinstance(payload.get("near"), Mapping) else {}
            far = payload.get("far") if isinstance(payload.get("far"), Mapping) else {}
            conn.execute(
                """INSERT OR IGNORE INTO entry_observations (
                    event_id, schema_version, event_time, mode, session_id,
                    config_hash, release_sha, run_id, source, near_contract,
                    far_contract, spread, rolling_mean, rolling_std, spread_z,
                    dz, spread_slope, velocity_ema, near_bid, near_ask,
                    far_bid, far_ask, quote_age_ms, pair_skew_ms,
                    entry_z_threshold, atr, regime, gross_expected_reversion,
                    estimated_total_cost, expected_net_edge, candidate_direction,
                    decision, rejection_reason, actual_fill_prices_json, mfe,
                    mae, final_net_pnl, payload_json, created_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, SCHEMA_VERSION, event_time, mode, session_id,
                    config_hash, release_sha, run_id, source,
                    payload.get("near_contract") or near.get("symbol"),
                    payload.get("far_contract") or far.get("symbol"),
                    _finite_or_none(payload.get("spread")),
                    _finite_or_none(payload.get("spread_ma", payload.get("spread_mean"))),
                    _finite_or_none(payload.get("spread_std")),
                    _finite_or_none(payload.get("spread_z")),
                    _finite_or_none(payload.get("dz")),
                    _finite_or_none(payload.get("spread_slope")),
                    _finite_or_none(payload.get("velocity_ema")),
                    _finite_or_none(payload.get("near_bid", near.get("bid"))),
                    _finite_or_none(payload.get("near_ask", near.get("ask"))),
                    _finite_or_none(payload.get("far_bid", far.get("bid"))),
                    _finite_or_none(payload.get("far_ask", far.get("ask"))),
                    _finite_or_none(payload.get("quote_age_ms")),
                    _finite_or_none(payload.get("pair_skew_ms")),
                    _finite_or_none(payload.get("entry_z", payload.get("entry_z_threshold"))),
                    _finite_or_none(payload.get("atr")),
                    payload.get("regime"),
                    _finite_or_none(payload.get("gross_expected_reversion")),
                    _finite_or_none(payload.get("estimated_total_cost")),
                    _finite_or_none(payload.get("expected_net_edge")),
                    payload.get("action") or payload.get("candidate_direction"),
                    payload.get("decision") or "ENTER",
                    payload.get("rejection_reason"),
                    json.dumps(payload.get("actual_fill_prices"), sort_keys=True, default=str)
                    if payload.get("actual_fill_prices") is not None else None,
                    _finite_or_none(payload.get("mfe")),
                    _finite_or_none(payload.get("mae")),
                    _finite_or_none(payload.get("final_net_pnl")),
                    json.dumps(payload, sort_keys=True, default=str),
                    int(time.time() * 1000),
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        # Shadow research must never block, reject, or alter an order.
        return False
