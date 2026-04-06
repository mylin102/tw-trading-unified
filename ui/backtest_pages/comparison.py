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
        src = st.radio(get_text("select_source"), src_opts, key="comp_src")
        date_val = None
        if src == get_text("specific_date"):
            date_val = st.text_input(get_text("enter_date"), key="comp_date")
        
        source_map = {get_text("today_ind"): "today", get_text("specific_date"): "specific", get_text("q1_data"): "q1"}
        df = load_backtest_data(source_map[src], date_val)
        
        if df is not None:
            st.success(get_text("loaded_bars", len(df)))
        else:
            st.stop()

        st.divider()
        st.header(get_text("params"))
        atr_mult = st.slider(get_text("atr_mult"), 1.0, 4.0, 2.0, 0.5)
        initial_bal = 100000.0

    # 2. Execution
    if st.button(get_text("btn_run_comp"), type="primary", use_container_width=True):
        results = []
        
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
            
            # 2. Simulate with FULL 16 arguments
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
                exit_on_vwap=True
            )
            
            # 3. Metrics
            metrics_raw = calculate_metrics(pnl, entries, exits, positions, initial_bal)
            metrics = dict(metrics_raw) # Convert Numba typed dict to regular python dict
            metrics["Strategy"] = name
            results.append(metrics)
            
        progress_bar.empty()
        
        # 3. Leaderboard Table
        res_df = pd.DataFrame(results)
        res_df = res_df[["Strategy", "total_pnl", "win_rate", "max_drawdown", "profit_factor", "total_trades"]]
        res_df = res_df.sort_values("total_pnl", ascending=False)
        
        st.header(get_text("ranking"))
        
        # Styled DataFrame
        def color_pnl(val):
            color = '#10B981' if val > 0 else '#EF4444'
            return f'color: {color}; font-weight: bold'

        st.dataframe(
            res_df.style.format({
                "total_pnl": "{:+,.0f}",
                "win_rate": "{:.1f}%",
                "max_drawdown": "{:,.0f}",
                "profit_factor": "{:.2f}"
            }).map(color_pnl, subset=["total_pnl"]),
            use_container_width=True
        )

        # 4. Bar Chart
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=res_df["Strategy"], 
            y=res_df["total_pnl"],
            marker_color=np.where(res_df["total_pnl"] > 0, '#10B981', '#EF4444')
        ))
        fig.update_layout(title=get_text("pnl_comp"), template="plotly_dark", height=400)
        st.plotly_chart(fig, use_container_width=True)

    else:
        st.info("Click 'Run Comparison' to evaluate all strategies on this dataset.")

if __name__ == "__main__":
    main()
