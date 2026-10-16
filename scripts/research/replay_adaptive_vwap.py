"""2026-10-16 Hermes Agent: offline paired-candidate replay, never a trading executor."""
from __future__ import annotations

import argparse
from collections import deque
import csv
from datetime import datetime, timedelta
import glob
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.plugins.futures.active.mts_adaptive_ab import (
    compare_outcomes, evaluate_hvwap_ab, log_ab_pair, paired_record,
)
from strategies.plugins.futures.active.mts_adaptive_vwap import AdaptiveVWAPConfig, TW, parse_ts
from strategies.plugins.futures.active.mts_oi_estimator import OIEstimator


def load_completed_bars(paths, *, timestamp_kind, source_bar_secs):
    """Normalize/merge overlapping CSVs and retain complete 5m buckets only."""
    if timestamp_kind not in ("start", "close") or source_bar_secs <= 0 or 300 % source_bar_secs:
        raise ValueError("source bars must evenly divide five minutes, with explicit timestamp kind")
    raw = {}
    for path in sorted(paths):
        with Path(path).open() as stream:
            for row in csv.DictReader(stream):
                ts = parse_ts(row.get("ts"))
                try:
                    bar = {key: float(row.get(key, row.get(key.capitalize(), "")))
                           for key in ("open", "high", "low", "close", "volume")}
                except (TypeError, ValueError):
                    continue
                if ts is None or not all(math.isfinite(v) for v in bar.values()):
                    continue
                if min(bar[k] for k in ("open", "high", "low", "close")) <= 0 or bar["volume"] < 0:
                    continue
                start = ts if timestamp_kind == "start" else ts - source_bar_secs
                if not math.isclose(start % source_bar_secs, 0.0, abs_tol=1e-6):
                    continue
                raw[start] = bar
    buckets = {}
    for ts, bar in sorted(raw.items()):
        bucket = int(ts // 300) * 300
        buckets.setdefault(bucket, []).append((ts, bar))
    completed = {}
    count = 300 // source_bar_secs
    for start, values in buckets.items():
        if [ts for ts, _ in values] != [start + i * source_bar_secs for i in range(count)]:
            continue  # missing minutes never become fabricated completed bars
        bars = [bar for _, bar in values]
        end = start + 300
        completed[end] = {"ts": end, "open": bars[0]["open"], "close": bars[-1]["close"],
                          "high": max(b["high"] for b in bars), "low": min(b["low"] for b in bars),
                          "volume": sum(b["volume"] for b in bars)}
    return completed


def replay(near_paths, far_paths, *, start_date, end_date, output_dir,
           timestamp_kind, source_bar_secs=60, events_path=None, outcomes_path=None,
           initial_equity=40000.0, config=None):
    cfg = config or AdaptiveVWAPConfig()
    start = datetime.fromisoformat(start_date).replace(tzinfo=TW).timestamp()
    end = (datetime.fromisoformat(end_date).replace(tzinfo=TW) + timedelta(days=1)).timestamp()
    if start >= end:
        raise ValueError("end date must not precede start date")
    near = load_completed_bars(near_paths, timestamp_kind=timestamp_kind, source_bar_secs=source_bar_secs)
    far = load_completed_bars(far_paths, timestamp_kind=timestamp_kind, source_bar_secs=source_bar_secs)
    estimator = OIEstimator(str(events_path)) if events_path else None
    root = Path(output_dir)
    if root.exists() and any(root.iterdir()):
        raise ValueError("output directory must be empty to prevent mixing replay runs")
    root.mkdir(parents=True, exist_ok=True)
    n_history, f_history = deque(maxlen=320), deque(maxlen=320)
    ranges, partial_15m = deque(maxlen=14), []
    previous_15m_close = None
    count, valid = 0, 0
    # 2026-10-16 Hermes Agent: outcome timestamps must match the replay pair.
    days, pair_times, pair_valid = set(), {}, {}
    for ts in sorted(near.keys() & far.keys()):
        if ts >= end:
            break
        nb, fb = near[ts], far[ts]
        n_history.append(nb)
        f_history.append(fb)
        partial_15m.append(nb)
        if ts % 900 == 0:
            expected = [ts - 600, ts - 300, ts]
            recent = partial_15m[-3:]
            if [b["ts"] for b in recent] == expected:
                high, low = max(b["high"] for b in recent), min(b["low"] for b in recent)
                tr = high - low
                if previous_15m_close is not None:
                    tr = max(tr, abs(high - previous_15m_close), abs(low - previous_15m_close))
                ranges.append(tr)
                previous_15m_close = recent[-1]["close"]
            partial_15m = []
        if ts < start:
            continue  # history may prewarm; no outputs before the requested date
        oi, events = None, None
        if estimator is not None:
            oi = estimator.estimate_oi(ts)
            events = [e for e in (estimator._event_cache or [])
                      if (et := estimator._parse_ts(e.get("ts", ""))) is not None and ts - 300 < et <= ts]
        inputs = dict(decision_ts=ts, near_bars=list(n_history), far_bars=list(f_history),
                      near_price=nb["close"], far_price=fb["close"],
                      # Fixed, explicitly hypothetical spread orientation for replay.
                      near_side="LONG", far_side="SHORT", position_phase="SPREAD",
                      atr_15m=sum(ranges) / len(ranges) if ranges else None,
                      oi_snapshot=oi, event_sequence=events)
        arms = evaluate_hvwap_ab(config=cfg, **inputs)
        pair_id = f"replay:{ts}"
        record = paired_record(pair_id, arms, cfg)
        record["source"] = "HISTORICAL_BAR_REPLAY"
        log_ab_pair(record, root / "logs" / "ab_test")
        pair_times[pair_id] = ts
        pair_valid[pair_id] = record["adaptive_data_valid"]
        count += 1
        valid += record["adaptive_data_valid"]
        days.add(datetime.fromtimestamp(ts, TW).date().isoformat())
    outcomes = []
    if outcomes_path:
        with Path(outcomes_path).open() as stream:
            outcomes = [json.loads(line) for line in stream if line.strip()]
        if any(row.get("pair_id") not in pair_times
               or parse_ts(row.get("ts")) != pair_times[row["pair_id"]] for row in outcomes):
            raise ValueError("outcomes contain pairs outside this replay")
    comparison = compare_outcomes(outcomes, initial_equity=initial_equity, config=cfg)
    report = {"source": "HISTORICAL_BAR_REPLAY", "start_date": start_date, "end_date": end_date,
              "paired_decisions": count, "valid_adaptive_decisions": valid,
              "observed_dates": sorted(days), "data_gaps": [], "comparison": comparison,
              "timestamp_kind": timestamp_kind, "source_bar_secs": source_bar_secs,
              "atr_provenance": "SMA of up to 14 completed 15m true ranges",
              "position_assumption": "hypothetical LONG NEAR / SHORT FAR; no orders or simulated profits"}
    if not events_path:
        report["data_gaps"].append("OI_QUALITY_EVENTS_MISSING")
    if count == 0:
        report["data_gaps"].append("NO_MATCHING_COMPLETED_BARS")
    if not outcomes:
        report["data_gaps"].append("PAIRED_NET_OUTCOMES_MISSING")
    if valid < count:
        report["data_gaps"].append("ADAPTIVE_INPUTS_INCOMPLETE")
    # 2026-10-16 Hermes Agent: only complete, matching outcome pairs may earn a
    # candidate GO. Off-segment observation gaps remain visible without vetoing
    # unrelated, valid paired trades; unavailable evaluated pairs always block GO.
    invalid_outcomes = any(not pair_valid[row["pair_id"]] for row in outcomes)
    if invalid_outcomes:
        report["data_gaps"].append("OUTCOMES_WITH_UNAVAILABLE_ADAPTIVE_INPUTS")
    if not events_path or not outcomes or count == 0 or invalid_outcomes:
        comparison["promotion"] = "NO_GO"
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    stem = f"ab_comparison_{start_date}_{end_date}"
    (reports / f"{stem}.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    (reports / f"{stem}.md").write_text(
        f"# Adaptive VWAP replay\n\nDates: {start_date} to {end_date}\n\n"
        f"Paired decisions: {count}; adaptive inputs valid: {valid}.\n\n"
        f"Observed dates: {', '.join(sorted(days)) or 'none'}.\n\n"
        f"Data gaps: {', '.join(report['data_gaps']) or 'none'}.\n\n"
        f"Comparison: {comparison['status']}; promotion: {comparison['promotion']}.\n\n"
        "```json\n" + json.dumps(comparison, indent=2, allow_nan=False) + "\n```\n\n"
        "No orders, fabricated OI/quality signals, or price-derived trading profits.\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--near-glob", required=True)
    parser.add_argument("--far-glob", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timestamp-kind", required=True, choices=("start", "close"))
    parser.add_argument("--source-bar-secs", type=int, default=60)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--outcomes", type=Path)
    parser.add_argument("--initial-equity", type=float, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config/adaptive_vwap_config.yaml")
    args = parser.parse_args(argv)
    result = replay(glob.glob(args.near_glob), glob.glob(args.far_glob), start_date=args.start,
                    end_date=args.end, output_dir=args.output_dir, timestamp_kind=args.timestamp_kind,
                    source_bar_secs=args.source_bar_secs, events_path=args.events, outcomes_path=args.outcomes,
                    initial_equity=args.initial_equity, config=AdaptiveVWAPConfig.load(args.config))
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    import os
    # 2026-10-16 Hermes Agent: E-Core policy also applies to spawned children.
    if sys.platform == "darwin":
        os.system(f"taskpolicy -b -p {os.getpid()}")
    main()
