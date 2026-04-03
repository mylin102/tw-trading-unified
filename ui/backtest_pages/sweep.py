import streamlit as st
import numpy as np
import plotly.express as px
import sys
from pathlib import Path
from datetime import datetime

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.sweep_engine import run_grid_sweep # noqa: E402
from backtest.monte_carlo import run_monte_carlo_drawdown # noqa: E402
from ui.backtest_pages.single_test import load_backtest_data # noqa: E402
from strategies.futures.entry_strategies import STRATEGIES # noqa: E402

def main():
    st.title("🔬 Parameter Sweep & Robustness Analysis")

    # 1. Sidebar Controls
    with st.sidebar:
        st.header("1. Data & Strategy")
        src = st.radio("Select source", ["Today's Indicators", "Specific Date", "Q1 Full Dataset"], key="sweep_src")
        date_val = None
        if src == "Specific Date":
            date_val = st.text_input("Enter date (YYYYMMDD)", datetime.now().strftime("%Y%m%d"), key="sweep_date")
        
        source_map = {"Today's Indicators": "today", "Specific Date": "specific", "Q1 Full Dataset": "q1"}
        df = load_backtest_data(source_map[src], date_val)
        
        if df is not None:
            st.success(f"Loaded {len(df)} bars")
        else:
            st.stop()

        strat_name = st.selectbox("Select strategy", list(STRATEGIES.keys()), key="sweep_strat")
        # 顯示策略說明 (打磨)
        strat_entry = STRATEGIES[strat_name]
        desc = strat_entry.get("desc", "No description available.") if isinstance(strat_entry, dict) else "No description available."
        st.info(desc)
        
        st.divider()
        st.header("2. Sweep Ranges")
        
        # Base Params
        atr_mult_range = st.multiselect("ATR Mult (Exit) Range", [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0], default=[1.5, 2.0, 2.5])
        
        # Strategy Specific Params
        sweep_params = {"atr_mult": atr_mult_range}
        
        if strat_name in ["squeeze_breakout", "vol_squeeze", "trend_follow"]:
            entry_score_range = st.multiselect("Entry Score Range", [10, 15, 20, 25, 30, 40, 50], default=[15, 20, 25])
            sweep_params["entry_score"] = entry_score_range
            
        if strat_name == "vol_squeeze":
            vol_mult_range = st.multiselect("Volume Multiplier Range", [1.0, 1.2, 1.5, 1.8, 2.0, 2.5], default=[1.2, 1.5, 1.8])
            sweep_params["vol_multiplier"] = vol_mult_range
            
        if strat_name == "psar_breakout":
            accel_range = st.multiselect("Acceleration Range", [0.01, 0.02, 0.03, 0.04, 0.05], default=[0.01, 0.02, 0.03])
            sma_range = st.multiselect("SMA Filter Range", [20, 50, 100, 200], default=[20, 50, 100])
            sweep_params["acceleration"] = accel_range
            sweep_params["sma_length"] = sma_range

        total_combinations = np.prod([len(v) for v in sweep_params.values()])
        st.warning(f"Total Combinations: {total_combinations}")
        if total_combinations > 200:
            st.error("Too many combinations! Please reduce ranges to keep it under 200.")
            st.stop()

    # 2. Execution
    if st.button("🚀 Run Grid Sweep", type="primary", use_container_width=True):
        base_cfg = {"strategy": {strat_name: {}}}
        
        with st.spinner(f"Scanning {total_combinations} combinations..."):
            results_df, trades_dict = run_grid_sweep(df, strat_name, sweep_params, base_cfg)
            st.session_state["sweep_results"] = results_df
            st.session_state["sweep_trades"] = trades_dict
            st.session_state["sweep_strat_used"] = strat_name

    # 3. Visualization
    if "sweep_results" in st.session_state and st.session_state.get("sweep_strat_used") == strat_name:
        results_df = st.session_state["sweep_results"]
        trades_dict = st.session_state["sweep_trades"]
        
        st.header("Performance Heatmap")
        
        # ... (keep existing heatmap logic) ...
        available_params = [k for k in sweep_params.keys() if len(sweep_params[k]) > 1]
        
        if len(available_params) >= 2:
            y_axis = st.selectbox("Y-Axis Param", available_params, index=0)
            x_axis = st.selectbox("X-Axis Param", [p for p in available_params if p != y_axis], index=0)
            pivot_df = results_df.groupby([y_axis, x_axis])["total_pnl"].mean().reset_index()
            pivot_pnl = pivot_df.pivot(index=y_axis, columns=x_axis, values="total_pnl")
            fig = px.imshow(pivot_pnl, labels=dict(x=x_axis, y=y_axis, color="Avg Total PnL"),
                            x=pivot_pnl.columns, y=pivot_pnl.index, color_continuous_scale="RdYlGn", aspect="auto")
            fig.update_layout(template="plotly_dark")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.write("Scan at least 2 parameters to see a heatmap.")
            st.dataframe(results_df, use_container_width=True)

        # 4. Robustness Check (Monte Carlo)
        st.divider()
        st.header("🛡️ Robustness: Real Trade Monte Carlo")
        best_idx = results_df["total_pnl"].idxmax()
        best_params = results_df.loc[best_idx]
        combo_idx = best_params["combo_idx"]
        
        st.subheader(f"Best Combination: Score={best_params.get('entry_score', 'N/A')}, ATR={best_params['atr_mult']}")
        
        # Use REAL trades from the sweep engine
        real_trades = trades_dict.get(combo_idx, np.array([]))
        
        if len(real_trades) > 0:
            with st.spinner(f"Shuffling {len(real_trades)} real trades 1,000 times..."):
                mc_results = run_monte_carlo_drawdown(real_trades, 100000.0, iterations=1000)
            
            fig_mc = px.histogram(mc_results, nbins=50, title="Potential Max Drawdown Distribution (Real Trades)")
            fig_mc.update_layout(template="plotly_dark", showlegend=False)
            st.plotly_chart(fig_mc, use_container_width=True)
            
            var_95 = np.percentile(mc_results, 95)
            st.error(f"⚠️ **Real-Trade 95% Risk:** If trade order was unfavorable, drawdown could reach **{var_95:,.0f} TWD**.")
        else:
            st.warning("No trades found for the best combination to run Monte Carlo.")
        
    else:
        st.info("Configure ranges and click 'Run Grid Sweep'.")

if __name__ == "__main__":
    main()
