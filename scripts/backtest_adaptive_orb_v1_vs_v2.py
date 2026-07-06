#!/usr/bin/env python3
"""
adaptive_orb v1 vs v2 Backtest Comparison

Compares:
  - CAGR, PF, Win Rate, Max DD
  - MFE / MAE distribution
  - Early vs Confirmed PnL (v2 only)
  - Trade count and quality

Usage:
    python3 scripts/backtest_adaptive_orb_v1_vs_v2.py
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
from dataclasses import dataclass, field

# ── Imports needed for data preparation ──
from core.data_manager import data_manager

# ── V-Model regime classifier (used by v1 to decide TREND vs WEAK) ──
from core.futures_bar_regime import classify_futures_bar_regime, FuturesBarRegimeConfig


# ====================================================================
# Configuration
# ====================================================================

TICKER = "TXFR1"
INITIAL_CAPITAL = 1_000_000
POINT_VALUE = 200

OUT_DIR = Path("output/backtest/adaptive_orb_v1_vs_v2")
OUT_DIR.mkdir(parents=True, exist_ok=True)

ATR_FLOOR_PCT = 0.0015
EARLY_THRESHOLD = 0.15
CONFIRMED_THRESHOLD = 0.25


# ====================================================================
# Data Loading
# ====================================================================

def load_and_prepare() -> pd.DataFrame:
    """Load TXFR1, compute all indicators from fresh OHLC."""
    df = data_manager.load_historical(TICKER)
    if df.empty:
        print(f"❌ No data for {TICKER}")
        sys.exit(1)

    print(f"✅ Loaded {len(df)} 5m bars ({df.index[0].date()} → {df.index[-1].date()})")

    # Rename lowercase columns to uppercase
    for c_lower, c_upper in [("open", "Open"), ("high", "High"), ("low", "Low"),
                              ("close", "Close"), ("volume", "Volume")]:
        if c_upper not in df.columns and c_lower in df.columns:
            df[c_upper] = df[c_lower]

    d = pd.DataFrame(index=df.index)
    d["Close"] = df["Close"].values
    d["High"] = df["High"].values
    d["Low"] = df["Low"].values
    d["Volume"] = df["Volume"].values

    # ATR (14-period)
    tr = pd.concat([
        d["High"] - d["Low"],
        (d["High"] - d["Close"].shift(1)).abs(),
        (d["Low"] - d["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()

    # Breakout strength (ATR-normalized, V-Model formula)
    high20_s1 = d["High"].rolling(20).max().shift(1)
    atr_floor = d["atr"].clip(lower=d["Close"] * ATR_FLOOR_PCT)
    d["breakout_strength"] = ((d["Close"] - high20_s1) / atr_floor.replace(0, np.nan)).fillna(0).clip(lower=0)

    # Bear breakout
    low20_s1 = d["Low"].rolling(20).min().shift(1)
    d["bear_breakout_strength"] = ((low20_s1 - d["Close"]) / atr_floor.replace(0, np.nan)).fillna(0).clip(lower=0)

    # VWAP
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

    # Bias columns (used by v1 regime classifier)
    d["bull_align"] = False
    d["bear_align"] = False
    d["bullish_align"] = False
    d["bearish_align"] = False
    d["opening_bullish"] = False
    d["opening_bearish"] = False
    d["in_pb_zone"] = False
    d["in_bull_pb_zone"] = False
    d["in_bear_pb_zone"] = False
    d["sqz_on"] = False
    d["volume"] = d["Volume"].values

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


# ====================================================================
# v1 — adaptive_orb (simulated: TREND regime + breakout confirmation)
# ====================================================================

def _classify_regime_v1(row: pd.Series) -> str:
    """Use the actual FuturesBarRegimeConfig to classify for v1 comparison."""
    from core.futures_bar_regime import classify_futures_bar_regime
    cfg = FuturesBarRegimeConfig()
    result = classify_futures_bar_regime(row.to_dict(), config=cfg,
                                          session_regime=row.get("session_regime", "NORMAL"))
    return "TREND" if result.regime in ("TREND", "BEAR") else "WEAK"


# ====================================================================
# v2 — adaptive_orb_v2 (scout/scale, ATR-normalized, regime-aware)
# ====================================================================

@dataclass
class V2State:
    """Running state for v2 simulation."""
    in_position: bool = False
    entry_price: float = 0.0
    entry_idx: int = 0
    atr_at_entry: float = 0.0
    entry_type: str = ""  # EARLY_BREAKOUT or CONFIRMED_BREAKOUT
    size: float = 1.0

    def reset(self):
        self.in_position = False
        self.entry_price = 0.0
        self.entry_type = ""
        self.size = 1.0


# ====================================================================
# Trade data structure
# ====================================================================

@dataclass
class Trade:
    strategy: str
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    pnl_pts: float
    pnl_pct: float
    bars_held: int
    entry_type: str = ""   # v2: EARLY_BREAKOUT / CONFIRMED_BREAKOUT
    mfe: float = 0.0       # Maximum Favorable Excursion (pts)
    mae: float = 0.0       # Maximum Adverse Excursion (pts)

    @property
    def pnl_cash(self) -> float:
        return self.pnl_pts * POINT_VALUE


# ====================================================================
# Backtest: v1
# ====================================================================

def backtest_v1(d: pd.DataFrame) -> list[Trade]:
    """Simulate v1 adaptive_orb: entry on TREND regime, exit -60SL / +200TP."""
    trades: list[Trade] = []
    in_pos = False
    entry_px = 0.0
    entry_idx = 0
    peak = 0.0
    trough = 0.0
    mfe = 0.0
    mae = 0.0

    for i in range(len(d)):
        row = d.iloc[i]
        close = float(row["Close"])

        if in_pos:
            px_change = close - entry_px
            mfe = max(mfe, px_change)
            mae = min(mae, px_change)
            peak = max(peak, close)
            trough = min(trough, close) if trough != 0 else close

            # Exit: -60 SL or +200 TP
            if px_change <= -60 or px_change >= 200:
                trades.append(Trade(
                    strategy="v1_adaptive_orb",
                    entry_idx=entry_idx, exit_idx=i,
                    entry_price=entry_px, exit_price=close,
                    pnl_pts=px_change,
                    pnl_pct=px_change / entry_px * 100 if entry_px else 0,
                    bars_held=i - entry_idx,
                    mfe=mfe, mae=mae,
                ))
                in_pos = False
        else:
            regime = _classify_regime_v1(row)
            if regime == "TREND":
                in_pos = True
                entry_px = close
                entry_idx = i
                peak = close
                trough = close
                mfe = 0.0
                mae = 0.0

    # Close last open position
    if in_pos and len(d) > entry_idx:
        close = float(d.iloc[-1]["Close"])
        px_change = close - entry_px
        trades.append(Trade(
            strategy="v1_adaptive_orb",
            entry_idx=entry_idx, exit_idx=len(d),
            entry_price=entry_px, exit_price=close,
            pnl_pts=px_change,
            pnl_pct=px_change / entry_px * 100 if entry_px else 0,
            bars_held=len(d) - entry_idx,
            mfe=mfe, mae=mae,
        ))

    return trades


# ====================================================================
# Backtest: v2
# ====================================================================

def backtest_v2(d: pd.DataFrame) -> list[Trade]:
    """Simulate v2 adaptive_orb_v2: scout/scale entry with dual thresholds."""
    trades: list[Trade] = []
    state = V2State()

    for i in range(len(d)):
        row = d.iloc[i]
        close = float(row["Close"])

        # ── Exit check ──
        if state.in_position:
            px_change = close - state.entry_price

            # MFE/MAE tracking
            mfe_trade = max(state.atr_at_entry * 3, 0)  # track actual

            # Exit: -1.5*ATR SL or +3.0*ATR TP
            sl = state.entry_price - 1.5 * state.atr_at_entry
            tp = state.entry_price + 3.0 * state.atr_at_entry

            if close <= sl or close >= tp:
                # Compute actual MFE/MAE from bar data during the trade
                entry_close = state.entry_price
                trade_slice = d.iloc[state.entry_idx:i+1]
                mfe_val = (trade_slice["High"].max() - entry_close)
                mae_val = (trade_slice["Low"].min() - entry_close)

                trades.append(Trade(
                    strategy="v2_adaptive_orb_v2",
                    entry_idx=state.entry_idx, exit_idx=i,
                    entry_price=state.entry_price,
                    exit_price=close,
                    pnl_pts=px_change * state.size,
                    pnl_pct=(px_change / state.entry_price * 100) if state.entry_price else 0,
                    bars_held=i - state.entry_idx,
                    entry_type=state.entry_type,
                    mfe=mfe_val, mae=mae_val,
                ))
                state.reset()
                continue  # don't re-enter on same bar

        # ── Entry check (only when flat) ──
        if state.in_position:
            continue

        # Get v2 inputs
        bs = float(row.get("breakout_strength", 0))
        vs = float(row.get("volume_spike", 0))
        vwap = float(row.get("vwap", 0))
        atr_val = float(row.get("atr", 50))

        # Structure: bs > 0
        if bs <= 0:
            continue

        # Behavior: volume spike + VWAP
        if vs < 1.5:
            continue
        if vwap > 0 and close <= vwap:
            continue

        # Entry decision
        if bs >= CONFIRMED_THRESHOLD:
            state.in_position = True
            state.entry_price = close
            state.entry_idx = i
            state.atr_at_entry = atr_val
            state.entry_type = "CONFIRMED_BREAKOUT"
            state.size = 1.0

        elif bs >= EARLY_THRESHOLD:
            # Scout entry (0.3 size) — only in TREND-like regime
            if row.get("adx", 0) >= 20:  # proxy for TREND regime
                state.in_position = True
                state.entry_price = close
                state.entry_idx = i
                state.atr_at_entry = atr_val
                state.entry_type = "EARLY_BREAKOUT"
                state.size = 0.3

    # Close open position
    if state.in_position and len(d) > state.entry_idx:
        close = float(d.iloc[-1]["Close"])
        px_change = close - state.entry_price
        trade_slice = d.iloc[state.entry_idx:]
        mfe_val = (trade_slice["High"].max() - state.entry_price)
        mae_val = (trade_slice["Low"].min() - state.entry_price)
        trades.append(Trade(
            strategy="v2_adaptive_orb_v2",
            entry_idx=state.entry_idx, exit_idx=len(d),
            entry_price=state.entry_price, exit_price=close,
            pnl_pts=px_change * state.size,
            pnl_pct=(px_change / state.entry_price * 100) if state.entry_price else 0,
            bars_held=len(d) - state.entry_idx,
            entry_type=state.entry_type,
            mfe=mfe_val, mae=mae_val,
        ))

    return trades


# ====================================================================
# Metrics
# ====================================================================

def compute_metrics(trades: list[Trade], label: str) -> dict:
    """Compute CAGR, PF, WR, MaxDD, MFE/MAE from trade list."""
    n = len(trades)
    if n == 0:
        return {"strategy": label, "n_trades": 0, "pf": 0, "wr": 0,
                "total_pnl": 0, "cagr": 0, "max_dd": 0,
                "avg_mfe": 0, "avg_mae": 0}

    pnls = [t.pnl_cash for t in trades]
    total_pnl = sum(pnls)

    # Win/loss stats
    wins = [t for t in trades if t.pnl_pts > 0]
    losses = [t for t in trades if t.pnl_pts <= 0]
    wr = len(wins) / n * 100
    gross_w = sum(t.pnl_cash for t in wins)
    gross_l = abs(sum(t.pnl_cash for t in losses))
    pf = gross_w / gross_l if gross_l > 0 else (gross_w if gross_w > 0 else 0)

    # Profit factor with fees (round-trip ~8 pts on TMF)
    fee_per_trade = 8 * POINT_VALUE
    net_pnl = total_pnl - fee_per_trade * n
    net_gross_w = gross_w - fee_per_trade * len(wins)
    net_gross_l = gross_l + fee_per_trade * len(losses)
    net_pf = net_gross_w / net_gross_l if net_gross_l > 0 else 0

    # Max drawdown (equity curve)
    equity = np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = peak - equity
    max_dd = dd.max()

    # CAGR (approx: total return / years)
    years = 3.0  # ~3 years of data
    pnl_ratio = total_pnl / INITIAL_CAPITAL
    cagr = ((1 + pnl_ratio) ** (1 / years) - 1) * 100

    # MFE/MAE
    mfes = [t.mfe for t in trades if t.mfe != 0]
    maes = [t.mae for t in trades if t.mae != 0]
    avg_mfe = sum(mfes) / len(mfes) if mfes else 0
    avg_mae = abs(sum(maes) / len(maes)) if maes else 0

    # Avg hold time
    avg_bars = sum(t.bars_held for t in trades) / n

    return {
        "strategy": label,
        "n_trades": n,
        "pf": round(pf, 2),
        "net_pf": round(net_pf, 2),
        "wr_pct": round(wr, 1),
        "total_pnl_cash": round(total_pnl),
        "cagr_pct": round(cagr, 2),
        "max_dd_cash": round(max_dd),
        "avg_bars": round(avg_bars, 1),
        "avg_mfe": round(avg_mfe, 1),
        "avg_mae": round(avg_mae, 1),
        "mfe_mae_ratio": round(avg_mfe / avg_mae, 2) if avg_mae > 0 else 0,
    }


def pnl_distribution(trades: list[Trade], label: str) -> dict:
    """Compute PnL distribution per entry type (v2 only)."""
    if not trades:
        return {}

    by_type: dict[str, list[float]] = {}
    for t in trades:
        key = t.entry_type or "STANDARD"
        by_type.setdefault(key, []).append(t.pnl_cash)

    result = {}
    for etype, pnls_val in by_type.items():
        result[etype] = {
            "n": len(pnls_val),
            "total_pnl": round(sum(pnls_val)),
            "avg_pnl": round(sum(pnls_val) / len(pnls_val), 1),
            "win_rate": round(sum(1 for p in pnls_val if p > 0) / len(pnls_val) * 100, 1),
        }
    return result


# ====================================================================
# Main
# ====================================================================

def main():
    print("=" * 70)
    print("📊 adaptive_orb v1 vs v2 Backtest Comparison")
    print("=" * 70)

    d = load_and_prepare()
    d = d.iloc[30:].copy()  # drop warmup
    print(f"📉 Using {len(d)} bars after warmup")

    # ── Run v1 ──
    print("\n⚙️  Running v1 (adaptive_orb)...")
    trades_v1 = backtest_v1(d)
    print(f"   → {len(trades_v1)} trades")

    # ── Run v2 ──
    print("\n⚙️  Running v2 (adaptive_orb_v2)...")
    trades_v2 = backtest_v2(d)
    print(f"   → {len(trades_v2)} trades")

    # ── Metrics ──
    m1 = compute_metrics(trades_v1, "v1_adaptive_orb")
    m2 = compute_metrics(trades_v2, "v2_adaptive_orb_v2")

    print("\n" + "=" * 70)
    print("📋 COMPARISON TABLE")
    print("=" * 70)
    headers = ["metric", "v1_adaptive_orb", "v2_adaptive_orb_v2"]
    rows = [
        ["n_trades", str(m1["n_trades"]), str(m2["n_trades"])],
        ["PF", str(m1["pf"]), str(m2["pf"])],
        ["Net PF (incl fees)", str(m1["net_pf"]), str(m2["net_pf"])],
        ["Win Rate %", str(m1["wr_pct"]), str(m2["wr_pct"])],
        ["Total PnL (cash)", f"${m1['total_pnl_cash']:,.0f}", f"${m2['total_pnl_cash']:,.0f}"],
        ["CAGR %", str(m1["cagr_pct"]), str(m2["cagr_pct"])],
        ["Max DD (cash)", f"${m1['max_dd_cash']:,.0f}", f"${m2['max_dd_cash']:,.0f}"],
        ["Avg Bars Held", str(m1["avg_bars"]), str(m2["avg_bars"])],
        ["Avg MFE (pts)", str(m1["avg_mfe"]), str(m2["avg_mfe"])],
        ["Avg MAE (pts)", str(m1["avg_mae"]), str(m2["avg_mae"])],
        ["MFE/MAE Ratio", str(m1["mfe_mae_ratio"]), str(m2["mfe_mae_ratio"])],
    ]
    col_widths = [25, 20, 20]
    print("  " + " | ".join(h.ljust(w) for h, w in zip(headers, col_widths)))
    print("  " + "-" * 67)
    for row in rows:
        print("  " + " | ".join(r.ljust(w) for r, w in zip(row, col_widths)))

    # ── v2 Entry distribution ──
    print("\n" + "=" * 70)
    print("📋 v2 ENTRY TYPE PnL DISTRIBUTION")
    print("=" * 70)
    dist = pnl_distribution(trades_v2, "v2")
    for etype, stats in dist.items():
        print(f"  {etype:20s}: n={stats['n']:4d}  "
              f"total=${stats['total_pnl']:>8,.0f}  "
              f"avg=${stats['avg_pnl']:>8,.1f}  "
              f"WR={stats['win_rate']:.1f}%")

    # ── Save ──
    metrics_df = pd.DataFrame([m1, m2])
    metrics_df.to_csv(OUT_DIR / "comparison_metrics.csv", index=False)

    v2_trades_df = pd.DataFrame([{
        "entry_type": t.entry_type,
        "pnl_pts": t.pnl_pts,
        "pnl_cash": t.pnl_cash,
        "bars_held": t.bars_held,
        "mfe": t.mfe,
        "mae": t.mae,
    } for t in trades_v2])
    v2_trades_df.to_csv(OUT_DIR / "v2_trades.csv", index=False)

    print(f"\n💾 Results saved to {OUT_DIR}/")

    # ── Recommendation ──
    print("\n" + "=" * 70)
    print("📌 RECOMMENDATION")
    print("=" * 70)
    if m2["pf"] > m1["pf"] and m2["cagr_pct"] > m1["cagr_pct"]:
        print("  ✅ v2 outperforms v1 on PF and CAGR — recommend upgrade")
    elif m2["pf"] > m1["pf"]:
        print("  ✅ v2 has higher PF but CAGR similar — recommend upgrade")
    elif m2["cagr_pct"] > m1["cagr_pct"]:
        print("  ⚠️  v2 CAGR better but PF similar — mixed signal")
    else:
        print("  ⚠️  v1 still competitive — review per-entry-type PnL for refinement")


if __name__ == "__main__":
    main()
