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

def main():
    st.title("🏆 Strategy Leaderboard")
    st.caption("Compare all registered strategies side-by-side on the same dataset.")

    # 1. Sidebar Controls
    with st.sidebar:
        st.header("1. Dataset")
        src = st.radio("Select source", ["Today's Indicators", "Specific Date", "Q1 Full Dataset"], key="comp_src")
        date_val = None
        if src == "Specific Date":
            date_val = st.text_input("Enter date (YYYYMMDD)", key="comp_date")
        
        source_map = {"Today's Indicators": "today", "Specific Date": "specific", "Q1 Full Dataset": "q1"}
        df = load_backtest_data(source_map[src], date_val)
        
        if df is not None:
            st.success(f"Loaded {len(df)} bars")
        else:
            st.stop()

        st.divider()
        st.header("2. Base Settings")
        atr_mult = st.slider("ATR Multiplier (Exit)", 1.0, 4.0, 2.0, 0.5)
        initial_bal = 100000.0

    # 2. Execution
    if st.button("🏁 Run Comparison", type="primary", use_container_width=True):
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
            
            # 2. Simulate
            _, _, _, pnl, _ = simulate_trades_vectorized(
                open_arr, close_arr, high_arr, low_arr, vwap_arr, atr_arr,
                longs, shorts, 
                initial_balance=initial_bal,
                atr_mult=atr_mult,
                exit_on_vwap=True
            )
            
            # 3. Metrics
            metrics = calculate_metrics(pnl, np.zeros(1), np.zeros(1), np.zeros(1), initial_bal)
            metrics["Strategy"] = name
            results.append(metrics)
            
        progress_bar.empty()
        
        # 3. Leaderboard Table
        res_df = pd.DataFrame(results)
        res_df = res_df[["Strategy", "total_pnl", "win_rate", "max_drawdown", "profit_factor", "total_trades"]]
        res_df = res_df.sort_values("total_pnl", ascending=False)
        
        st.header("Ranking")
        
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
            }).applymap(color_pnl, subset=["total_pnl"]),
            use_container_width=True
        )

        # 4. Bar Chart
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=res_df["Strategy"], 
            y=res_df["total_pnl"],
            marker_color=np.where(res_df["total_pnl"] > 0, '#10B981', '#EF4444')
        ))
        fig.update_layout(title="PnL Comparison", template="plotly_dark", height=400)
        st.plotly_chart(fig, use_container_width=True)

    else:
        st.info("Click 'Run Comparison' to evaluate all strategies on this dataset.")

if __name__ == "__main__":
    main()
