"""台股策略排行榜 — 跨策略 × 跨標的比較"""
import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.stocks.entry_strategies import STOCK_STRATEGIES  # noqa: E402
from backtest.signal_generator import generate_signals  # noqa: E402
from backtest.stock_engine import simulate_stock_trades, calculate_stock_metrics  # noqa: E402
from strategies.options.options_engine.engine.indicators import calculate_stock_squeeze  # noqa: E402

DATA_DIR = ROOT / "data" / "taifex_raw"


def load_stock_csv(ticker):
    f = DATA_DIR / f"STOCK_{ticker}_5m.csv"
    if not f.exists():
        return None
    df = pd.read_csv(f)
    date_col = "Date" if "Date" in df.columns else "timestamp"
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.set_index(date_col)
    if len(df) < 60:
        return None
    return calculate_stock_squeeze(df)


def main():
    st.title("🍎 台股策略排行榜")
    st.caption("跨策略 × 跨標的回測比較 — 找出最適合的策略與標的組合")

    # Sidebar
    with st.sidebar:
        st.header("參數設定")
        available = sorted([f.stem.replace("STOCK_", "").replace("_5m", "") for f in DATA_DIR.glob("STOCK_*_5m.csv")])
        tickers = st.multiselect("標的", available, default=available[:10])
        strat_names = st.multiselect("策略", list(STOCK_STRATEGIES.keys()), default=list(STOCK_STRATEGIES.keys()))
        capital = st.number_input("單筆資金 (TWD)", value=20000, step=5000)
        sl_pct = st.slider("停損 %", 1.0, 10.0, 3.0, 0.5) / 100
        tp_pct = st.slider("停利 %", 3.0, 30.0, 15.0, 1.0) / 100
        ts_pct = st.slider("移動停利 %", 1.0, 10.0, 3.0, 0.5) / 100

    if not tickers or not strat_names:
        st.info("請在左側選擇標的和策略")
        return

    if st.button("🚀 執行排行榜回測", type="primary", use_container_width=True):
        results = []
        total = len(tickers) * len(strat_names)
        progress = st.progress(0)
        i = 0

        for ticker in tickers:
            df = load_stock_csv(ticker)
            if df is None:
                i += len(strat_names)
                continue
            trading_days = (df.index.year * 10000 + df.index.month * 100 + df.index.day).values

            for sname in strat_names:
                i += 1
                progress.progress(i / total, text=f"{ticker} × {sname}")
                try:
                    cfg = {"strategy": {"entry_score": 20, sname: {"stop_loss_pct": sl_pct, "take_profit_pct": tp_pct}}}
                    longs, shorts = generate_signals(df, sname, cfg)
                    if longs.sum() == 0 and shorts.sum() == 0:
                        continue
                    _, _, _, pnl, reasons, _ = simulate_stock_trades(
                        df["Close"].values, df["High"].values, df["Low"].values,
                        trading_days, longs, shorts,
                        100000.0, capital, sl_pct, tp_pct, ts_pct,
                    )
                    metrics = calculate_stock_metrics(pnl, 100000.0)
                    if metrics["total_trades"] > 0:
                        results.append({
                            "標的": ticker,
                            "策略": sname,
                            "交易數": int(metrics["total_trades"]),
                            "勝率%": round(metrics["win_rate"], 1),
                            "總損益": round(metrics["total_pnl"]),
                            "最大回撤": round(metrics.get("max_drawdown", 0)),
                            "Profit Factor": round(metrics.get("profit_factor", 0), 2),
                        })
                except Exception as e:
                    st.warning(f"{ticker} × {sname}: {e}")

        progress.empty()

        if not results:
            st.warning("沒有產生任何交易")
            return

        rdf = pd.DataFrame(results)

        # === 策略排行 ===
        st.header("🏆 策略排行（跨標的加總）")
        strat_agg = rdf.groupby("策略").agg(
            總損益=("總損益", "sum"), 交易數=("交易數", "sum"),
            平均勝率=("勝率%", "mean"), 標的數=("標的", "nunique"),
        ).sort_values("總損益", ascending=False).reset_index()
        st.dataframe(strat_agg, use_container_width=True, hide_index=True)

        # === 熱力圖 ===
        st.header("🗺️ 策略 × 標的 損益熱力圖")
        pivot = rdf.pivot_table(index="標的", columns="策略", values="總損益", aggfunc="sum").fillna(0)
        fig = px.imshow(pivot, color_continuous_scale="RdYlGn", aspect="auto",
                        labels=dict(x="策略", y="標的", color="PnL (TWD)"), height=max(400, len(tickers) * 30))
        fig.update_yaxes(type="category")
        st.plotly_chart(fig, use_container_width=True)

        # === 明細 ===
        st.header("📋 完整明細")
        st.dataframe(rdf.sort_values("總損益", ascending=False), use_container_width=True, hide_index=True)


main()
