# 2026-10-19 Hermes Agent: derive MTS day/night volume z-score parameters from runtime bars.
"""Estimate reproducible MTS volume z-score parameters from selected TMF indicator bars."""

import csv
import datetime
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

DATA_DIR = Path("/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime/logs/market_data")
OUTPUT_PATH = Path(__file__).resolve().parents[2] / "config" / "mts_vol_zscore_params.json"
MIN_TRADE_DAYS = 55
DAY_START = datetime.time(8, 45)
DAY_END = datetime.time(13, 45)
NIGHT_START = datetime.time(15, 0)
NIGHT_END = datetime.time(5, 0)


def _trade_date_and_segment(timestamp: datetime.datetime):
    """Return the TAIFEX session label and trade date for a local bar timestamp."""
    clock = timestamp.time()
    if DAY_START <= clock <= DAY_END:
        return timestamp.date(), "day"
    if clock >= NIGHT_START:
        return timestamp.date() + datetime.timedelta(days=1), "night"
    if clock <= NIGHT_END:
        return timestamp.date(), "night"
    return None, None


def _select_daily_files(data_dir: Path):
    by_date = defaultdict(list)
    for path in data_dir.glob("TMF_*_indicators.csv"):
        fields = path.stem.split("_")
        if len(fields) != 4 or fields[0] != "TMF" or fields[2] not in ("PAPER", "LIVE"):
            continue
        try:
            datetime.datetime.strptime(fields[1], "%Y%m%d")
        except ValueError:
            continue
        by_date[fields[1]].append(path)
    selected = {}
    for date, paths in by_date.items():
        selected[date] = sorted(paths, key=lambda p: ("LIVE" not in p.stem, p.name))[0]
    return by_date, selected


def estimate_from_directory(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    by_date, selected = _select_daily_files(Path(data_dir))
    if not selected:
        raise ValueError(f"No TMF daily indicator CSV files found in {data_dir}")

    volumes = {"day": [], "night": []}
    used_dates = set()
    rows_read = 0
    rows_outside_sessions = 0
    rows_from_other_trade_dates = 0
    rows_missing_or_nonfinite_volume = 0
    rows_zero_volume = 0
    rows_used = 0
    selected_live = 0
    selected_paper = 0
    target_timestamp_groups = defaultdict(list)
    trading_day_mismatches = 0

    for filename_date, path in sorted(selected.items()):
        selected_live += path.stem.endswith("_LIVE_indicators")
        selected_paper += path.stem.endswith("_PAPER_indicators")
        with path.open(newline="", encoding="utf-8") as source:
            rows = list(csv.DictReader(source))
        rows_read += len(rows)
        for row in rows:
            raw_ts = row.get("timestamp")
            try:
                timestamp = datetime.datetime.fromisoformat(raw_ts)
            except (TypeError, ValueError):
                rows_missing_or_nonfinite_volume += 1
                continue
            trade_date, segment = _trade_date_and_segment(timestamp)
            if segment is None:
                rows_outside_sessions += 1
                continue
            if trade_date.strftime("%Y%m%d") != filename_date:
                rows_from_other_trade_dates += 1
                continue
            if timestamp.second != 0 or timestamp.microsecond != 0 or timestamp.minute % 5:
                raise ValueError(f"Non-5-minute bar timestamp in {path.name}: {raw_ts}")
            row_trade_day = row.get("trading_day")
            if row_trade_day:
                normalized = row_trade_day.replace("-", "")
                if normalized != filename_date:
                    trading_day_mismatches += 1
            target_timestamp_groups[(filename_date, segment)].append(timestamp)
            try:
                volume = float(row.get("volume", ""))
            except (TypeError, ValueError):
                rows_missing_or_nonfinite_volume += 1
                continue
            if not math.isfinite(volume):
                rows_missing_or_nonfinite_volume += 1
                continue
            if volume == 0:
                rows_zero_volume += 1
                continue
            if volume < 0:
                rows_missing_or_nonfinite_volume += 1
                continue
            volumes[segment].append(volume)
            used_dates.add(filename_date)
            rows_used += 1

    for key, timestamps in target_timestamp_groups.items():
        ordered = sorted(timestamps)
        if any((right - left).total_seconds() % 300 for left, right in zip(ordered, ordered[1:])):
            raise ValueError(f"Bar spacing is not on a 5-minute grid for {key}")

    if len(used_dates) < MIN_TRADE_DAYS:
        raise ValueError(f"Need at least {MIN_TRADE_DAYS} trade dates with usable volume; found {len(used_dates)}")
    if any(not volumes[segment] for segment in ("day", "night")):
        raise ValueError("Both day and night sessions must contain usable volume")

    params = {
        segment: {
            "mean": statistics.mean(values),
            "std": statistics.pstdev(values),
        }
        for segment, values in volumes.items()
    }
    if any(not math.isfinite(v["std"]) or v["std"] <= 0 for v in params.values()):
        raise ValueError("Each session needs a finite positive standard deviation")

    return {
        "source": {
            "path": str(Path(data_dir)),
            "filename_date_range": [min(by_date), max(by_date)],
            "selected_files": len(selected),
            "candidate_files": sum(map(len, by_date.values())),
            "duplicate_date_files_omitted": sum(len(paths) - 1 for paths in by_date.values()),
            "selected_live_files": selected_live,
            "selected_paper_files": selected_paper,
            "used_trade_date_range": [min(used_dates), max(used_dates)],
            "usable_trade_dates": len(used_dates),
            "bar_granularity": "5-minute",
            "trade_date_rule": "day 08:45-13:45 same date; night 15:00-05:00, 15:00+ maps to next calendar date",
            "rows_read": rows_read,
            "rows_outside_sessions_excluded": rows_outside_sessions,
            "rows_from_other_trade_dates_excluded": rows_from_other_trade_dates,
            "rows_missing_or_nonfinite_volume_excluded": rows_missing_or_nonfinite_volume,
            "zero_volume_rows_excluded": rows_zero_volume,
            "rows_used": rows_used,
            "trading_day_field_mismatches": trading_day_mismatches,
        },
        "params": params,
    }


def write_output(data_dir: Path = DATA_DIR, output_path: Path = OUTPUT_PATH) -> dict[str, Any]:
    result = estimate_from_directory(data_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def load_params_output(output_path: Path = OUTPUT_PATH) -> dict[str, dict[str, float]]:
    return json.loads(Path(output_path).read_text(encoding="utf-8"))["params"]


if __name__ == "__main__":
    result = write_output()
    print(json.dumps(result, indent=2, sort_keys=True))
