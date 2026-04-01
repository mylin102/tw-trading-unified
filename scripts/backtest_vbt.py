#!/usr/bin/env python3
"""
Optimized backtest with vectorbt analytics.
Strategy logic stays as custom loop (squeeze-specific), 
but uses vbt for performance metrics, plotting, and parameter heatmap.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies", "futures"))

import numpy as np
import pandas as pd
import vectorbt as vbt
from itertools import product
from squeeze_futures.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment

EXPORTS = os.path.join(os.path.dirname(__file__), "..", "exports")


def load_and_calc(path):
    df_raw = pd.read_csv(path, index_col=0, parse_dates=True)
    df_raw = df_raw[df_raw["Close"] > 20000]
    df = calculate_futures_squeeze(df_raw, bb_length=20, ema_fast=20, ema_slow=60, lookback=60, pb_buffer=1.002)
    df_15m = calculate_futures_squeeze(
        df_raw.resample("15min").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna(),
        bb_length=20, ema_fast=20, ema_slow=60, lookback=60, pb_buffer=1.002)

    scores = []
    for ts in df.index:
        p = {"5m": df.loc[:ts]}
        m15 = df_15m.loc[:ts]
        if len(m15) > 0: p["15m"] = m15
        scores.append(calculate_mtf_alignment(p, weights={"5m": 0.5, "15m": 0.5})["score"])
    df["score"] = scores
    print(f"Data: {len(df)} bars, {df.index[0]} → {df.index[-1]}")
    return df


def backtest_futures(df, entry_score, sl_pts, tp_pts, pv=10):
    """Returns equity curve Series for vbt analysis."""
    equity = [100000.0]
    pos, ep = 0, 0
    for i in range(1, len(df)):
        r = df.iloc[i]
        price, score, sqz, mom = r["Close"], r["score"], r["sqz_on"], r["mom_state"]
        if pos != 0:
            pnl_pts = (price - ep) * pos
            if pnl_pts <= -sl_pts:
                equity.append(equity[-1] - sl_pts * pv); pos = 0
            elif pnl_pts >= tp_pts:
                equity.append(equity[-1] + tp_pts * pv); pos = 0
            else:
                equity.append(equity[-1] + (pnl_pts - (df.iloc[i-1]["Close"] - ep) * pos) * pv)
                continue
        else:
            if not sqz and score >= entry_score and mom >= 2:
                pos, ep = 1, price
            elif not sqz and score <= -entry_score and mom <= 1:
                pos, ep = -1, price
            equity.append(equity[-1])
    return pd.Series(equity, index=df.index[:len(equity)])


def backtest_options(df, entry_score, sl_pct, tp_pct, premium=100, pv=50):
    equity = [50000.0]
    pos, side, ep, emtx, ebar = 0, None, 0, 0, 0
    theta = 0.02 / 54
    for i in range(1, len(df)):
        r = df.iloc[i]
        price, score, sqz = r["Close"], r["score"], r["sqz_on"]
        if pos == 1:
            pts = (price - emtx) * (1 if side == "C" else -1)
            cur = ep * (1 - theta * (i - ebar)) + pts * 0.5
            if cur <= ep * (1 - sl_pct):
                equity.append(equity[-1] - sl_pct * ep * pv); pos = 0
            elif cur >= ep * (1 + tp_pct):
                equity.append(equity[-1] + tp_pct * ep * pv); pos = 0
            elif abs(score) < 20:
                equity.append(equity[-1] + (cur - ep) * pv); pos = 0
            else:
                equity.append(equity[-1])
                continue
        else:
            if not sqz and score >= entry_score:
                pos, side, ep, emtx, ebar = 1, "C", premium, price, i
            elif not sqz and score <= -entry_score:
                pos, side, ep, emtx, ebar = 1, "P", premium, price, i
            equity.append(equity[-1])
    return pd.Series(equity, index=df.index[:len(equity)])


def main():
    q1 = os.path.expanduser("~/Documents/mylin102/tw-option-squeeze-trading/exports/tmf_replay_5min_q1_2026.csv")
    today = os.path.join(EXPORTS, "tmf_5m_full_20260401.csv")
    df = load_and_calc(q1 if os.path.exists(q1) else today)

    # === Futures sweep ===
    print("\n🔵 FUTURES TMF — Parameter Sweep")
    f_params = list(product([20, 40, 60, 80], [30, 40, 50, 60], [40, 60, 80, 100]))
    f_results = []
    for es, sl, tp in f_params:
        eq = backtest_futures(df, es, sl, tp)
        ret = eq.pct_change().dropna()
        pnl = eq.iloc[-1] - eq.iloc[0]
        sharpe = ret.mean() / ret.std() * np.sqrt(252 * 54) if ret.std() > 0 else 0
        dd = (eq / eq.cummax() - 1).min()
        f_results.append({"entry": es, "sl": sl, "tp": tp, "pnl": round(pnl), "sharpe": round(sharpe, 2), "max_dd%": round(dd * 100, 1)})

    fdf = pd.DataFrame(f_results).sort_values("pnl", ascending=False)
    print(fdf.head(10).to_string(index=False))
    fdf.to_csv(os.path.join(EXPORTS, "vbt_futures_sweep.csv"), index=False)

    # Best futures equity curve
    best_f = fdf.iloc[0]
    best_eq = backtest_futures(df, int(best_f["entry"]), int(best_f["sl"]), int(best_f["tp"]))
    print(f"\nBest: entry={best_f['entry']} sl={best_f['sl']} tp={best_f['tp']} → PnL={best_f['pnl']:+,.0f} Sharpe={best_f['sharpe']}")

    # === Options sweep ===
    print("\n🟠 OPTIONS TXO — Parameter Sweep")
    o_params = list(product([60, 70, 80, 90], [0.10, 0.15, 0.20], [0.5, 0.8, 1.0, 1.2]))
    o_results = []
    for es, sl, tp in o_params:
        eq = backtest_options(df, es, sl, tp)
        ret = eq.pct_change().dropna()
        pnl = eq.iloc[-1] - eq.iloc[0]
        sharpe = ret.mean() / ret.std() * np.sqrt(252 * 54) if ret.std() > 0 else 0
        dd = (eq / eq.cummax() - 1).min()
        o_results.append({"entry": es, "sl%": sl, "tp%": tp, "pnl": round(pnl), "sharpe": round(sharpe, 2), "max_dd%": round(dd * 100, 1)})

    odf = pd.DataFrame(o_results).sort_values("pnl", ascending=False)
    print(odf.head(10).to_string(index=False))
    odf.to_csv(os.path.join(EXPORTS, "vbt_options_sweep.csv"), index=False)

    # Heatmap: entry_score vs tp for best sl
    print("\n📊 Generating heatmaps...")
    try:
        import plotly.express as px
        # Futures heatmap
        pivot = fdf.pivot_table(values="pnl", index="sl", columns="tp", aggfunc="max")
        fig = px.imshow(pivot, text_auto=True, title="Futures PnL: SL vs TP (best entry_score)", color_continuous_scale="RdYlGn")
        fig.write_html(os.path.join(EXPORTS, "vbt_futures_heatmap.html"))
        # Options heatmap
        pivot_o = odf.pivot_table(values="pnl", index="sl%", columns="tp%", aggfunc="max")
        fig_o = px.imshow(pivot_o, text_auto=True, title="Options PnL: SL% vs TP%", color_continuous_scale="RdYlGn")
        fig_o.write_html(os.path.join(EXPORTS, "vbt_options_heatmap.html"))
        print(f"Heatmaps saved to {EXPORTS}/vbt_*_heatmap.html")
    except Exception as e:
        print(f"Heatmap error: {e}")

    print(f"\nAll results saved to {EXPORTS}/vbt_*.csv")


if __name__ == "__main__":
    main()
