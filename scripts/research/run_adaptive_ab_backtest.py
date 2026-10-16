"""2026-10-16 Hermes Agent: explicitly assumed next-bar proxy study, not MTS execution."""
from __future__ import annotations

import argparse
import csv
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.deployment_role_gate import assert_research_allowed
from scripts.research.replay_adaptive_vwap import load_completed_bars
from strategies.plugins.futures.active.mts_adaptive_vwap import AdaptiveVWAP, AdaptiveVWAPConfig, TW
from strategies.plugins.futures.active.mts_hvwap_candidate import vwap_session_bounds
from strategies.plugins.futures.active.mts_vol_adjuster import VolAdjuster


MODEL = {
    "name": "NEXT_BAR_DIRECTION_PROXY_V1",
    "signal": "decision close above/below reference -> long/short; equality -> flat",
    "entry": "next contiguous completed five-minute bar open",
    "exit": "same next bar close; one-bar hold",
    "quantity": 1, "cost_points": 0.0, "pnl_unit": "index_points",
    "oi_intensity_default": 1.5, "oi_direction_default": 0.65,
    "quality_model": "VolAdjuster Gaussian volume quality; unavailable outside calibrated segments",
    "vwap_scope": "all available complete NEAR bars in current 15:00-reset session",
    "price_equality_tolerance": 1e-9,
    "promotion_eligible": False,
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def direction(price, reference):
    if reference is None or abs(price - reference) <= MODEL["price_equality_tolerance"]:
        return 0
    return 1 if price > reference else -1


def audit_source(path):
    """2026-10-16 Hermes Agent: fail closed on invalid or conflicting source minutes."""
    seen, duplicates = {}, 0
    with Path(path).open() as stream:
        for row in csv.DictReader(stream):
            ts = datetime.fromisoformat(row["ts"])
            values = tuple(float(row[k]) for k in ("Open", "High", "Low", "Close", "Volume"))
            o, h, low, c, volume = values
            if (ts.second or ts.microsecond or not all(math.isfinite(v) for v in values)
                    or min(o, h, low, c) <= 0 or volume < 0 or not low <= min(o, c) <= max(o, c) <= h):
                raise ValueError(f"invalid source minute: {path}: {ts}")
            if ts in seen:
                duplicates += 1
                if seen[ts] != values:
                    raise ValueError(f"conflicting source minute: {path}: {ts}")
            seen[ts] = values
    if not seen:
        raise ValueError("empty source snapshot")
    return {"unique_minutes": len(seen), "identical_duplicates": duplicates,
            "first_minute": str(min(seen)), "last_minute": str(max(seen))}


def run_study(near_path, far_path, *, start_date, end_date, output_dir,
              config_path=ROOT / "config/adaptive_vwap_config.yaml", initial_equity=40000.0):
    """Paired price-line proxy with modeled missing signals; realized outcomes stay separate."""
    assert_research_allowed(ROOT)
    if datetime.now().astimezone().utcoffset() != timedelta(hours=8):
        raise ValueError("supplied VolAdjuster requires an Asia/Taipei local timezone")
    if isinstance(initial_equity, bool) or not isinstance(initial_equity, (float, int)) or not 0 < initial_equity < float("inf"):
        raise ValueError("initial equity must be finite and positive")
    cfg = AdaptiveVWAPConfig.load(config_path)
    # 2026-10-16 Hermes Agent: refuse to silently change the frozen v1 model.
    if cfg.reference_method != "twap" or cfg.window_secs != 300 or cfg.bar_secs != 300:
        raise ValueError("v1 requires its frozen 300-second TWAP config; use --model-version v2 for EMA")
    start = datetime.fromisoformat(start_date).replace(tzinfo=TW).timestamp()
    end = (datetime.fromisoformat(end_date).replace(tzinfo=TW) + timedelta(days=1)).timestamp()
    if start >= end:
        raise ValueError("invalid date range")
    root = Path(output_dir)
    if root.exists() and (list(root.glob("baseline_*.jsonl")) or list(root.glob("adaptive_*.jsonl"))
                          or (root / "model_manifest.json").exists()):
        raise ValueError("proxy output already exists; use a fresh output directory")
    source_audit = {str(path): audit_source(path) for path in (near_path, far_path)}
    near = load_completed_bars([near_path], timestamp_kind="start", source_bar_secs=60)
    far = load_completed_bars([far_path], timestamp_kind="start", source_bar_secs=60)
    rows = []
    history = deque(maxlen=320)
    session, volume_sum, price_volume_sum = None, 0.0, 0.0
    for ts, nb in sorted(near.items()):
        if ts >= end:
            break
        history.append(nb)
        label, _ = vwap_session_bounds(ts)
        if label != session:
            session, volume_sum, price_volume_sum = label, 0.0, 0.0
        volume_sum += nb["volume"]
        price_volume_sum += nb["close"] * nb["volume"]
        if ts < start or ts not in far:
            continue
        vwap = price_volume_sum / volume_sum if volume_sum > 0 else None
        adjustment = VolAdjuster.adjust(ts, nb["volume"])
        calibrated = adjustment.segment is not None
        adaptive = (AdaptiveVWAP.compute(ts=ts, oi_intensity=MODEL["oi_intensity_default"],
            oi_direction=MODEL["oi_direction_default"], vol_quality=adjustment.quality_rating,
            vol_zscore=adjustment.z_score, vwap=vwap, bars=list(history), config=cfg)
                    if calibrated else None)
        # 2026-10-16 Hermes Agent: features use bars closed at/before ts. The future bar
        # below is used exclusively for execution/outcome accounting.
        next_ts = ts + 300
        observed_outcome = next_ts in near and next_ts in far and next_ts < end
        refs = {"baseline": vwap, "adaptive": adaptive.adaptive_price if adaptive else None}
        common = vwap is not None and adaptive is not None
        signals = {
            "oi_intensity": MODEL["oi_intensity_default"], "oi_direction": MODEL["oi_direction_default"],
            "oi_source": "ASSUMED_CONSTANT_NOT_OBSERVED", "vol_zscore": adjustment.z_score if calibrated else None,
            "vol_anomaly_level": adjustment.anomaly_level, "vol_quality_score": adjustment.quality_rating if calibrated else None,
            "quality_source": "GAUSSIAN_VOLUME_PROXY_NOT_FRAUD_MEASUREMENT",
        }
        for arm, reference in refs.items():
            signal = direction(nb["close"], reference)
            evaluable = common and observed_outcome
            traded = evaluable and signal != 0
            entry = near[next_ts]["open"] if traded else None
            exit_price = near[next_ts]["close"] if traded else None
            pnl = (exit_price - entry) * signal if traded else (0.0 if evaluable else None)
            reason = ("EVALUATED" if evaluable else "ADAPTIVE_INPUT_UNAVAILABLE"
                      if not common else "NEXT_PAIRED_BAR_MISSING")
            rows.append({
                "schema_version": 1, "pair_id": f"proxy-v1:{ts}", "ts": ts, "arm": arm,
                "mode": "OFFLINE_ASSUMPTION_PROXY", "model": MODEL["name"],
                "near_close": nb["close"], "far_close": far[ts]["close"],
                "vwap": vwap, "twap": adaptive.twap if adaptive else None,
                "reference_price": reference, "chips_strength": adaptive.chips_strength if adaptive else None,
                "alpha": adaptive.alpha if adaptive else None, "signals": signals,
                "confirmed": reference is not None, "signal_direction": signal,
                "outcome_evaluable": evaluable, "simulated_trade": traded,
                "entry_ts": ts if traded else None, "entry_price": entry,
                "exit_ts": next_ts if evaluable else None, "exit_price": exit_price,
                "exit_decision": traded if evaluable else None,
                "gross_pnl_points": pnl, "assumed_cost_points": MODEL["cost_points"],
                "unavailable_reason": reason, "real_order_emitted": False,
            })
    if not rows:
        raise ValueError("no paired decision bars in requested dates")
    root.mkdir(parents=True, exist_ok=True)
    handles = {}
    try:
        for row in rows:
            day = datetime.fromtimestamp(row["ts"], TW).strftime("%Y%m%d")
            key = (row["arm"], day)
            if key not in handles:
                handles[key] = (root / f"{key[0]}_{day}.jsonl").open("w")
            handles[key].write(json.dumps(row, allow_nan=False) + "\n")
    finally:
        for stream in handles.values():
            stream.close()
    trace = [{"pair_id": row["pair_id"], "ts": row["ts"], "reason": row["unavailable_reason"],
              "oi_source": row["signals"]["oi_source"], "quality_source": row["signals"]["quality_source"]}
             for row in rows if row["arm"] == "adaptive"]
    (root / "replay_trace.jsonl").write_text("".join(json.dumps(row) + "\n" for row in trace))
    source_files = [Path(near_path), Path(far_path), Path(config_path), Path(__file__),
                    ROOT / "strategies/plugins/futures/active/mts_adaptive_vwap.py",
                    ROOT / "strategies/plugins/futures/active/mts_vol_adjuster.py",
                    ROOT / "strategies/plugins/futures/active/mts_hvwap_candidate.py",
                    ROOT / "scripts/research/replay_adaptive_vwap.py"]
    manifest = {
        "schema_version": 1, "study_kind": "ASSUMPTION_PROXY_NOT_MTS_BACKTEST", "model": MODEL,
        "start_date": start_date, "end_date": end_date, "initial_equity_points": initial_equity,
        "timestamp_contract": "source minute bars START; five-minute bars CLOSE",
        "config": asdict(cfg), "hostname": socket.gethostname(),
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_files": {str(path): sha256(path) for path in source_files},
        "source_audit": source_audit,
        "record_counts": {arm: sum(row["arm"] == arm for row in rows) for arm in ("baseline", "adaptive")},
        "outputs": {p.name: sha256(p) for p in sorted(root.glob("*.jsonl"))},
        "limitations": ["No observed OI or fraud indicators", "Zero modeled costs; gross point PnL",
                        "Fixed one-bar proxy is not the existing MTS strategy", "Sparse complete paired buckets only",
                        "Source CSV has no contract-code provenance", "No production promotion"]}
    (root / "model_manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--near", required=True, type=Path)
    parser.add_argument("--far", required=True, type=Path)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    # 2026-10-16 Hermes Agent: prevent silently mixing v1 point equity with v2 TWD capital.
    parser.add_argument("--initial-equity-points", type=float)
    parser.add_argument("--config", type=Path, default=ROOT / "config/adaptive_vwap_config.yaml")
    # 2026-10-16 Hermes Agent: v1 remains explicit; new CLI defaults to paired lifecycle v2.
    parser.add_argument("--model-version", choices=("v1", "v2"), default="v2")
    parser.add_argument("--validation-config", type=Path, default=ROOT / "config/adaptive_validation_config.yaml")
    parser.add_argument("--futures-config", type=Path, default=ROOT / "config/futures.yaml")
    args = parser.parse_args(argv)
    if args.model_version == "v2":
        if args.initial_equity_points is not None:
            raise ValueError("v2 uses TWD capital in validation config, not --initial-equity-points")
        from scripts.research.adaptive_validation_v2 import run_v2
        print(json.dumps(run_v2(args.near, args.far, start_date=args.start_date, end_date=args.end_date,
            output_dir=args.output_dir, config_path=args.config, validation_path=args.validation_config,
            futures_path=args.futures_config), indent=2))
        return
    print(json.dumps(run_study(args.near, args.far, start_date=args.start_date, end_date=args.end_date,
        output_dir=args.output_dir, config_path=args.config,
        initial_equity=40000.0 if args.initial_equity_points is None else args.initial_equity_points), indent=2))


if __name__ == "__main__":
    import os
    # 2026-10-16 Hermes Agent: macOS policy propagates to children too.
    if sys.platform == "darwin":
        os.system(f"taskpolicy -b -p {os.getpid()}")
    main()
