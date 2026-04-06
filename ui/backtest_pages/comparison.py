import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import sys
from pathlib import Path

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.signal_generator import generate_signals # noqa: E402
from strategies.futures.squeeze_futures.engine.vectorized import simulate_trades_vectorized, calculate_metrics # noqa: E402
from ui.backtest_pages.single_test import load_backtest_data # noqa: E402
from strategies.futures.entry_strategies import STRATEGIES # noqa: E402
from core.i18n import get_text # noqa: E402

def main():
    st.title(f"🏆 {get_text('leaderboard_title')}")
    st.caption(get_text("leaderboard_cap"))

    # 1. Sidebar Controls
    with st.sidebar:
        st.header(get_text("data_source"))
        src_opts = [get_text("today_ind"), get_text("specific_date"), get_text("q1_data")]
        # Default to Q1 data (always available)
        src = st.radio(get_text("select_source"), src_opts, key="comp_src", index=2)
        date_val = None
        if src == get_text("specific_date"):
            date_val = st.text_input(get_text("enter_date"), key="comp_date")

        source_map = {get_text("today_ind"): "today", get_text("specific_date"): "specific", get_text("q1_data"): "q1"}
        df = load_backtest_data(source_map[src], date_val)

        if df is None:
            st.error("No data available. Try 'Q1 Historical Data'.")
            st.stop()

        st.success(get_text("loaded_bars", len(df)))

        st.divider()
        st.header(get_text("params"))
        atr_mult = st.slider(get_text("atr_mult"), 1.0, 4.0, 2.0, 0.5)
        initial_bal = 100000.0
        intraday_mode = st.checkbox("🌙 日內模式", value=True)

    # 2. Execution
    if st.button(get_text("btn_run_comp"), type="primary", use_container_width=True):
        results = []

        # Debug info
        st.info(f"日內模式: {'✅ 開啟 (強制日終平倉)' if intraday_mode else '❌ 關閉 (允許持倉過夜)'}")

        # NumPy extraction
        open_arr = df["Open"].values
        close_arr = df["Close"].values
        high_arr = df["High"].values
        low_arr = df["Low"].values
        vwap_arr = df["vwap"].values if "vwap" in df.columns else np.zeros(len(df))
        atr_arr = df["atr"].values if "atr" in df.columns else np.full(len(df), 30.0)

        progress_bar = st.progress(0)
        strats = list(STRATEGIES.keys())
        
        for i, name in enumerate(strats):
            # Update progress
            progress_bar.progress((i + 1) / len(strats), text=f"Testing {name}...")
            
            # 1. Generate Signals
            # Use base config
            cfg = {"strategy": {"entry_score": 20, "regime_filter": "mid", name: {"atr_mult": atr_mult}}}
            longs, shorts = generate_signals(df, name, cfg)

            # Build end-of-day bars mask
            eod_bars = np.zeros(len(df), dtype=np.bool_)
            if intraday_mode and "trading_day" in df.columns:
                for i in range(1, len(df)):
                    if df["trading_day"].values[i] != df["trading_day"].values[i - 1]:
                        eod_bars[i - 1] = True
                eod_bars[-1] = True
                st.info(f"📅 已標記 {eod_bars.sum()} 個日終平倉點 / {len(df)} 根 K 線")

            # 2. Simulate with FULL 16 arguments + intraday params
            entries, exits, positions, pnl, reasons = simulate_trades_vectorized(
                open_arr, close_arr, high_arr, low_arr, vwap_arr, atr_arr,
                longs, shorts,
                initial_balance=initial_bal,
                point_value=10.0,
                fee_per_side=10.0,
                exchange_fee=2.0,
                tax_rate=0.00002,
                max_positions=1,
                lots_per_trade=1,
                slippage=1.0,
                stop_loss_pts=30,
                atr_mult=atr_mult,
                tp1_pts=30,
                tp1_lots=1,
                exit_on_vwap=True,
                intraday_only=intraday_mode,
                eod_bars=eod_bars,
            )
            
            # 3. Metrics
            metrics_raw = calculate_metrics(pnl, entries, exits, positions, initial_bal)
            metrics = dict(metrics_raw) # Convert Numba typed dict to regular python dict
            metrics["Strategy"] = name

            # Add exit reason counts for debugging
            if intraday_mode:
                eod_exits = int((reasons == 4).sum())  # Reason 4 = EOD exit
                metrics["eod_exits"] = eod_exits

            results.append(metrics)
            
        progress_bar.empty()
        
        # 3. Leaderboard Table
        res_df = pd.DataFrame(results)

        # Engine returns PascalCase: Total_PnL, Total_Trades, etc.
        pnl_col = "Total_PnL" if "Total_PnL" in res_df.columns else "total_pnl"
        wr_col = "Win_Rate" if "Win_Rate" in res_df.columns else "win_rate"
        mdd_col = "Max_Drawdown" if "Max_Drawdown" in res_df.columns else "max_drawdown"
        pf_col = "Profit_Factor" if "Profit_Factor" in res_df.columns else "profit_factor"
        trades_col = "Total_Trades" if "Total_Trades" in res_df.columns else "total_trades"

        cols = ["Strategy", pnl_col, wr_col, mdd_col, pf_col, trades_col]
        if intraday_mode and "eod_exits" in res_df.columns:
            cols.append("eod_exits")

        res_df = res_df[cols].sort_values(pnl_col, ascending=False)

        st.header(get_text("ranking"))

        # Styled DataFrame
        def color_pnl(val):
            color = '#10B981' if val > 0 else '#EF4444'
            return f'color: {color}; font-weight: bold'

        fmt = {pnl_col: "{:+,.0f}", wr_col: "{:.1f}%", mdd_col: "{:,.0f}", pf_col: "{:.2f}"}
        st.dataframe(
            res_df.style.format(fmt).map(color_pnl, subset=[pnl_col]),
            use_container_width=True
        )

        # 4. Bar Chart
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=res_df["Strategy"],
            y=res_df[pnl_col],
            marker_color=np.where(res_df[pnl_col] > 0, '#10B981', '#EF4444')
        ))
        fig.update_layout(title=get_text("pnl_comp"), template="plotly_dark", height=400)
        st.plotly_chart(fig, use_container_width=True)

    else:
        st.info("Click 'Run Comparison' to evaluate all strategies on this dataset.")

if __name__ == "__main__":
    main()
