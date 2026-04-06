import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from pathlib import Path
import sys

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.stock_engine import simulate_stock_trades, calculate_stock_metrics # noqa: E402
from backtest.sweep_engine import run_multi_asset_backtest # noqa: E402
from backtest.signal_generator import generate_signals # noqa: E402
from strategies.stocks.entry_strategies import STOCK_STRATEGIES # noqa: E402
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze # noqa: E402
from scripts.add_watchlist import add_to_watchlist # noqa: E402
from core.i18n import get_text # noqa: E402

def get_available_tickers():
    DATA_DIR = ROOT / "data" / "taifex_raw"
    return sorted([f.stem.split("_")[1] for f in DATA_DIR.glob("STOCK_*_5m.csv")])

def main():
    st.title(f"🍎 {get_text('nav_stock')}")
    st.caption("True Vectorbt-style Portfolio Parameter Optimization.")

    available_tickers = get_available_tickers()

    # 1. Sidebar Controls
    with st.sidebar:
        st.header(get_text("ticker_select"))
        ticker = st.selectbox(get_text("ticker_select"), available_tickers, index=available_tickers.index("2330") if "2330" in available_tickers else 0)
        
        with st.expander(f"➕ {get_text('add_ticker')}"):
            new_t = st.text_input("Ticker Code", key="new_ticker_input")
            if st.button("Download"):
                if new_t:
                    with st.spinner(f"Fetching {new_t}..."):
                        success, msg = add_to_watchlist(new_t)
                        if success: st.rerun()
                        else: st.error(msg)

        st.divider()
        st.header(get_text("strategy_settings"))
        strat_name = st.selectbox("Select strategy", list(STOCK_STRATEGIES.keys()))
        st.info(STOCK_STRATEGIES[strat_name]["desc"])
        
        st.divider()
        st.header(get_text("params"))
        sl_pct = st.slider("Initial Stop Loss (%)", 1.0, 10.0, 3.0, 0.5) / 100.0
        ts_pct_current = st.slider("Trailing Stop (%)", 0.5, 5.0, 1.5, 0.1) / 100.0
        tp_pct = st.slider("Take Profit (%)", 1.0, 20.0, 5.0, 0.5) / 100.0
        capital = st.number_input(get_text("initial_bal"), 1000, 100000, 10000)

        st.divider()
        st.header("⚡️ Portfolio Optimization")
        ts_range = st.multiselect("Scan Trailing Stop Range (%)", [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0], default=[1.0, 1.5, 2.0])
        run_opt = st.button("🚀 Run Global Optimization (Matrix)", type="primary", use_container_width=True)

    # --- EXECUTION: PORTFOLIO OPTIMIZATION ---
    if run_opt:
        all_dfs = {}
        with st.spinner(f"Preparing {len(available_tickers)} assets..."):
            progress = st.progress(0)
            for i, t in enumerate(available_tickers):
                try:
                    path = ROOT / "data" / "taifex_raw" / f"STOCK_{t}_5m.csv"
                    tdf = pd.read_csv(path)
                    date_col = "Date" if "Date" in tdf.columns else "timestamp"
                    tdf[date_col] = pd.to_datetime(tdf[date_col])
                    tdf = tdf.set_index(date_col)
                    tdf.columns = [c.capitalize() if c.lower() in ["open", "high", "low", "close", "volume"] else c for c in tdf.columns]
                    if len(tdf) > 30:
                        all_dfs[t] = calculate_futures_squeeze(tdf)
                except Exception: continue
                progress.progress((i+1)/len(available_tickers))

        opt_results = []
        with st.spinner(f"Optimizing across {len(ts_range)} parameter sets..."):
            for val in ts_range:
                cfg = {
                    "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct, "trailing_stop_pct": val/100.0,
                    "strategy": {"entry_score": 20, strat_name: {"atr_mult": 2.0}}
                }
                summary, _ = run_multi_asset_backtest(all_dfs, strat_name, cfg, capital_per_trade=capital)
                if not summary.empty:
                    for _, row in summary.iterrows():
                        opt_results.append({
                            "Ticker": row["ticker"],
                            "TS%": val,
                            "PnL": row["pnl"]
                        })
        
        if opt_results:
            opt_df = pd.DataFrame(opt_results)
            st.header("📊 Portfolio Optimization Matrix")
            # Pivot for Heatmap
            matrix = opt_df.pivot(index="Ticker", columns="TS%", values="PnL")
            fig = px.imshow(matrix, labels=dict(x="Trailing Stop %", y="Ticker", color="PnL"),
                            color_continuous_scale="RdYlGn", aspect="auto", height=800)
            fig.update_yaxes(type='category') # Force Tickers to be categorical, not numeric
            st.plotly_chart(fig, use_container_width=True)
            
            # Recommendation
            best_ts = opt_df.groupby("TS%")["PnL"].sum().idxmax()
            st.success(f"💡 **Recommendation:** For this portfolio, a **{best_ts}% Trailing Stop** yields the highest total profit.")
        else:
            st.error("No trades triggered.")
        st.stop()

    # --- EXECUTION: SINGLE TEST ---
    data_path = ROOT / "data" / "taifex_raw" / f"STOCK_{ticker}_5m.csv"
    df_raw = pd.read_csv(data_path)
    date_col = "Date" if "Date" in df_raw.columns else "timestamp"
    df_raw[date_col] = pd.to_datetime(df_raw[date_col])
    df_raw = df_raw.set_index(date_col)
    df = calculate_futures_squeeze(df_raw)

    if st.button(get_text("btn_run_single"), type="primary", use_container_width=True):
        cfg = {"strategy": {"entry_score": 20, strat_name: {"atr_mult": 2.0, "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct, "trailing_stop_pct": ts_pct_current}}}
        longs, shorts = generate_signals(df, strat_name, cfg)
        trading_days = (df.index.year * 10000 + df.index.month * 100 + df.index.day).values
        ent, ext, pos, pnl, reasons = simulate_stock_trades(df["Close"].values, df["High"].values, df["Low"].values, trading_days, longs, shorts, 100000.0, capital, sl_pct, tp_pct, ts_pct_current)
        res = calculate_stock_metrics(pnl, 100000.0)
        
        st.header(f"📊 {get_text('results')}: {ticker}")
        c1, c2, c3 = st.columns(3)
        c1.metric(get_text("profit"), f"{res['total_pnl']:+,.0f} TWD")
        c2.metric(get_text("win_rate"), f"{res['win_rate']:.1f}%")
        c3.metric(get_text("trades"), int(res['total_trades']))
        
        equity = 100000.0 + np.cumsum(pnl)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df.index, y=equity, name="Equity", line=dict(color="#10B981", width=2)))
        fig.update_layout(title=get_text("equity_curve"), height=400)
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("📋 Trade Ledger")
        trade_idx = np.where(pnl != 0)[0]
        if len(trade_idx) > 0:
            REASON_MAP = {1: "偵察兵進場", 2: "主軍加碼", 3: "硬性止損", 4: "目標止盈", 5: "移動停損", 6: "訊號出場", 7: "收盤平倉"}
            log_df = pd.DataFrame({"Time": df.index[trade_idx], "PnL": pnl[trade_idx], "Reason": [REASON_MAP.get(r, "未知") for r in reasons[trade_idx]]})
            st.dataframe(log_df, use_container_width=True)

if __name__ == "__main__":
    main()
