#!/usr/bin/env python3
"""
adaptive_orb v1 vs v1.5 vs v2 — 三強對決回測

v1:   ORB range + regime-triggered entry, exit -60/+200
v1.5: v1 entry + ATR breakout confirmation gate (structure + behavior), same exit
v2:   ATR-normalized scout/scale, tight 2x/4x ATR exit

Metrics: CAGR, PF, MFE/MAE, Avg Hold, Max DD, Net PF (incl fees)
"""
import sys
sys.path.insert(0, ".")

import logging
logging.basicConfig(level=logging.WARNING)
for name in ("regime", "core.futures_bar_regime", "data_manager"):
    logging.getLogger(name).setLevel(logging.WARNING)

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass

from core.data_manager import data_manager
from core.futures_bar_regime import classify_futures_bar_regime, FuturesBarRegimeConfig

# ═══ Config ═══
TICKER = "TXFR1"
INITIAL_CAPITAL = 1_000_000
POINT_VALUE = 200
OUT_DIR = Path("output/backtest/adaptive_orb_v1_vs_v2")
OUT_DIR.mkdir(parents=True, exist_ok=True)

ATR_FLOOR_PCT = 0.0015
MIN_VOLUME_SPIKE = 1.5
EARLY_THRESHOLD = 0.15
CONFIRMED_THRESHOLD = 0.25
FEE_PER_TRADE = 8 * POINT_VALUE  # ~8 pts round-trip on TMF


# ═══ Data ═══
def load_and_prepare() -> pd.DataFrame:
    df = data_manager.load_historical(TICKER)
    if df.empty:
        print(f"❌ No data for {TICKER}")
        sys.exit(1)
    print(f"✅ Loaded {len(df)} bars ({df.index[0].date()} → {df.index[-1].date()})")

    for c_l, c_u in [("open", "Open"), ("high", "High"), ("low", "Low"),
                      ("close", "Close"), ("volume", "Volume")]:
        if c_u not in df.columns and c_l in df.columns:
            df[c_u] = df[c_l]

    d = pd.DataFrame(index=df.index)
    d["Close"] = df["Close"].values
    d["High"] = df["High"].values
    d["Low"] = df["Low"].values
    d["Volume"] = df["Volume"].values

    # ATR (14)
    tr = pd.concat([
        d["High"] - d["Low"],
        (d["High"] - d["Close"].shift(1)).abs(),
        (d["Low"] - d["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()

    # Breakout strength (ATR-normalised with floor)
    high20_s1 = d["High"].rolling(20).max().shift(1)
    atr_floor = d["atr"].clip(lower=d["Close"] * ATR_FLOOR_PCT)
    d["breakout_strength"] = ((d["Close"] - high20_s1) / atr_floor.replace(0, np.nan)).fillna(0).clip(lower=0)
    d["bear_breakout_strength"] = (
        ((d["Low"].rolling(20).min().shift(1) - d["Close"]) / atr_floor.replace(0, np.nan))
        .fillna(0).clip(lower=0)
    )

    # VWAP
    typical = (d["High"] + d["Low"] + d["Close"]) / 3
    d["vwap"] = typical.rolling(20, min_periods=1).mean()
    d["price_vs_vwap"] = ((d["Close"] - d["vwap"]) / d["vwap"].replace(0, np.nan)).fillna(0)

    # ADX (14)
    d["adx"] = _adx(d)
    d["trend_strength_raw"] = d["Close"].ewm(span=20).mean().pct_change(5).fillna(0)
    d["volume_spike"] = (d["Volume"] / d["Volume"].rolling(20).mean().replace(0, np.nan)).fillna(1.0)

    # EMA fast/slow
    d["ema_fast"] = d["Close"].ewm(span=8).mean()
    d["ema_slow"] = d["Close"].ewm(span=21).mean()

    # Bias columns needed by regime classifier
    for col in ["bull_align", "bear_align", "bullish_align", "bearish_align",
                "opening_bullish", "opening_bearish", "in_pb_zone",
                "in_bull_pb_zone", "in_bear_pb_zone", "sqz_on"]:
        d[col] = False
    d["volume"] = d["Volume"].values

    # Session regime heuristic
    d["session_regime"] = "NORMAL"
    d.loc[d["adx"] >= 25, "session_regime"] = "TRENDING"
    d.loc[d["adx"] < 15, "session_regime"] = "SQUEEZE"

    return d


def _adx(df, period=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    up, down = h.diff(), -l.diff()
    p = ((up > down) & (up > 0)) * up
    m = ((down > up) & (down > 0)) * down
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    a = tr.rolling(period, min_periods=1).mean().replace(0, np.nan)
    pdi = 100 * (p.rolling(period, min_periods=1).mean() / a)
    mdi = 100 * (m.rolling(period, min_periods=1).mean() / a)
    dx = 100 * ((pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan))
    return dx.rolling(period, min_periods=1).mean().fillna(0)


# ═══ Trade data ═══
@dataclass
class Trade:
    strategy: str
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    pnl_pts: float
    bars_held: int
    mfe: float = 0.0
    mae: float = 0.0

    @property
    def pnl_cash(self) -> float:
        return self.pnl_pts * POINT_VALUE


# ═══ v1: regime-triggered ORB entry ═══
def _is_trend(row, cfg=None):
    if cfg is None:
        cfg = FuturesBarRegimeConfig()
    r = classify_futures_bar_regime(row.to_dict(), config=cfg,
                                     session_regime=row.get("session_regime", "NORMAL"))
    return r.regime in ("TREND", "BEAR")


def backtest_v1(d):
    trades = []
    in_pos = False
    entry_px = 0.0
    entry_idx = 0
    mfe = 0.0
    mae = 0.0
    for i in range(len(d)):
        row = d.iloc[i]
        close = float(row["Close"])
        if in_pos:
            px = close - entry_px
            mfe = max(mfe, px)
            mae = min(mae, px)
            if px <= -60 or px >= 200:
                trades.append(Trade("v1", entry_idx, i, entry_px, close, px, i - entry_idx, mfe, mae))
                in_pos = False
        else:
            if _is_trend(row):
                in_pos = True
                entry_px = close
                entry_idx = i
                mfe = 0.0
                mae = 0.0
    if in_pos and len(d) > entry_idx:
        close = float(d.iloc[-1]["Close"])
        trades.append(Trade("v1", entry_idx, len(d), entry_px, close,
                           close - entry_px, len(d) - entry_idx, mfe, mae))
    return trades


# ═══ v1.5: v1 entry + ATR breakout confirmation gate ═══
def _atr_confirmed(row, d, i):
    """ATR breakout confirmation gate: structure + behavior."""
    close = float(row["Close"])
    if i < 21:
        return True, "INSUFFICIENT_DATA"
    high20_prev = float(d["High"].iloc[:i + 1].rolling(20).max().shift(1).iloc[-1])
    if close <= high20_prev:
        return False, f"NO_STRUCTURE"
    vs = float(row.get("volume_spike", 0))
    vw = float(row.get("vwap", 0))
    if vs < MIN_VOLUME_SPIKE:
        return False, f"VOL_TOO_LOW"
    if vw > 0 and close <= vw:
        return False, f"VWAP_REJECT"
    return True, "OK"


def backtest_v15(d):
    trades = []
    in_pos = False
    entry_px = 0.0
    entry_idx = 0
    mfe = 0.0
    mae = 0.0
    for i in range(len(d)):
        row = d.iloc[i]
        close = float(row["Close"])
        if in_pos:
            px = close - entry_px
            mfe = max(mfe, px)
            mae = min(mae, px)
            if px <= -60 or px >= 200:
                trades.append(Trade("v1.5", entry_idx, i, entry_px, close, px, i - entry_idx, mfe, mae))
                in_pos = False
        else:
            if _is_trend(row):
                ok, reason = _atr_confirmed(row, d, i)
                if ok:
                    in_pos = True
                    entry_px = close
                    entry_idx = i
                    mfe = 0.0
                    mae = 0.0
    if in_pos and len(d) > entry_idx:
        close = float(d.iloc[-1]["Close"])
        trades.append(Trade("v1.5", entry_idx, len(d), entry_px, close,
                           close - entry_px, len(d) - entry_idx, mfe, mae))
    return trades


# ═══ v2: ATR scout/scale (EARLY disabled, CONFIRMED only) ═══
def backtest_v2(d):
    trades = []
    in_pos = False
    entry_px = 0.0
    entry_idx = 0
    entry_atr = 0.0
    mfe = 0.0
    mae = 0.0
    for i in range(len(d)):
        row = d.iloc[i]
        close = float(row["Close"])
        if in_pos:
            px = close - entry_px
            mfe = max(mfe, px)
            mae = min(mae, px)
            sl = entry_px - 2.0 * entry_atr
            tp = entry_px + 4.0 * entry_atr
            if close <= sl or close >= tp or (i - entry_idx) >= 60:
                trades.append(Trade("v2", entry_idx, i, entry_px, close, px, i - entry_idx, mfe, mae))
                in_pos = False
        else:
            bs = float(row.get("breakout_strength", 0))
            vs = float(row.get("volume_spike", 0))
            vw = float(row.get("vwap", 0))
            atr_val = float(row.get("atr", 50))
            if bs >= CONFIRMED_THRESHOLD and vs >= 1.5 and (vw <= 0 or close > vw):
                in_pos = True
                entry_px = close
                entry_idx = i
                entry_atr = atr_val
                mfe = 0.0
                mae = 0.0
    if in_pos and len(d) > entry_idx:
        close = float(d.iloc[-1]["Close"])
        trades.append(Trade("v2", entry_idx, len(d), entry_px, close,
                           close - entry_px, len(d) - entry_idx, mfe, mae))
    return trades


# ═══ Metrics ═══
def compute(trades, label):
    n = len(trades)
    if n == 0:
        return {"strategy": label, "n": 0, "pf": 0, "net_pf": 0, "wr": 0,
                "total": 0, "cagr": 0, "max_dd": 0, "bars": 0,
                "mfe": 0, "mae": 0, "mfe_mae": 0}
    pnls = [t.pnl_cash for t in trades]
    wins = [t for t in trades if t.pnl_pts > 0]
    losses = [t for t in trades if t.pnl_pts <= 0]
    wr = len(wins) / n * 100
    gw = sum(t.pnl_cash for t in wins)
    gl = abs(sum(t.pnl_cash for t in losses))
    pf = gw / gl if gl > 0 else (gw if gw > 0 else 0)
    net_pf = (gw - FEE_PER_TRADE * len(wins)) / (gl + FEE_PER_TRADE * len(losses)) if gl + FEE_PER_TRADE * len(losses) > 0 else 0
    equity = np.cumsum(pnls)
    dd = np.maximum.accumulate(equity) - equity
    max_dd = dd.max()
    total = sum(pnls)
    cagr = ((1 + total / INITIAL_CAPITAL) ** (1 / 3.0) - 1) * 100
    net_total = total - FEE_PER_TRADE * n
    net_cagr = ((1 + net_total / INITIAL_CAPITAL) ** (1 / 3.0) - 1) * 100 if net_total > -INITIAL_CAPITAL else -100.0
    mfes = [t.mfe for t in trades if t.mfe != 0]
    maes = [t.mae for t in trades if t.mae != 0]
    avg_mfe = sum(mfes) / len(mfes) if mfes else 0
    avg_mae = abs(sum(maes) / len(maes)) if maes else 0
    return {
        "strategy": label, "n": n, "pf": round(pf, 2), "net_pf": round(net_pf, 3),
        "wr": round(wr, 1), "total": f"${total:,.0f}", "net_total": f"${net_total:,.0f}",
        "cagr": round(cagr, 2), "net_cagr": round(net_cagr, 2),
        "max_dd": f"${max_dd:,.0f}", "bars": round(sum(t.bars_held for t in trades) / n, 1),
        "mfe": round(avg_mfe, 1), "mae": round(avg_mae, 1),
        "mfe_mae": round(avg_mfe / avg_mae, 2) if avg_mae > 0 else 0,
    }


# ═══ main ═══
def main():
    print("=" * 65)
    print("📊 adaptive_orb v1 vs v1.5 vs v2")
    print("=" * 65)
    d = load_and_prepare()
    d = d.iloc[30:].copy()
    print(f"📉 {len(d)} bars\n")

    print("⚙️  v1 (ORB + regime)...", end=" ", flush=True)
    t1 = backtest_v1(d)
    print(f"{len(t1)} trades")

    print("⚙️  v1.5 (+ ATR confirmation gate)...", end=" ", flush=True)
    t15 = backtest_v15(d)
    print(f"{len(t15)} trades")

    print("⚙️  v2 (ATR CONFIRMED only)...", end=" ", flush=True)
    t2 = backtest_v2(d)
    print(f"{len(t2)} trades")

    m1 = compute(t1, "v1")
    m15 = compute(t15, "v1.5")
    m2 = compute(t2, "v2")

    print("\n" + "=" * 65)
    print("📋 三強對決")
    print("=" * 65)
    headers = ["metric", "v1", "v1.5", "v2"]
    fields = [("n_trades", "n"), ("PF", "pf"), ("Net PF", "net_pf"),
              ("Win Rate %", "wr"), ("Total PnL", "total"),
              ("Net PnL (aft fees)", "net_total"),
              ("CAGR %", "cagr"), ("Net CAGR %", "net_cagr"),
              ("Max DD", "max_dd"), ("Avg Bars", "bars"),
              ("Avg MFE", "mfe"), ("Avg MAE", "mae"), ("MFE/MAE", "mfe_mae")]
    for fname, fkey in fields:
        vals = [str(m1[fkey]), str(m15[fkey]), str(m2[fkey])]
        print(f"  {fname:15s} | {vals[0]:>12s} | {vals[1]:>12s} | {vals[2]:>12s}")

    # ── Delta analysis: v1.5 vs v1 ──
    print("\n" + "=" * 65)
    print("📋 v1.5 vs v1 Delta")
    print("=" * 65)
    delta_trades = m15["n"] - m1["n"]
    delta_pf = m15["pf"] - m1["pf"]
    print(f"  Trades:   {m1['n']:>4d} → {m15['n']:>4d}  ({delta_trades:>+d})")
    print(f"  PF:       {m1['pf']} → {m15['pf']}  ({delta_pf:>+.2f})")
    print(f"  Win Rate: {m1['wr']}% → {m15['wr']}%")
    print(f"  MFE/MAE:  {m1['mfe_mae']} → {m15['mfe_mae']}")
    print(f"  CAGR:     {m1['cagr']}% → {m15['cagr']}%")

    # ── Save ──
    pd.DataFrame([m1, m15, m2]).to_csv(OUT_DIR / "comparison_v1_v15_v2.csv", index=False)
    print(f"\n💾 Saved to {OUT_DIR}/comparison_v1_v15_v2.csv")


if __name__ == "__main__":
    main()
