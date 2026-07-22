#!/usr/bin/env python3
"""
Daily Episode Dataset Builder — run after each trading session.

Reads TMF indicators + far prices + orders for a given date,
detects episodes, computes episode-level metrics, and appends
to the research episode dataset.

Usage:
    .venv/bin/python scripts/build_episode_dataset.py --date 2026-07-22
    .venv/bin/python scripts/build_episode_dataset.py --date auto   # latest
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta
from statistics import mean, stdev
from pathlib import Path

# ── Config ──
ENTRY_Z = 2.0
RESET_Z = 0.5
ROLLING_WINDOW = 20
DATASET_PATH = "data/episode_dataset.jsonl"
MARKET_DATA_DIR = "logs/market_data"
TRADES_DIR = "exports/trades"


def load_indicators(date_str: str):
    """Load near indicators and far prices for a given trading date."""
    near_path = os.path.join(MARKET_DATA_DIR, f"TMF_{date_str}_PAPER_indicators.csv")
    far_path = os.path.join(MARKET_DATA_DIR, f"TMF_far_{date_str}_PAPER.csv")

    if not os.path.exists(near_path):
        print(f"  [SKIP] No near indicators: {near_path}")
        return None
    if not os.path.exists(far_path):
        print(f"  [SKIP] No far prices: {far_path}")
        return None

    with open(near_path) as f:
        near_rows = list(csv.DictReader(f))
    with open(far_path) as f:
        far_rows = list(csv.DictReader(f))

    far_map = {}
    for r in far_rows:
        try:
            far_map[r["timestamp"]] = float(r["close"])
        except (ValueError, KeyError):
            pass

    # Merge near + far
    merged = []
    for r in near_rows:
        ts = r.get("timestamp", "")
        if ts not in far_map:
            continue
        try:
            near_c = float(r.get("close", 0))
            far_c = far_map[ts]
            spread = far_c - near_c
            merged.append({"ts": ts, "near": near_c, "far": far_c, "spread": spread})
        except (ValueError, TypeError):
            pass

    if len(merged) < ROLLING_WINDOW + 5:
        print(f"  [SKIP] Too few merged bars ({len(merged)})")
        return None

    return merged


def compute_z_scores(merged: list[dict]):
    """Add rolling Z-score to merged data."""
    for i, d in enumerate(merged):
        if i < ROLLING_WINDOW:
            d["z"] = 0.0
            continue
        ws = [merged[j]["spread"] for j in range(i - ROLLING_WINDOW, i)]
        mu, s = mean(ws), stdev(ws)
        d["z"] = (d["spread"] - mu) / s if s > 0 else 0.0
    return merged


def detect_episodes(merged: list[dict]) -> list[dict]:
    """Detect episodes from spread Z-score."""
    episodes = []
    cur = None
    for i, d in enumerate(merged):
        az = abs(d["z"])
        if cur is None:
            if az >= ENTRY_Z:
                cur = {
                    "start_bar": i, "end_bar": i,
                    "start_ts": d["ts"], "end_ts": d["ts"],
                    "dir": "WIDE" if d["z"] > 0 else "NARROW",
                    "start_z": d["z"], "max_z": az,
                    "start_spread": d["spread"],
                    "max_spread": d["spread"], "min_spread": d["spread"],
                    "bar_count": 1,
                }
        else:
            cur["end_bar"] = i
            cur["end_ts"] = d["ts"]
            cur["max_z"] = max(cur["max_z"], az)
            cur["max_spread"] = max(cur["max_spread"], d["spread"])
            cur["min_spread"] = min(cur["min_spread"], d["spread"])
            cur["bar_count"] += 1
            if az < RESET_Z:
                episodes.append(cur)
                cur = None
    if cur:
        episodes.append(cur)
    return episodes


def load_config() -> dict:
    """Load strategy config parameters for episode context."""
    import yaml
    params = {}
    for cfg_name in ["futures.yaml", "futures_night.yaml"]:
        cfg_path = Path("config") / cfg_name
        if not cfg_path.exists():
            continue
        try:
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            spread = cfg.get("mts", {}).get("params", {})
            trailing = cfg.get("trailing_stop", {})
            params[cfg_name] = {
                "entry_z": spread.get("entry_z", "N/A"),
                "release_stop_points": spread.get("release_stop_points", "N/A"),
                "trail_distance_points": spread.get("trail_distance_points", "N/A"),
                "atr_multiplier_stop": spread.get("atr_multiplier_stop", "N/A"),
                "atr_multiplier_trail": spread.get("atr_multiplier_trail", "N/A"),
            }
        except Exception:
            params[cfg_name] = {}
    return params


def load_orders(date_str: str) -> list[dict]:
    """Load TMF orders for a given date."""
    order_path = os.path.join(TRADES_DIR, f"TMF_{date_str}_orders.json")
    if not os.path.exists(order_path):
        return []
    with open(order_path) as f:
        return json.load(f)


def match_entries_to_episodes(orders: list[dict], merged: list[dict], episodes: list[dict]) -> list[dict]:
    """Match entries to episodes and add entry counts."""
    entries = [o for o in orders if o.get("strategy") == "MTS_ENTRY"]

    # Build bar timestamp lookup
    bar_times = {}
    for i, d in enumerate(merged):
        try:
            bar_times[d["ts"]] = i
        except (ValueError, KeyError):
            pass

    for ep in episodes:
        ep["entry_count"] = 0
        ep["entry_ids"] = []

    for e in entries:
        ts = str(e.get("created_at", ""))
        # Normalize: find the bar containing this timestamp
        try:
            if "T" in ts:
                dt = datetime.strptime(ts.split(".")[0], "%Y-%m-%dT%H:%M:%S")
            else:
                dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        except (ValueError, IndexError):
            continue

        # Find matching bar (with tolerance)
        for ep in episodes:
            try:
                t0 = datetime.strptime(merged[ep["start_bar"]]["ts"], "%Y-%m-%d %H:%M:%S")
                t1 = datetime.strptime(merged[ep["end_bar"]]["ts"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, IndexError):
                continue
            if t0 - timedelta(minutes=5) <= dt <= t1 + timedelta(minutes=5):
                ep["entry_count"] += 1
                ep["entry_ids"].append(e.get("order_id", ""))
                break

    return episodes


def build_episode_records(date_str: str, episodes: list[dict], config: dict, orders: list[dict]) -> list[dict]:
    """Build structured episode records with config thresholds."""
    records = []
    # Determine which config was active (night vs day)
    active_config = config.get("futures_night.yaml", config.get("futures.yaml", {}))
    day_config = config.get("futures.yaml", {})
    night_config = config.get("futures_night.yaml", {})
    
    # Build release/exit price lookup by entry order_id
    release_prices = {}
    exit_prices = {}
    for o in orders:
        strat = o.get("strategy", "")
        if strat == "MTS_RELEASE":
            pid = (o.get("parent_order_id") or o.get("order_id", ""))[:15]
            release_prices[pid] = o.get("avg_fill_price")
        elif strat == "MTS_EXIT":
            pid = (o.get("parent_order_id") or o.get("order_id", ""))[:15]
            exit_prices[pid] = o.get("avg_fill_price")

    for i, ep in enumerate(episodes):
        if ep["entry_count"] == 0:
            continue
        dur_min = ep["bar_count"] * 5
        
        # Determine if episode was during night session
        is_night = "18:" in ep.get("start_ts", "") or "19:" in ep.get("start_ts", "") or \
                   "20:" in ep.get("start_ts", "") or "21:" in ep.get("start_ts", "") or \
                   "22:" in ep.get("start_ts", "") or "23:" in ep.get("start_ts", "") or \
                   "00:" in ep.get("start_ts", "") or "01:" in ep.get("start_ts", "") or \
                   "02:" in ep.get("start_ts", "") or "03:" in ep.get("start_ts", "") or \
                   "04:" in ep.get("start_ts", "")
        cfg = night_config if is_night else day_config
        
        records.append({
            "dataset_generation": "v1",
            "trading_date": date_str,
            "episode_id": f"{date_str.replace('-', '')}_{i+1:03d}",
            "direction": ep["dir"],
            "start_ts": ep["start_ts"],
            "end_ts": ep["end_ts"],
            "duration_min": dur_min,
            "bar_count": ep["bar_count"],
            "start_z": round(ep["start_z"], 2),
            "max_z": round(ep["max_z"], 2),
            "start_spread": ep["start_spread"],
            "max_spread": ep["max_spread"],
            "min_spread": ep["min_spread"],
            "entry_count": ep["entry_count"],
            "entry_ids": ep["entry_ids"],
            # Config thresholds at time of episode
            "config_entry_z": cfg.get("entry_z", "N/A"),
            "config_release_stop_points": cfg.get("release_stop_points", "N/A"),
            "config_trail_distance_points": cfg.get("trail_distance_points", "N/A"),
            "config_atr_multiplier_stop": cfg.get("atr_multiplier_stop", "N/A"),
            "config_atr_multiplier_trail": cfg.get("atr_multiplier_trail", "N/A"),
        })
    return records


def main():
    parser = argparse.ArgumentParser(description="Build daily episode dataset")
    parser.add_argument("--date", default="auto", help="Trading date (YYYY-MM-DD) or 'auto' for latest")
    args = parser.parse_args()

    date_str = args.date
    if date_str == "auto":
        # Find latest available indicators file
        files = sorted(Path(MARKET_DATA_DIR).glob("TMF_*_PAPER_indicators.csv"))
        if not files:
            print("No indicator files found")
            sys.exit(1)
        last_file = files[-1].stem  # TMF_20260722_PAPER_indicators
        date_compact = last_file.split("_")[1]
    else:
        date_compact = date_str.replace("-", "")

    print(f"Building episode dataset for {date_str} (compact: {date_compact})...")

    # Load and process
    merged = load_indicators(date_compact)
    if merged is None:
        sys.exit(0)

    merged = compute_z_scores(merged)
    episodes = detect_episodes(merged)
    orders = load_orders(date_compact)
    episodes = match_entries_to_episodes(orders, merged, episodes)
    config = load_config()

    records = build_episode_records(date_str, episodes, config, orders)

    if not records:
        print("  No episodes with entries found")
        return

    # Append to dataset
    dataset_path = Path(DATASET_PATH)
    dataset_path.parent.mkdir(exist_ok=True)

    existing = 0
    if dataset_path.exists():
        with open(dataset_path) as f:
            existing = sum(1 for _ in f)

    with open(dataset_path, "a") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"  Episodes with entries: {len(records)}")
    print(f"  Total entries in episodes: {sum(r['entry_count'] for r in records)}")
    print(f"  Dataset total (before): {existing} records")
    print(f"  Dataset total (after): {existing + len(records)} records")
    print(f"  Appended to: {dataset_path}")


if __name__ == "__main__":
    main()
