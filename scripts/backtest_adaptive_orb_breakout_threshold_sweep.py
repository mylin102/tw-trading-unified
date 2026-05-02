#!/usr/bin/env python3
"""
Breakout Strength Threshold Sweep — regime classifier effect.

Computes fresh indicators directly from OHLC columns, bypassing
pre-computed (broken) parquet indicator columns.
"""
import sys
sys.path.insert(0, ".")

import logging
logging.basicConfig(level=logging.WARNING)
for name in ("regime", "core.futures_bar_regime"):
    logging.getLogger(name).setLevel(logging.WARNING)

import pandas as pd
import numpy as np

from core.futures_bar_regime import classify_futures_bar_regime, FuturesBarRegimeConfig

THRESHOLDS = [0.15, 0.20, 0.25, 0.30]
TICKER = "TXFR1"


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all needed indicators from raw OHLC. Returns a fresh DataFrame."""
    d = pd.DataFrame(index=df.index)
    d["Close"] = df["Close"].values
    d["High"] = df["High"].values
    d["Low"] = df["Low"].values
    d["Volume"] = df["Volume"].values

    # ATR (14-period Wilder's)
    high_low = d["High"] - d["Low"]
    high_pc = (d["High"] - d["Close"].shift(1)).abs()
    low_pc = (d["Low"] - d["Close"].shift(1)).abs()
    tr = pd.concat([high_low, high_pc, low_pc], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()

    # Breakout strength: (Close - High20.shift(1)) / ATR
    high20_s1 = d["High"].rolling(20).max().shift(1)
    bs = (d["Close"] - high20_s1) / d["atr"].replace(0, np.nan)
    d["breakout_strength"] = bs.fillna(0).clip(lower=0)

    # Bear breakout: (Low20.shift(1) - Close) / ATR
    low20_s1 = d["Low"].rolling(20).min().shift(1)
    bbs = (low20_s1 - d["Close"]) / d["atr"].replace(0, np.nan)
    d["bear_breakout_strength"] = bbs.fillna(0).clip(lower=0)

    # VWAP (rolling 20-bar)
    typical = (d["High"] + d["Low"] + d["Close"]) / 3
    d["vwap"] = typical.rolling(20, min_periods=1).mean()

    # ADX (14-period)
    d["adx"] = _compute_adx(d)

    # Trend strength
    d["trend_strength_raw"] = d["Close"].ewm(span=20).mean().pct_change(5).fillna(0)

    # Volume spike
    d["volume_spike"] = (d["Volume"] / d["Volume"].rolling(20).mean().replace(0, np.nan)).fillna(1.0)

    # Price vs VWAP
    d["price_vs_vwap"] = ((d["Close"] - d["vwap"]) / d["vwap"].replace(0, np.nan)).fillna(0)

    # EMA fast/slow
    d["ema_fast"] = d["Close"].ewm(span=8).mean()
    d["ema_slow"] = d["Close"].ewm(span=21).mean()

    # Session regime heuristic
    d["session_regime"] = "NORMAL"
    d.loc[d["adx"] >= 25, "session_regime"] = "TRENDING"
    d.loc[d["adx"] < 15, "session_regime"] = "SQUEEZE"

    return d


def _compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    a = tr.rolling(period, min_periods=1).mean().replace(0, np.nan)
    pdi = 100 * (plus_dm.rolling(period, min_periods=1).mean() / a)
    mdi = 100 * (minus_dm.rolling(period, min_periods=1).mean() / a)
    dx = 100 * ((pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan))
    return dx.rolling(period, min_periods=1).mean().fillna(0)


def run_sweep(d: pd.DataFrame) -> pd.DataFrame:
    """Run regime classification for each threshold."""
    results = []
    total = len(d)
    cfg_template = FuturesBarRegimeConfig()
    cfg_default = FuturesBarRegimeConfig()  # for reference

    for thresh in THRESHOLDS:
        cfg = FuturesBarRegimeConfig(
            breakout_strength_trend_threshold=thresh,
            bear_breakout_strength_trend_threshold=thresh,
            trend_regime_threshold_mult=cfg_template.trend_regime_threshold_mult,
            squeeze_regime_threshold_mult=cfg_template.squeeze_regime_threshold_mult,
        )

        counts = {"TREND": 0, "BEAR": 0, "WEAK": 0, "SQUEEZE": 0, "STRETCHED": 0}
        bs_ok, bs_fail = 0, 0

        trades = []
        in_pos = False
        pos_side = ""
        entry_idx = 0
        entry_px = 0.0

        for i in range(total):
            row_d = d.iloc[i]
            result = classify_futures_bar_regime(
                row_d.to_dict(),
                config=cfg,
                session_regime=row_d.get("session_regime", "UNKNOWN"),
            )
            counts[result.regime] = counts.get(result.regime, 0) + 1

            bs_val = float(row_d.get("breakout_strength", 0))
            if bs_val > 1e-9:
                if result.regime in ("TREND", "BEAR"):
                    bs_ok += 1
                else:
                    bs_fail += 1

            close = float(row_d["Close"])
            if in_pos:
                pnl_pts = (close - entry_px) * (1 if pos_side == "LONG" else -1)
                if pnl_pts <= -60 or pnl_pts >= 200:
                    trades.append({"pnl_pts": pnl_pts, "bars": i - entry_idx})
                    in_pos = False
                    continue
                else:
                    continue

            if not in_pos and result.regime == "TREND" and result.bias in ("LONG", "SHORT"):
                in_pos = True
                pos_side = result.bias
                entry_idx = i
                entry_px = close

        if in_pos and total > entry_idx:
            close = float(d.iloc[-1]["Close"])
            pnl_pts = (close - entry_px) * (1 if pos_side == "LONG" else -1)
            trades.append({"pnl_pts": pnl_pts, "bars": total - entry_idx})

        wins = [t for t in trades if t["pnl_pts"] > 0]
        losses = [t for t in trades if t["pnl_pts"] <= 0]
        gross_w = sum(t["pnl_pts"] for t in wins)
        gross_l = abs(sum(t["pnl_pts"] for t in losses))
        pf = gross_w / gross_l if gross_l > 0 else (gross_w if gross_w > 0 else 0)
        wr = len(wins) / len(trades) * 100 if trades else 0

        results.append({
            "threshold": thresh,
            "trend_mult": cfg.trend_regime_threshold_mult,
            "effective_trend_min": round(thresh * cfg.trend_regime_threshold_mult, 3),
            "total_bars": total,
            "TREND": counts["TREND"],
            "BEAR": counts["BEAR"],
            "WEAK": counts["WEAK"],
            "SQUEEZE": counts["SQUEEZE"],
            "STRETCHED": counts["STRETCHED"],
            "trend_pct": round(counts["TREND"] / total * 100, 2),
            "bear_pct": round(counts["BEAR"] / total * 100, 2),
            "bs_hit_rate_pct": round(bs_ok / (bs_ok + bs_fail) * 100, 1) if (bs_ok + bs_fail) else 0,
            "n_trades": len(trades),
            "pf": round(pf, 2),
            "wr_pct": round(wr, 1),
        })
        print(f"  thresh={thresh}: T={counts['TREND']} B={counts['BEAR']} W={counts['WEAK']} "
              f"Sq={counts['SQUEEZE']} St={counts['STRETCHED']} "
              f"trades={len(trades)} PF={pf:.2f} WR={wr:.1f}%")

    return pd.DataFrame(results)


def main():
    print("=" * 70)
    print("📊 Breakout Strength Threshold Sweep — Regime Classifier")
    print("=" * 70)

    from core.data_manager import data_manager
    df = data_manager.load_historical(TICKER)
    if df.empty:
        print(f"❌ No data for {TICKER}")
        sys.exit(1)
    print(f"✅ Raw data: {len(df)} 5m bars ({df.index[0].date()} → {df.index[-1].date()})")

    # Compute fresh indicators from OHLC (bypass broken parquet indicators)
    d = compute_indicators(df)

    # Drop warmup
    d = d.iloc[30:].copy()
    print(f"📉 Fresh indicators computed on {len(d)} bars (after warmup)")

    # Quick stats check
    bs_pos = (d["breakout_strength"] > 0).sum()
    print(f"   bars with bs>0: {bs_pos}")
    vs_high = (d["volume_spike"] >= 1.5).sum()
    print(f"   bars with vol_spike>=1.5: {vs_high}")

    print("\n⚙️  Sweeping...")
    results = run_sweep(d)

    print("\n📋 RESULTS")
    print("=" * 70)
    print(results.to_string(index=False))

    import os
    out_dir = "output/backtest/adaptive_orb_threshold_sweep"
    os.makedirs(out_dir, exist_ok=True)
    results.to_csv(f"{out_dir}/sweep_results.csv", index=False)
    print(f"\n💾 Saved to {out_dir}/sweep_results.csv")

    if results["pf"].max() > 0:
        best = results.loc[results["pf"].idxmax()]
        print(f"\n🏆 RECOMMENDED: threshold={best['threshold']} (PF={best['pf']} WR={best['wr_pct']}%)")


if __name__ == "__main__":
    main()
