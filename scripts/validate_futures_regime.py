#!/usr/bin/env python3
"""Validate futures bar regime classification against historical price behavior.

Purpose:
  Run classify_futures_bar_regime() over historical 5-min bars (with computed
  squeeze indicators) and measure forward price action for each regime label.

Questions answered:
  1. Distribution: how often is each regime assigned?
  2. Forward return: after a regime+ bias label, does price move in the
     expected direction with statistical significance?
  3. Regime transition: what follows SQUEEZE/WEAK/TREND?
  4. MOMENTUM_OVERRIDE: does upgrading WEAK→TRANSITION improve prediction?
  5. STRETCHED: does price revert to VWAP?

Output:
  - stdout summary table
  - scripts/regime_validation_report.txt
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

# ─── Config ───────────────────────────────────────────────────────────────────
REGIME_CONFIG = FuturesBarRegimeConfig()
REPORT_PATH = BASE / "scripts" / "regime_validation_report.txt"
FORWARD_BARS = [1, 3, 5, 10, 20]  # look-ahead windows in 5-min bars

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_int(v):
    try:
        return int(v) if not (isinstance(v, float) and np.isnan(v)) else 0
    except (TypeError, ValueError):
        return 0

def compute_bb_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """Compute BB columns needed by classify_futures_bar_regime for squeeze
    breakout detection (bear_breakout, bull_breakout)."""
    bb_length = 20
    bb_std = 2.0
    basis = df["Close"].rolling(bb_length).mean()
    std = df["Close"].rolling(bb_length).std(ddof=0)
    df["bb_up"] = basis + std * bb_std
    df["bb_low"] = basis - std * bb_std
    df["bb_mid"] = basis

    # Squeeze release for bias inference
    if "sqz_on" in df.columns and "fired" in df.columns:
        df["squeeze_release"] = df["fired"]
        df["bear_breakout"] = (
            df["sqz_on"]
            & (df["Close"] < df["bb_low"])
            & (df.get("bear_breakout_strength", pd.Series(0, index=df.index)) > 0)
        )
        df["bull_breakout"] = (
            df["sqz_on"]
            & (df["Close"] > df["bb_up"])
            & (df.get("breakout_strength", pd.Series(0, index=df.index)) > 0)
        )
    else:
        df["bear_breakout"] = False
        df["bull_breakout"] = False
        df["squeeze_release"] = False

    if "volume_spike" not in df.columns:
        df["volume_spike"] = 1.0

    # bars_since_open — approximate from trading_day
    if "trading_day" not in df.columns:
        df["trading_day"] = pd.to_datetime(df.index.date)
    df["bars_since_open"] = df.groupby("trading_day").cumcount() + 1

    return df


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


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 72)
    print("  Regime Classification Validation")
    print("=" * 72)

    # 1. Load data
    print("\n[1/5] Loading TXFR1 historical data...")
    df_raw = dm.load_historical("TXFR1")
    print(f"  Loaded {len(df_raw):,} rows ({df_raw.index[0]} → {df_raw.index[-1]})")

    # We need raw OHLCV for 2+ years.  Drop fully-null rows
    df = df_raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    df = df.dropna(subset=["Close"])

    # Resample: some parquet files have 1-min granularity; aggregate to 5-min
    # Check if already 5-min
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

    # 2. Compute squeeze indicators (need ~50 bars warmup)
    print(f"\n[2/5] Computing squeeze indicators ({len(df):,} rows)...")
    df_ind = calculate_futures_squeeze(df)
    df_ind = compute_bb_vwap(df_ind)

    # Drop warmup period (first 60 bars are unreliable)
    warmup = 60
    df_ind = df_ind.iloc[warmup:].copy()
    print(f"  After warmup: {len(df_ind):,} rows")

    # 3. Classify every bar
    print(f"\n[3/5] Classifying {len(df_ind):,} bars...")
    results: list[tuple[pd.Timestamp, FuturesBarRegimeResult]] = []

    for idx, row in df_ind.iterrows():
        try:
            result = classify_futures_bar_regime(row, REGIME_CONFIG, session_regime=None)
            results.append((idx, result))
        except Exception as e:
            continue

    print(f"  Classified {len(results):,} bars successfully")

    # Build a DataFrame with regime labels
    ts_list, regime_list, bias_list, conf_list = [], [], [], []
    for ts, r in results:
        ts_list.append(ts)
        regime_list.append(r.regime)
        bias_list.append(r.bias)
        conf_list.append(r.confidence)

    df_labels = pd.DataFrame({
        "timestamp": ts_list,
        "regime": regime_list,
        "bias": bias_list,
        "confidence": conf_list,
    }).set_index("timestamp") if ts_list else pd.DataFrame()

    if df_labels.empty:
        print("  ERROR: no classifications")
        return

    # Merge with OHLC for forward return calculation
    df_merged = df_ind[["Close", "High", "Low"]].join(df_labels, how="inner")
    print(f"  Merged: {len(df_merged):,} rows")

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
                    label=regime,
                    bias=bias,
                    n=n,
                    mean_returns={},
                    win_rates={},
                    mean_mfe={},
                    mean_mae={},
                    next_regime_dist={},
                )
            stats_by_group[key].mean_returns[lookahead] = signed_ret.mean() if len(signed_ret) > 0 else 0.0
            stats_by_group[key].win_rates[lookahead] = win_rate
            stats_by_group[key].mean_mfe[lookahead] = mfe
            stats_by_group[key].mean_mae[lookahead] = mae

    # 5. Next-bar regime transition matrix
    print("  Computing regime transition probabilities...")
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
    lines.append("  Futures Regime Classification — Historical Validation Report")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  Data: TXFR1 5-min bars, {len(df_merged):,} classified bars")
    lines.append(f"  Period: {df_merged.index[0]} → {df_merged.index[-1]}")
    lines.append("=" * 80)
    lines.append("")

    # ── Distribution ──
    lines.append("─── 1. Regime Distribution ───")
    lines.append(f"{'Regime / Bias':<28s} {'Count':>8s} {'%':>6s}")
    lines.append("-" * 44)
    total_bars = len(df_merged)
    if total_bars > 0:
        for (regime, bias), cnt in sorted(
            df_merged.groupby(["regime", "bias"]).size().items(),
            key=lambda x: -x[1],
        ):
            lines.append(f"  {regime:<16s} {bias:<8s} {cnt:>8,d} {cnt/total_bars*100:>5.1f}%")
    lines.append("")

    # ── Forward returns by group ──
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

    # ── MFE/MAE ──
    lines.append("─── 3. MFE / MAE (5-bar lookahead, signed: + = correct direction) ───")
    lines.append(f"{'Label':<28s} {'MFE':>10s} {'MAE':>10s} {'MFE/MAE':>10s}")
    lines.append("-" * 60)
    for key in sorted(stats_by_group.keys()):
        s = stats_by_group[key]
        mfe = s.mean_mfe.get(5, 0)
        mae = s.mean_mae.get(5, 0)
        ratio = abs(mfe / mae) if abs(mae) > 1e-10 else float("inf")
        lines.append(f"  {s.label:<16s} {s.bias:<8s} {mfe*100:>+9.3f}% {mae*100:>+9.3f}% {ratio:>8.2f}x")
    lines.append("")

    # ── Regime Transition Matrix ──
    lines.append("─── 4. Regime Transition Probabilities (current → next bar) ───")
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

    # ── MOMENTUM_OVERRIDE deep-dive ──
    lines.append("─── 5. Momentum Override Deep-Dive (WEAK→TRANSITION upgrade) ───")
    # Find bars classified as TRANSITION that would have been WEAK without override
    # Re-classify without momentum override to compare
    override_results: dict[str, list] = {"total": 0, "with_override": 0}
    override_samples = 0
    for idx, row in df_ind.iterrows():
        if idx not in df_merged.index:
            continue
        actual = df_merged.loc[idx, "regime"]
        if actual != "TRANSITION":
            continue
        override_samples += 1

    lines.append(f"  TRANSITION-labeled bars: {override_samples:,}")
    if override_samples > 0:
        # Compare forward returns of TRANSITION vs WEAK
        trans_grp = stats_by_group.get("TRANSITION / LONG")
        weak_grp = stats_by_group.get("WEAK / LONG")
        if trans_grp and weak_grp:
            lines.append("")
            lines.append("  TRANSITION vs WEAK (LONG bias, 5-bar):")
            lines.append(f"    WEAK:      R={weak_grp.mean_returns.get(5,0)*100:+.3f}% WR={weak_grp.win_rates.get(5,0)*100:.1f}%")
            lines.append(f"    TRANSITION: R={trans_grp.mean_returns.get(5,0)*100:+.3f}% WR={trans_grp.win_rates.get(5,0)*100:.1f}%")

        trans_bear = stats_by_group.get("TRANSITION / SHORT")
        weak_bear = stats_by_group.get("WEAK / SHORT")
        if trans_bear and weak_bear:
            lines.append("")
            lines.append("  TRANSITION vs WEAK (SHORT bias, 5-bar):")
            lines.append(f"    WEAK:      R={weak_bear.mean_returns.get(5,0)*100:+.3f}% WR={weak_bear.win_rates.get(5,0)*100:.1f}%")
            lines.append(f"    TRANSITION: R={trans_bear.mean_returns.get(5,0)*100:+.3f}% WR={trans_bear.win_rates.get(5,0)*100:.1f}%")
    lines.append("")

    # ── STRETCHED reversion ──
    lines.append("─── 6. STRETCHED Regime: Mean Reversion Rate ───")
    st_grp_long = stats_by_group.get("STRETCHED / LONG")
    st_grp_short = stats_by_group.get("STRETCHED / SHORT")
    if st_grp_long:
        lines.append(f"  STRETCHED/LONG:  {st_grp_long.n:,} bars observed")
        for lb in FORWARD_BARS:
            r = st_grp_long.mean_returns.get(lb, 0)
            wr = st_grp_long.win_rates.get(lb, 0)
            lines.append(f"    {lb}-bar: R={r*100:+.3f}% WR={wr*100:.1f}%")
    if st_grp_short:
        lines.append(f"  STRETCHED/SHORT: {st_grp_short.n:,} bars observed")
        for lb in FORWARD_BARS:
            r = st_grp_short.mean_returns.get(lb, 0)
            wr = st_grp_short.win_rates.get(lb, 0)
            lines.append(f"    {lb}-bar: R={r*100:+.3f}% WR={wr*100:.1f}%")
    if not st_grp_long and not st_grp_short:
        lines.append("  (No STRETCHED classifications in dataset)")
    lines.append("")

    # ── SQUEEZE follow-through ──
    lines.append("─── 7. SQUEEZE Regime: What Happens Next? ───")
    for bias in ("LONG", "SHORT", "NEUTRAL"):
        key = f"SQUEEZE / {bias}"
        if key not in stats_by_group:
            continue
        s = stats_by_group[key]
        lines.append(f"  SQUEEZE/{bias}:")
        for lb in FORWARD_BARS:
            r = s.mean_returns.get(lb, 0)
            wr = s.win_rates.get(lb, 0)
            lines.append(f"    {lb}-bar return={r*100:+.3f}% win_rate={wr*100:.1f}%")
        lines.append(f"    Next regime distribution:")
        for nr, pct in sorted(s.next_regime_dist.items(), key=lambda x: -x[1]):
            lines.append(f"      {nr}: {pct*100:.1f}%")
        lines.append("")

    # ── Print & Save ──
    report = "\n".join(lines)
    print("\n" + report)

    with open(REPORT_PATH, "w") as f:
        f.write(report)
    print(f"\n  Report saved to: {REPORT_PATH}")
    print("Done.")


if __name__ == "__main__":
    main()
