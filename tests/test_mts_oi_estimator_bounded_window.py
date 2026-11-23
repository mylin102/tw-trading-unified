"""2026-11-23 Gemini CLI: Parity and bounded-memory tests for OIEstimator reverse-chunk tail-read.

Verifies:
1. Continuous append + changing mtime produces identical OISnapshot and window events
   compared to a full-file scan reference implementation.
2. Memory usage stays O(window) rather than O(file size) across 10k vs 100k JSONL lines
   measured with tracemalloc.
"""

from __future__ import annotations

import json
import os
import time
import tracemalloc
from datetime import datetime, timedelta

from strategies.plugins.futures.active.mts_oi_estimator import OIEstimator, OISnapshot


def _reference_estimate_oi(path: str, decision_ts: float, window_secs: int = 300) -> tuple[OISnapshot, list[dict]]:
    """Reference full-file scan matching pre-fix OIEstimator semantics."""
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    window_start = decision_ts - window_secs
    window_events = []
    for ev in events:
        ets = OIEstimator._parse_ts(ev.get("ts", ""))
        if ets is not None and window_start < ets <= decision_ts:
            window_events.append(ev)

    new_orders = OIEstimator._deduplicate_orders(
        window_events, ["ENTRY_SUBMITTED", "ENTRY", "ENTRY_AUDIT"]
    )
    close_orders = OIEstimator._deduplicate_orders(
        window_events,
        ["EXIT_LOG", "RELEASE_NEAR_SUBMITTED", "RELEASE_FAR_SUBMITTED", "EXIT_REMAINING"],
    )
    fills = [e for e in window_events if e.get("event") == "LEG_FILLED"]
    total_orders = len(new_orders) + len(close_orders)
    oi_direction = len(new_orders) / total_orders if total_orders > 0 else 0.5
    oi_intensity = total_orders / max(window_secs / 60, 1)
    filled_ratio = len(fills) / total_orders if total_orders > 0 else 0.0
    quality_score = OIEstimator._calculate_quality_score(filled_ratio)

    snap = OISnapshot(
        ts=decision_ts,
        oi_direction=oi_direction,
        oi_intensity=oi_intensity,
        filled_ratio=filled_ratio,
        new_orders=len(new_orders),
        close_orders=len(close_orders),
        total_fills=len(fills),
        quality_score=quality_score,
    )
    return snap, window_events


def test_incremental_appends_with_changing_mtime_match_full_scan_exactly(tmp_path):
    """Appending new events across multiple 5m windows with changing mtime must match
    full-file scan results identically (including UTF-8 payloads and slight out-of-order)."""
    log_path = tmp_path / "mts_spread_events.jsonl"
    base_dt = datetime(2026, 11, 23, 9, 0, 0)
    est = OIEstimator(str(log_path))
    # Force smaller chunk size to stress multi-chunk boundary splitting + UTF-8 reassembly
    est._TAIL_CHUNK_BYTES = 1024

    order_seq = 0
    for step in range(5):
        step_start = base_dt + timedelta(minutes=5 * step)
        batch = []
        # Add bulk padding events + real order/fill events + UTF-8 text + 10s out-of-order entry
        for j in range(15):
            ev_dt = step_start + timedelta(seconds=15 * j)
            batch.append({
                "event": "HVWAP_CANDIDATE",
                "ts": ev_dt.isoformat(),
                "note": f"中文事件備註-{step}-{j}-" + ("測試" * 20),
            })
        order_seq += 1
        batch.append({"event": "ENTRY", "order_id": f"ORD-N-{order_seq}", "ts": (step_start + timedelta(seconds=60)).isoformat()})
        batch.append({"event": "LEG_FILLED", "order_id": f"ORD-N-{order_seq}", "ts": (step_start + timedelta(seconds=65)).isoformat()})
        # Slight out-of-order event (10s earlier than previous line, within 60s grace)
        batch.append({"event": "ENTRY_AUDIT", "order_id": f"ORD-N-{order_seq}", "ts": (step_start + timedelta(seconds=55)).isoformat()})
        if step % 2 == 1:
            batch.append({"event": "EXIT_LOG", "order_id": f"ORD-X-{order_seq}", "ts": (step_start + timedelta(seconds=240)).isoformat()})

        with open(log_path, "a", encoding="utf-8") as f:
            for row in batch:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        # Explicitly bump mtime on every append
        now_t = time.time() + step + 1
        os.utime(log_path, (now_t, now_t))

        decision_ts = (step_start + timedelta(minutes=5)).timestamp()
        actual = est.estimate_oi(decision_ts, window_secs=300)
        expected, expected_events = _reference_estimate_oi(str(log_path), decision_ts, window_secs=300)

        assert actual == expected
        assert est._event_cache == expected_events


def _write_sized_fixture(path, total_lines: int, recent_lines: int = 100) -> float:
    """Write total_lines where only the last recent_lines fall inside the 300s window."""
    decision_dt = datetime(2026, 11, 23, 15, 30, 0)
    old_count = total_lines - recent_lines
    old_start = decision_dt - timedelta(hours=12)
    old_span_secs = 11 * 3600  # 12h ago .. 1h ago (well outside 300s window)

    with open(path, "w", encoding="utf-8") as f:
        for i in range(old_count):
            ts = old_start + timedelta(seconds=(i * old_span_secs) / max(old_count, 1))
            f.write(
                json.dumps({
                    "event": "HVWAP_CANDIDATE",
                    "ts": ts.isoformat(),
                    "payload": "x" * 180,
                }) + "\n"
            )
        for j in range(recent_lines):
            ts = decision_dt - timedelta(seconds=(recent_lines - j) * 2)
            ev_type = "ENTRY" if j % 3 == 0 else ("LEG_FILLED" if j % 3 == 1 else "EXIT_LOG")
            f.write(
                json.dumps({
                    "event": ev_type,
                    "order_id": f"REC-{j // 3}",
                    "ts": ts.isoformat(),
                }) + "\n"
            )
    return decision_dt.timestamp()


def test_memory_stays_bounded_10k_vs_100k_lines(tmp_path):
    """Peak and steady memory must stay flat when historical log grows 10x (10k -> 100k lines)."""
    p10k = tmp_path / "events_10k.jsonl"
    p100k = tmp_path / "events_100k.jsonl"
    ts_10k = _write_sized_fixture(p10k, total_lines=10_000, recent_lines=100)
    ts_100k = _write_sized_fixture(p100k, total_lines=100_000, recent_lines=100)

    def _measure(path, decision_ts):
        tracemalloc.start()
        try:
            est = OIEstimator(str(path))
            snap = est.estimate_oi(decision_ts, window_secs=300)
            cur, peak = tracemalloc.get_traced_memory()
            cache_len = len(est._event_cache or [])
            return snap, cur, peak, cache_len
        finally:
            tracemalloc.stop()

    snap_10k, cur_10k, peak_10k, len_10k = _measure(p10k, ts_10k)
    snap_100k, cur_100k, peak_100k, len_100k = _measure(p100k, ts_100k)

    assert snap_10k == snap_100k
    assert len_10k == 100
    assert len_100k == 100
    # 10x larger file must stay well under 5 MB peak and not scale linearly with file size
    assert peak_100k < 5 * 1024 * 1024, f"100k peak too high: {peak_100k / 1048576:.2f} MB"
    assert peak_100k <= max(peak_10k * 2.0, 2 * 1024 * 1024), (
        f"Memory scaled with file size: 10k={peak_10k/1048576:.2f}MB vs 100k={peak_100k/1048576:.2f}MB"
    )
