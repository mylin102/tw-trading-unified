#!/usr/bin/env python3
"""
Vectorbt-style Portfolio Grid Sweep for Taiwan Stocks.
Scans stop_loss × take_profit × trailing_stop across ALL 46 assets simultaneously.
Outputs: heatmap HTML + best parameter recommendation.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import time
import numpy as np
import pandas as pd
from datetime import datetime
from backtest.sweep_engine import run_portfolio_grid_sweep
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze

DATA_DIR = ROOT / "data" / "taifex_raw"
REPORT_DIR = ROOT / "exports" / "backtests"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# ── 0. 可用策略 ──
STRATEGY = "momentum_breakout"  # 回測最佳: PnL=+37,512, 100% 參數組合獲利

# ── 1. 載入全部標的 ──
def load_all_stocks():
    all_dfs = {}
    for f in sorted(DATA_DIR.glob("STOCK_*_5m.csv")):
        ticker = f.stem.split("_")[1]
        try:
            df = pd.read_csv(f)
            date_col = "Date" if "Date" in df.columns else "timestamp"
            df[date_col] = pd.to_datetime(df[date_col])
            df = df.set_index(date_col)
            df.columns = [c.capitalize() if c.lower() in ["open", "high", "low", "close", "volume"] else c for c in df.columns]
            if len(df) > 60:
                all_dfs[ticker] = calculate_futures_squeeze(df)
        except Exception as e:
            print(f"  ⚠️ Skip {ticker}: {e}")
    return all_dfs

# ── 2. 掃描參數定義 ──
SWEEP_PARAMS = {
    "stop_loss_pct":    [0.02, 0.03, 0.04, 0.05],
    "take_profit_pct":  [0.03, 0.05, 0.08, 0.10, 0.15],
    "trailing_stop_pct": [0.01, 0.015, 0.02, 0.03],
}

BASE_CFG = {
    "strategy": {
        "entry_score": 20,
    }
}

# ── 3. 執行 ──
def main():
    print("=" * 60)
    print("🍎 Vectorbt Portfolio Grid Sweep — Taiwan Stocks")
    print("=" * 60)

    print("\n📂 Loading data...")
    t0 = time.time()
    all_dfs = load_all_stocks()
    print(f"   ✅ {len(all_dfs)} tickers loaded ({time.time()-t0:.1f}s)")

    combos = 1
    for v in SWEEP_PARAMS.values():
        combos *= len(v)
    print(f"\n🔬 Sweep: {combos} combos × {len(all_dfs)} assets = {combos * len(all_dfs)} backtests (strategy: {STRATEGY})")

    t1 = time.time()
    results = run_portfolio_grid_sweep(
        all_dfs, STRATEGY, SWEEP_PARAMS, BASE_CFG, capital_per_trade=20000.0
    )
    elapsed = time.time() - t1
    print(f"   ✅ Done in {elapsed:.1f}s ({combos * len(all_dfs) / elapsed:.0f} backtests/sec)")

    # ── 4. 分析結果 ──
    results = results.sort_values("Total_PnL", ascending=False)
    best = results.iloc[0]
    worst = results.iloc[-1]

    print(f"\n{'=' * 60}")
    print("🏆 TOP 5 Parameter Combinations")
    print("=" * 60)
    for i, (_, r) in enumerate(results.head(5).iterrows()):
        print(f"  #{i+1}  SL={r['stop_loss_pct']:.0%} TP={r['take_profit_pct']:.0%} TS={r['trailing_stop_pct']:.1%}"
              f"  │ PnL={r['Total_PnL']:+,.0f}  Trades={int(r['Total_Trades'])}  Win={int(r['Winning_Assets'])}/{len(all_dfs)} ({r['Profitable_Ratio']:.0f}%)")

    print(f"\n{'=' * 60}")
    print("💀 WORST 3")
    print("=" * 60)
    for _, r in results.tail(3).iterrows():
        print(f"  SL={r['stop_loss_pct']:.0%} TP={r['take_profit_pct']:.0%} TS={r['trailing_stop_pct']:.1%}"
              f"  │ PnL={r['Total_PnL']:+,.0f}  Trades={int(r['Total_Trades'])}")

    # ── 5. 穩健性分析：找高原區 ──
    print(f"\n{'=' * 60}")
    print("🛡️ Robustness: Plateau Detection")
    print("=" * 60)
    profitable = results[results["Total_PnL"] > 0]
    print(f"  獲利組合: {len(profitable)}/{len(results)} ({len(profitable)/len(results)*100:.0f}%)")
    if len(profitable) > 0:
        # 找最穩定的 trailing_stop 值
        ts_agg = results.groupby("trailing_stop_pct")["Total_PnL"].agg(["mean", "std", "min"])
        ts_agg["sharpe"] = ts_agg["mean"] / ts_agg["std"].replace(0, np.nan)
        best_ts = ts_agg["sharpe"].idxmax()
        print(f"  最穩健 Trailing Stop: {best_ts:.1%} (Sharpe={ts_agg.loc[best_ts, 'sharpe']:.2f})")

        sl_agg = results.groupby("stop_loss_pct")["Total_PnL"].agg(["mean", "std"])
        sl_agg["sharpe"] = sl_agg["mean"] / sl_agg["std"].replace(0, np.nan)
        best_sl = sl_agg["sharpe"].idxmax()
        print(f"  最穩健 Stop Loss:    {best_sl:.0%} (Sharpe={sl_agg.loc[best_sl, 'sharpe']:.2f})")

    # ── 6. 輸出 HTML 報告 ──
    report_path = generate_report(results, all_dfs, elapsed)
    print(f"\n📊 Report: {report_path}")

    # ── 7. 輸出 CSV ──
    csv_path = REPORT_DIR / f"sweep_results_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    results.to_csv(csv_path, index=False)
    print(f"📋 CSV:    {csv_path}")


def generate_report(results, all_dfs, elapsed):
    """產出 Heatmap HTML 報告"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    best = results.iloc[0]

    # 建立多個 heatmap (每個 TP 值一張)
    heatmaps_html = ""
    for tp in sorted(results["take_profit_pct"].unique()):
        subset = results[results["take_profit_pct"] == tp]
        pivot = subset.pivot_table(index="stop_loss_pct", columns="trailing_stop_pct", values="Total_PnL")
        
        # 轉成 HTML table with color
        rows_html = ""
        vmin, vmax = results["Total_PnL"].min(), results["Total_PnL"].max()
        for sl in pivot.index:
            cells = ""
            for ts_val in pivot.columns:
                val = pivot.loc[sl, ts_val]
                if pd.isna(val):
                    cells += "<td>-</td>"
                else:
                    ratio = (val - vmin) / (vmax - vmin) if vmax != vmin else 0.5
                    r = int(239 * (1 - ratio) + 16 * ratio)
                    g = int(68 * (1 - ratio) + 185 * ratio)
                    b = int(68 * (1 - ratio) + 129 * ratio)
                    cells += f'<td style="background:rgb({r},{g},{b});color:white;font-weight:bold;text-align:center">{val:+,.0f}</td>'
            rows_html += f"<tr><td style='font-weight:bold'>{sl:.0%}</td>{cells}</tr>"
        
        header = "".join(f"<th>{c:.1%}</th>" for c in pivot.columns)
        heatmaps_html += f"""
        <div class="card p-3 mb-4">
            <h4>Take Profit = {tp:.0%}</h4>
            <table class="table table-sm table-bordered text-center">
                <thead><tr><th>SL \\ TS</th>{header}</tr></thead>
                <tbody>{rows_html}</tbody>
            </table>
        </div>"""

    # 組合排行
    top10 = results.head(10)
    ranking_html = top10.to_html(classes="table table-striped table-sm", index=False, float_format=lambda x: f"{x:,.2f}")

    html = f"""<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<title>Portfolio Grid Sweep Report</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
<style>body{{background:#f8f9fa;padding:30px;font-family:'Inter','PingFang TC',sans-serif}}
.card{{border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.08);border:none}}
.metric{{font-size:28px;font-weight:800}} .pos{{color:#10b981}} .neg{{color:#ef4444}}</style>
</head><body><div class="container">
<h1>🍎 Portfolio Grid Sweep Report</h1>
<p class="text-muted">{ts} | {len(all_dfs)} assets | {len(results)} combos | {elapsed:.1f}s</p>

<div class="row mb-4">
  <div class="col-md-3"><div class="card p-3"><div class="text-muted">Best PnL</div>
    <div class="metric {'pos' if best['Total_PnL']>0 else 'neg'}">{best['Total_PnL']:+,.0f}</div></div></div>
  <div class="col-md-3"><div class="card p-3"><div class="text-muted">Best Params</div>
    <div class="metric" style="font-size:16px">SL={best['stop_loss_pct']:.0%} TP={best['take_profit_pct']:.0%} TS={best['trailing_stop_pct']:.1%}</div></div></div>
  <div class="col-md-3"><div class="card p-3"><div class="text-muted">Winning Assets</div>
    <div class="metric">{int(best['Winning_Assets'])}/{len(all_dfs)}</div></div></div>
  <div class="col-md-3"><div class="card p-3"><div class="text-muted">Total Trades</div>
    <div class="metric">{int(best['Total_Trades'])}</div></div></div>
</div>

<h2>🔥 Heatmaps (SL × TS, grouped by TP)</h2>
{heatmaps_html}

<h2>🏆 Top 10 Combinations</h2>
<div class="card p-3">{ranking_html}</div>

<div class="mt-4 text-center text-muted">tw-trading-unified v4.1 | Vectorbt Portfolio Sweep</div>
</div></body></html>"""

    path = REPORT_DIR / f"portfolio_sweep_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


if __name__ == "__main__":
    main()
