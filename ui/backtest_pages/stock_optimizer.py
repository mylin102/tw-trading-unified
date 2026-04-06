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
    st.caption("Vectorbt-style Multi-Asset Portfolio Analysis for Taiwan Stocks.")

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
        
        sl_pct = st.slider("Initial Stop Loss (%)", 1.0, 10.0, 3.0, 0.5) / 100.0
        ts_pct = st.slider("Trailing Stop (%)", 0.5, 5.0, 1.5, 0.1) / 100.0
        tp_pct = st.slider("Take Profit (%)", 1.0, 20.0, 5.0, 0.5) / 100.0
        capital = st.number_input("Capital per trade", 1000, 100000, 10000)

        st.divider()
        st.header(get_text("adv_tools"))
        run_all = st.button(get_text("btn_run_global"), type="primary", use_container_width=True)

    # --- EXECUTION: GLOBAL SCAN ---
    if run_all:
        all_dfs = {}
        with st.spinner(f"Processing {len(available_tickers)} tickers..."):
            progress = st.progress(0)
            for i, t in enumerate(available_tickers):
                try:
                    path = ROOT / "data" / "taifex_raw" / f"STOCK_{t}_5m.csv"
                    if path.exists():
                        tdf = pd.read_csv(path)
                        date_col = "Date" if "Date" in tdf.columns else "timestamp"
                        tdf[date_col] = pd.to_datetime(tdf[date_col])
                        tdf = tdf.set_index(date_col)
                        # 確保欄位大小寫標準化
                        tdf.columns = [c.capitalize() if c.lower() in ["open", "high", "low", "close", "volume"] else c for c in tdf.columns]
                        tdf = tdf.loc[:, ~tdf.columns.duplicated()].copy()
                        
                        if len(tdf) > 30: # 確保有足夠數據計算指標
                            tdf = calculate_futures_squeeze(tdf)
                            if not tdf.empty:
                                all_dfs[t] = tdf
                except Exception as e:
                    st.warning(f"Skipping {t} due to error: {e}")
                progress.progress((i+1)/len(available_tickers))
        
        if not all_dfs:
            st.error("No valid data found for global scan.")
            st.stop()

        with st.spinner("Aggregating portfolio performance..."):
            cfg = {
                "stop_loss_pct": sl_pct,
                "take_profit_pct": tp_pct,
                "trailing_stop_pct": ts_pct,
                "strategy": {"entry_score": 20, strat_name: {"atr_mult": 2.0}}
            }
            summary, ledger = run_multi_asset_backtest(all_dfs, strat_name, cfg, capital_per_trade=capital)
            
            st.header("🌍 Global Portfolio Results")
            if not summary.empty:
                # 1. Metrics
                c1, c2, c3 = st.columns(3)
                c1.metric("Total Portfolio PnL", f"{summary['pnl'].sum():+,.0f} TWD")
                c2.metric("Profitable Assets", f"{len(summary[summary['pnl'] > 0])} / {len(summary)}")
                c3.metric("Total Trades", int(summary["trades"].sum()))
                
                # 2. Reason Analysis
                st.divider()
                st.subheader("🕵️ Exit Reason Distribution")
                exits_only = ledger[ledger["reason"] != "ENTRY"]
                if not exits_only.empty:
                    reason_counts = exits_only["reason"].value_counts().reset_index()
                    reason_counts.columns = ["Reason", "Count"]
                    fig_reasons = px.pie(reason_counts, values="Count", names="Reason", 
                                         color_discrete_sequence=px.colors.qualitative.T10,
                                         hole=0.4)
                    fig_reasons.update_layout(template="plotly_dark", height=350)
                    st.plotly_chart(fig_reasons, use_container_width=True)
                
                # 3. Tables
                st.divider()
                t1, t2 = st.tabs(["By Asset", "Full Trade Ledger"])
                with t1:
                    st.dataframe(summary.sort_values("pnl", ascending=False).style.format({"pnl": "{:+,.0f}", "win_rate": "{:.1f}%"}), use_container_width=True)
                with t2:
                    st.dataframe(ledger.sort_values("time", ascending=False), use_container_width=True)
            else:
                st.info("No trades executed.")
        st.stop()

    # --- EXECUTION: SINGLE TEST ---
    data_path = ROOT / "data" / "taifex_raw" / f"STOCK_{ticker}_5m.csv"
    if not data_path.exists():
        st.error(f"Missing file: {data_path.name}")
        st.stop()

    df_raw = pd.read_csv(data_path)
    date_col = "Date" if "Date" in df_raw.columns else "timestamp"
    df_raw[date_col] = pd.to_datetime(df_raw[date_col])
    df_raw = df_raw.set_index(date_col)

    with st.spinner("Calculating single asset indicators..."):
        df = calculate_futures_squeeze(df_raw)

    if st.button(get_text("btn_run_single"), type="primary", use_container_width=True):
        if df.empty:
            st.error("Data calculation failed. Please check CSV format.")
            st.stop()

        cfg = {
            "strategy": {
                "entry_score": 20, 
                strat_name: {
                    "atr_mult": 2.0, 
                    "stop_loss_pct": sl_pct,
                    "take_profit_pct": tp_pct,
                    "trailing_stop_pct": ts_pct
                }
            }
        }
        
        with st.spinner(get_text("gen_signals")):
            long_signals, short_signals = generate_signals(df, strat_name, cfg)
            
        trading_days = (df.index.year * 10000 + df.index.month * 100 + df.index.day).values
        
        ent, ext, pos, pnl, reasons = simulate_stock_trades(
            df["Close"].values, df["High"].values, df["Low"].values,
            trading_days, long_signals, short_signals,
            100000.0, capital, sl_pct, tp_pct, ts_pct
        )
        res = calculate_stock_metrics(pnl, 100000.0)
        
        st.header(f"Performance: {ticker}")
        c1, c2, c3 = st.columns(3)
        c1.metric(get_text("profit"), f"{res['total_pnl']:+,.0f} TWD")
        c2.metric(get_text("win_rate"), f"{res['win_rate']:.1f}%")
        c3.metric(get_text("trades"), int(res['total_trades']))
        
        equity = 100000.0 + np.cumsum(pnl)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df.index, y=equity, name="Equity", line=dict(color="#10B981", width=2)))
        fig.update_layout(title=get_text("equity_curve"), template="plotly_dark", height=400)
        st.plotly_chart(fig, use_container_width=True)

        # 原因分析
        st.subheader("📋 Trade Ledger")
        trade_idx = np.where(pnl != 0)[0]
        if len(trade_idx) > 0:
            REASON_MAP = {1: "ENTRY", 2: "SCALE", 3: "STOP", 4: "TP", 5: "TRAILING", 6: "SIGNAL", 7: "FINAL"}
            log_df = pd.DataFrame({
                "Time": df.index[trade_idx],
                "PnL": pnl[trade_idx],
                "Reason": [REASON_MAP.get(r, "UNKNOWN") for r in reasons[trade_idx]]
            })
            st.dataframe(log_df, use_container_width=True)

if __name__ == "__main__":
    main()
