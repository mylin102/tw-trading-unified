#!/usr/bin/env python3
"""Validate futures bar regime classification — V2 with volume_spike fix.

V1 issue: volume_spike was MISSING from fresh indicator computation, so
classify_futures_bar_regime always saw volume_spike=0 → volume_confirmed=False
→ TREND/BEAR labels never assigned.

This version computes a proper volume_spike from rolling average.
"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass

from core.data_manager import data_manager as dm
from core.futures_bar_regime import (
    classify_futures_bar_regime,
    FuturesBarRegimeConfig,
    FuturesBarRegimeResult,
)
from strategies.futures.squeeze_futures.engine.indicators import (
    calculate_futures_squeeze,
    calculate_atr,
    calculate_mtf_alignment,
)

np.set_printoptions(precision=4, suppress=True)
pd.set_option("display.width", 140)
pd.set_option("display.max_columns", 30)

REGIME_CONFIG = FuturesBarRegimeConfig()
REPORT_PATH = BASE / "scripts" / "regime_validation_report_v2.txt"
FORWARD_BARS = [1, 3, 5, 10, 20]


def _safe_int(v):
    try:
        return int(v) if not (isinstance(v, float) and np.isnan(v)) else 0
    except (TypeError, ValueError):
        return 0


def compute_volume_spike(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Compute volume_spike as current volume / rolling average volume."""
    avg_vol = df["Volume"].rolling(window, min_periods=5).mean()
    spike = df["Volume"] / avg_vol
    return spike.fillna(1.0)


@dataclass
class ForwardStats:
    label: str
    bias: str
    n: int
    mean_returns: dict[int, float]
    win_rates: dict[int, float]
    mean_mfe: dict[int, float]
    mean_mae: dict[int, float]
    next_regime_dist: dict[str, float]


def main():
    print("=" * 72)
    print("  Regime Classification Validation — V2 (volume_spike fix)")
    print("=" * 72)

    # 1. Load data
    print("\n[1/5] Loading TXFR1 historical data...")
    df_raw = dm.load_historical("TXFR1")
    print(f"  Loaded {len(df_raw):,} rows")

    # Use only raw OHLCV — drop all pre-computed columns to avoid contamination
    df = df_raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    df = df.dropna(subset=["Close"])

    # Resample to 5-min if needed
    delta = df.index[1] - df.index[0]
    if delta.total_seconds() < 300:
        print(f"  Detected {int(delta.total_seconds())}s bars → resampling to 5-min")
        df = df.resample("5min").agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }).dropna(subset=["Close"])
        print(f"  After resample: {len(df):,} rows")

    # Use last 80K bars (recent ~9 months) for speed
    df = df.iloc[-80000:].copy()
    print(f"  Using last {len(df):,} bars")

    # 2. Compute squeeze indicators
    print(f"\n[2/5] Computing squeeze indicators...")
    df_ind = calculate_futures_squeeze(df)

    # Add volume_spike — now computed live by calculate_futures_squeeze
    # (no longer depends on Parquet pre-computed field)
    if "volume_spike" not in df_ind.columns:
        _vol_ma20 = df_ind["Volume"].rolling(20, min_periods=5).mean().fillna(method="ffill").fillna(df_ind["Volume"])
        df_ind["volume_spike"] = (df_ind["Volume"] / _vol_ma20.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(1.0)
    print(f"  volume_spike: min={df_ind['volume_spike'].min():.2f} max={df_ind['volume_spike'].max():.2f}")
    print(f"  volume_spike >= 1.5: {(df_ind['volume_spike']>=1.5).sum():,} / {len(df_ind):,}")

    # Add required BB columns for breakout detection
    bb_length, bb_std = 20, 2.0
    basis = df_ind["Close"].rolling(bb_length).mean()
    std = df_ind["Close"].rolling(bb_length).std(ddof=0)
    df_ind["bb_up"] = basis + std * bb_std
    df_ind["bb_low"] = basis - std * bb_std
    df_ind["bb_mid"] = basis
    df_ind["bear_breakout"] = (
        df_ind["sqz_on"]
        & (df_ind["Close"] < df_ind["bb_low"])
        & (df_ind.get("bear_breakout_strength", pd.Series(0, index=df_ind.index)) > 0)
    )
    df_ind["bull_breakout"] = (
        df_ind["sqz_on"]
        & (df_ind["Close"] > df_ind["bb_up"])
        & (df_ind.get("breakout_strength", pd.Series(0, index=df_ind.index)) > 0)
    )
    df_ind["squeeze_release"] = df_ind["fired"]
    df_ind["bars_since_open"] = df_ind.groupby("trading_day").cumcount() + 1

    # Drop warmup
    warmup = 60
    df_ind = df_ind.iloc[warmup:].copy()
    print(f"  After warmup: {len(df_ind):,} rows")

    # 3. Classify every bar
    print(f"\n[3/5] Classifying {len(df_ind):,} bars...")
    ts_list, regime_list, bias_list, conf_list = [], [], [], []
    reasons_list = []

    for idx, row in df_ind.iterrows():
        try:
            result = classify_futures_bar_regime(row, REGIME_CONFIG, session_regime=None)
            ts_list.append(idx)
            regime_list.append(result.regime)
            bias_list.append(result.bias)
            conf_list.append(result.confidence)
            reasons_list.append(result.reasons)
        except Exception as e:
            continue

    df_labels = pd.DataFrame({
        "regime": regime_list,
        "bias": bias_list,
        "confidence": conf_list,
        "reasons": reasons_list,
    }, index=ts_list) if ts_list else pd.DataFrame()

    if df_labels.empty:
        print("  ERROR: no classifications")
        return

    # Merge
    df_merged = df_ind[["Close", "High", "Low", "volume_spike", "breakout_strength_atr"]].join(df_labels, how="inner")
    print(f"  Merged: {len(df_merged):,} rows")

    # Print regime distribution
    print("\n  Regime Distribution:")
    for (r, b), cnt in sorted(df_merged.groupby(["regime", "bias"]).size().items(), key=lambda x: -x[1]):
        print(f"    {r:<12s} {b:<8s}: {cnt:>7,d} ({cnt/len(df_merged)*100:5.1f}%)")

    # 4. Forward statistics
    print(f"\n[4/5] Computing forward statistics...")
    stats_by_group: dict[str, ForwardStats] = {}
    group_regime_next: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for lookahead in FORWARD_BARS:
        forward_close = df_merged["Close"].shift(-lookahead)
        forward_high = df_merged["High"].rolling(lookahead, min_periods=1).max().shift(-lookahead)
        forward_low = df_merged["Low"].rolling(lookahead, min_periods=1).min().shift(-lookahead)
        entry_close = df_merged["Close"]

        for (regime, bias), grp in df_merged.groupby(["regime", "bias"]):
            key = f"{regime} / {bias}"
            n = len(grp)
            if n < 10:
                continue

            idx = grp.index
            fc = forward_close.loc[idx]
            fh = forward_high.loc[idx]
            fl = forward_low.loc[idx]
            ec = entry_close.loc[idx]

            ret = (fc / ec - 1.0).dropna()
            direction = 1 if bias == "LONG" else (-1 if bias == "SHORT" else 0)
            signed_ret = ret * direction if direction != 0 else ret.abs()

            win_rate = (signed_ret > 0).sum() / max(len(signed_ret.dropna()), 1)
            mfe = ((fh / ec - 1.0) * direction).dropna().mean()
            mae = ((fl / ec - 1.0) * direction).dropna().mean()

            if key not in stats_by_group:
                stats_by_group[key] = ForwardStats(
                    label=regime, bias=bias, n=n,
                    mean_returns={}, win_rates={}, mean_mfe={}, mean_mae={},
                    next_regime_dist={},
                )
            stats_by_group[key].mean_returns[lookahead] = signed_ret.mean() if len(signed_ret) > 0 else 0.0
            stats_by_group[key].win_rates[lookahead] = win_rate
            stats_by_group[key].mean_mfe[lookahead] = mfe
            stats_by_group[key].mean_mae[lookahead] = mae

    # Regime transition matrix
    df_merged["next_regime"] = df_merged["regime"].shift(-1)
    for regime, grp in df_merged.groupby("regime"):
        total = len(grp)
        for next_r, cnt in grp["next_regime"].value_counts().items():
            group_regime_next[regime][next_r] = cnt / total

    for key in stats_by_group:
        stats_by_group[key].next_regime_dist = dict(group_regime_next.get(stats_by_group[key].label, {}))

    # ─── Report ───────────────────────────────────────────────────────────
    print(f"\n[5/5] Generating report...")
    lines = []
    lines.append("=" * 80)
    lines.append("  Futures Regime Classification — Historical Validation Report V2")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  Data: TXFR1 5-min bars, {len(df_merged):,} classified bars")
    lines.append(f"  Period: {df_merged.index[0]} → {df_merged.index[-1]}")
    lines.append("=" * 80)
    lines.append("")

    # Distribution
    lines.append("─── 1. Regime Distribution ───")
    lines.append(f"{'Regime / Bias':<28s} {'Count':>8s} {'%':>6s}")
    lines.append("-" * 44)
    total_bars = len(df_merged)
    for (r, b), cnt in sorted(
        df_merged.groupby(["regime", "bias"]).size().items(), key=lambda x: -x[1]
    ):
        lines.append(f"  {r:<16s} {b:<8s} {cnt:>8,d} {cnt/total_bars*100:>5.1f}%")
    lines.append("")

    # Forward returns
    lines.append("─── 2. Forward Return by Regime/Bias (signed: + = correct direction) ───")
    header = f"{'Label':<28s} {'N':>7s}"
    for lb in FORWARD_BARS:
        header += f" {'R'+str(lb):>10s}"
    for lb in FORWARD_BARS:
        header += f" {'W'+str(lb):>7s}"
    lines.append(header)
    lines.append("-" * (28 + 7 + 10 * len(FORWARD_BARS) + 7 * len(FORWARD_BARS)))

    for key in sorted(stats_by_group.keys()):
        s = stats_by_group[key]
        row = f"  {s.label:<16s} {s.bias:<8s} {s.n:>7,d}"
        for lb in FORWARD_BARS:
            r = s.mean_returns.get(lb, 0)
            row += f" {r*100:>+9.3f}%"
        for lb in FORWARD_BARS:
            wr = s.win_rates.get(lb, 0)
            row += f" {wr*100:>6.1f}%"
        lines.append(row)
    lines.append("")

    # MFE/MAE
    lines.append("─── 3. MFE / MAE (5-bar lookahead) ───")
    lines.append(f"{'Label':<28s} {'MFE':>10s} {'MAE':>10s} {'MFE/MAE':>10s}")
    lines.append("-" * 60)
    for key in sorted(stats_by_group.keys()):
        s = stats_by_group[key]
        mfe = s.mean_mfe.get(5, 0)
        mae = s.mean_mae.get(5, 0)
        ratio = abs(mfe / mae) if abs(mae) > 1e-10 else float("inf")
        lines.append(f"  {s.label:<16s} {s.bias:<8s} {mfe*100:>+9.3f}% {mae*100:>+9.3f}% {ratio:>8.2f}x")
    lines.append("")

    # Transition matrix
    lines.append("─── 4. Regime Transition Probabilities ───")
    regimes_order = ["TREND", "BEAR", "SQUEEZE", "WEAK", "TRANSITION", "STRETCHED"]
    header = f"{'From → To':<20s}"
    for r in regimes_order:
        header += f" {r:>12s}"
    lines.append(header)
    lines.append("-" * (20 + 12 * len(regimes_order)))
    for r in regimes_order:
        if r not in group_regime_next:
            continue
        row = f"  {r:<18s}"
        for r2 in regimes_order:
            pct = group_regime_next[r].get(r2, 0) * 100
            row += f" {pct:>11.1f}%"
        lines.append(row)
    lines.append("")

    # ── MOMENTUM_OVERRIDE deep-dive (REMOVED 2026-05-12) ──
    lines.append("─── 5. MOMENTUM_OVERRIDE Deep-Dive ───")
    lines.append("  [REMOVED 2026-05-12] Data proved it reduced predictive power:")
    lines.append("    TRANSITION/LONG  5-bar WR=48.2% vs WEAK/LONG  50.4%")
    lines.append("    TRANSITION/SHORT 5-bar WR=42.0% vs WEAK/SHORT 45.9%")
    lines.append("  All previously TRANSITION bars now fall through to WEAK.")
    lines.append("  See V2 report section 5.")
    lines.append("")

    # STRETCHED
    lines.append("─── 6. STRETCHED Mean Reversion ───")
    for bias in ("LONG", "SHORT"):
        key = f"STRETCHED / {bias}"
        if key not in stats_by_group:
            continue
        s = stats_by_group[key]
        lines.append(f"  STRETCHED/{bias}: {s.n:,} bars")
        for lb in FORWARD_BARS:
            r = s.mean_returns.get(lb, 0)
            wr = s.win_rates.get(lb, 0)
            lines.append(f"    {lb}-bar: R={r*100:+.3f}% WR={wr*100:.1f}%")
    lines.append("")

    # Volume spike impact
    lines.append("─── 7. Volume Spike Impact (on WEAK regime) ───")
    # Split WEAK bars by volume_spike level
    df_merged["vol_high"] = df_merged["volume_spike"] >= 1.5
    for bias in ("LONG", "SHORT"):
        for vol_label, vol_flag in [("vol>=1.5", True), ("vol<1.5", False)]:
            subset = df_merged[(df_merged["regime"] == "WEAK") & (df_merged["bias"] == bias) & (df_merged["vol_high"] == vol_flag)]
            n = len(subset)
            if n < 30:
                continue
            ec = subset["Close"]
            fc = subset["Close"].shift(-5)
            fh = subset["High"].rolling(5, min_periods=1).max().shift(-5)
            fl = subset["Low"].rolling(5, min_periods=1).min().shift(-5)
            ret = (fc / ec - 1.0).dropna()
            direction = 1 if bias == "LONG" else -1
            sr = ret * direction
            wr = (sr > 0).sum() / max(len(sr.dropna()), 1)
            mfe = ((fh / ec - 1.0) * direction).dropna().mean()
            mae = ((fl / ec - 1.0) * direction).dropna().mean()
            ratio = abs(mfe / mae) if abs(mae) > 1e-10 else float("inf")
            lines.append(f"  WEAK/{bias:<6s} {vol_label:<10s}: n={n:>6d} R={sr.mean()*100:+.3f}% WR={wr*100:.1f}% MFE/MAE={ratio:.2f}x")

    lines.append("")

    # ── Simulated WEAK Volume Gate Impact 1.5 ──
    lines.append("─── 8. Simulated WEAK Volume Gate (applied post-classification) ───")
    lines.append("  Only WEAK bars with volume_spike >= 1.5 pass through to strategies.")
    for bias in ("LONG", "SHORT"):
        passed = df_merged[(df_merged["regime"] == "WEAK") & (df_merged["bias"] == bias) & (df_merged["volume_spike"] >= 1.5)]
        blocked = df_merged[(df_merged["regime"] == "WEAK") & (df_merged["bias"] == bias) & (df_merged["volume_spike"] < 1.5)]
        lines.append(f"  WEAK/{bias}:")
        lines.append(f"    Passed (vol>=1.5): {len(passed):,} bars  Blocked (vol<1.5): {len(blocked):,} bars ({len(blocked)/max(len(passed)+len(blocked),1)*100:.0f}% filtered)")
        for label, subset in [("passed", passed), ("blocked", blocked)]:
            n = len(subset)
            if n < 10:
                continue
            ec = subset["Close"]
            fc = ec.shift(-5)
            fh = subset["High"].rolling(5, min_periods=1).max().shift(-5)
            fl = subset["Low"].rolling(5, min_periods=1).min().shift(-5)
            ret = (fc / ec - 1.0).dropna()
            direction = 1 if bias == "LONG" else -1
            sr = ret * direction
            wr = (sr > 0).sum() / max(len(sr.dropna()), 1)
            lines.append(f"      {label:>8s} (n={n:>5d}): 5-bar R={sr.mean()*100:+.3f}% WR={wr*100:.1f}%")

    # Print & save
    report = "\n".join(lines)
    print("\n" + report)
    with open(REPORT_PATH, "w") as f:
        f.write(report)
    print(f"\n  Report saved to: {REPORT_PATH}")


if __name__ == "__main__":
    main()
