"""Read-only integrity report for the MTS entry research shadow database."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from core.runtime_paths import runtime_path


REQUIRED_COLUMNS = {
    "event_id", "schema_version", "event_time", "mode", "session_id",
    "config_hash", "release_sha", "run_id", "source", "near_contract",
    "far_contract", "spread_z", "dz", "spread_slope", "velocity_ema",
    "near_bid", "near_ask", "far_bid", "far_ask", "decision",
}
NUMERIC_COLUMNS = (
    "spread", "rolling_mean", "rolling_std", "spread_z", "dz",
    "spread_slope", "velocity_ema", "near_bid", "near_ask", "far_bid",
    "far_ask", "quote_age_ms", "pair_skew_ms", "entry_z_threshold", "atr",
    "gross_expected_reversion", "estimated_total_cost", "expected_net_edge",
    "mfe", "mae", "final_net_pnl",
)


def _default_db() -> Path:
    return Path(os.environ.get(
        "MTS_ENTRY_RESEARCH_DB",
        runtime_path("exports", "research", "mts_entry_research.sqlite3"),
    ))


def _default_report() -> Path:
    return Path(runtime_path("exports", "research", "entry_research_report.json"))


def _read_db(path: Path) -> dict[str, Any]:
    base = {
        "report_version": 1,
        "generated_at_ms": int(time.time() * 1000),
        "database": str(path),
        "database_exists": path.is_file(),
    }
    if not path.is_file():
        return {**base, "status": "NO_DATABASE", "reason": "NO_OBSERVATIONS_YET", "rows": 0}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.0)
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='entry_observations'"
            ).fetchone()
            if not table:
                return {**base, "status": "CORRUPT", "reason": "TABLE_MISSING", "rows": 0}
            columns = {row[1] for row in conn.execute("PRAGMA table_info(entry_observations)")}
            missing_columns = sorted(REQUIRED_COLUMNS - columns)
            if missing_columns:
                return {**base, "status": "CORRUPT", "reason": "SCHEMA_MISSING_COLUMNS",
                        "missing_columns": missing_columns, "rows": 0}
            rows = conn.execute(
                "SELECT * FROM entry_observations ORDER BY event_time, event_id"
            ).fetchall()
            names = [row[1] for row in conn.execute("PRAGMA table_info(entry_observations)")]
            records = [dict(zip(names, row)) for row in rows]
            missing = {name: 0 for name in sorted(REQUIRED_COLUMNS)}
            nonfinite = {name: 0 for name in NUMERIC_COLUMNS}
            source_mismatch = 0
            for record in records:
                for name in missing:
                    if record.get(name) in (None, ""):
                        missing[name] += 1
                if record.get("mode") == "live" and record.get("source") != "live_strategy":
                    source_mismatch += 1
                if record.get("mode") == "paper" and record.get("source") != "paper_strategy":
                    source_mismatch += 1
                for name in NUMERIC_COLUMNS:
                    value = record.get(name)
                    if value is not None:
                        try:
                            if not math.isfinite(float(value)):
                                nonfinite[name] += 1
                        except (TypeError, ValueError):
                            nonfinite[name] += 1
            decisions = dict(conn.execute(
                "SELECT decision, COUNT(*) FROM entry_observations GROUP BY decision"
            ).fetchall())
            modes = dict(conn.execute(
                "SELECT mode, COUNT(*) FROM entry_observations GROUP BY mode"
            ).fetchall())
            candidate_features = {
                name: sum(1 for record in records if record.get(name) not in (None, ""))
                for name in ("dz", "spread_slope", "velocity_ema", "near_bid", "near_ask", "far_bid", "far_ask", "estimated_total_cost", "expected_net_edge")
            }
            hard_errors = source_mismatch + sum(nonfinite.values())
            status = "READY_FOR_RESEARCH" if records and not hard_errors else (
                "INSUFFICIENT_EVIDENCE" if records else "NO_OBSERVATIONS_YET"
            )
            return {
                **base, "status": status, "rows": len(records), "modes": modes,
                "decisions": decisions, "event_time_min": records[0].get("event_time") if records else None,
                "event_time_max": records[-1].get("event_time") if records else None,
                "missing_required": missing, "nonfinite_numeric": nonfinite,
                "source_mismatch_rows": source_mismatch,
                "candidate_feature_coverage": candidate_features,
            }
        finally:
            conn.close()
    except Exception as exc:
        return {**base, "status": "UNAVAILABLE", "reason": type(exc).__name__, "rows": 0}


def write_report(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=str(output.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, output)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _report_lock(output: Path):
    lock_path = output.with_suffix(output.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=_default_db())
    parser.add_argument("--output", type=Path, default=_default_report())
    args = parser.parse_args(argv)
    with _report_lock(args.output) as acquired:
        if not acquired:
            return 0
        report = _read_db(args.db)
        write_report(report, args.output)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "READY_FOR_RESEARCH" else 2


if __name__ == "__main__":
    raise SystemExit(main())
