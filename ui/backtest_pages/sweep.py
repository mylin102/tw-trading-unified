import streamlit as st
import pandas as pd
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
from core.i18n import get_text # noqa: E402

def main():
    st.title(f"🔬 {get_text('nav_sweep')}")

    # 1. Sidebar Controls
    with st.sidebar:
        st.header(get_text("data_source"))
        src_opts = [get_text("today_ind"), get_text("specific_date"), get_text("q1_data")]
        src = st.radio(get_text("select_source"), src_opts, key="sweep_src")
        
        date_val = None
        if src == get_text("specific_date"):
            date_val = st.text_input(get_text("enter_date"), datetime.now().strftime("%Y%m%d"), key="sweep_date")
        
        source_map = {get_text("today_ind"): "today", get_text("specific_date"): "specific", get_text("q1_data"): "q1"}
        df = load_backtest_data(source_map[src], date_val)
        
        if df is not None:
            st.success(get_text("loaded_bars", len(df)))
        else:
            st.stop()

        st.header(get_text("strategy_settings"))
        strat_name = st.selectbox(get_text("select_strategy"), list(STRATEGIES.keys()), key="sweep_strat")
        strat_entry = STRATEGIES[strat_name]
        desc = strat_entry.get("desc", "No description available.") if isinstance(strat_entry, dict) else "No description available."
        st.info(desc)
        
        st.divider()
        st.header(get_text("sweep_ranges"))
        
        # Base Params
        atr_mult_range = st.multiselect("ATR Mult (Exit) Range", [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0], default=[1.5, 2.0, 2.5])
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
    if st.button(get_text("btn_run_sweep"), type="primary", use_container_width=True):
        base_cfg = {"strategy": {strat_name: {}}}
        
        with st.spinner(f"Scanning {total_combinations} combinations..."):
            sweep_data = run_grid_sweep(df, strat_name, sweep_params, base_cfg)
            
            # Robust unpacking
            if isinstance(sweep_data, tuple) and len(sweep_data) >= 2:
                results_df = sweep_data[0]
                trades_dict = sweep_data[1]
            elif isinstance(sweep_data, pd.DataFrame):
                results_df = sweep_data
                trades_dict = {} 
            else:
                st.error(f"Unexpected return from sweep engine: {type(sweep_data)}")
                st.stop()
                
            st.session_state["sweep_results"] = results_df
            st.session_state["sweep_trades"] = trades_dict
            st.session_state["sweep_strat_used"] = strat_name

    # 3. Visualization
    if "sweep_results" in st.session_state and st.session_state.get("sweep_strat_used") == strat_name:
        results_df = st.session_state["sweep_results"]
        trades_dict = st.session_state["sweep_trades"]
        
        st.header(get_text("perf_heatmap"))
        
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
        st.header(get_text("robustness"))
        best_idx = results_df["total_pnl"].idxmax()
        best_params = results_df.loc[best_idx]
        combo_idx = best_params["combo_idx"]
        
        st.subheader(f"{get_text('best_combo')}: Score={best_params.get('entry_score', 'N/A')}, ATR={best_params['atr_mult']}")
        
        real_trades = trades_dict.get(combo_idx, np.array([]))
        
        if len(real_trades) > 0:
            with st.spinner(get_text("shuffling", len(real_trades))):
                mc_results = run_monte_carlo_drawdown(real_trades, 100000.0, iterations=1000)
            
            fig_mc = px.histogram(mc_results, nbins=50, title=get_text("mc_dist"))
            fig_mc.update_layout(template="plotly_dark", showlegend=False)
            st.plotly_chart(fig_mc, use_container_width=True)
            
            var_95 = np.percentile(mc_results, 95)
            st.error(get_text("risk_95", f"{var_95:,.0f}"))
        else:
            st.warning("No trades found for the best combination to run Monte Carlo.")
            
        # 5. Stability Score
        st.divider()
        st.header(get_text("plateau"))
        std_pnl = results_df["total_pnl"].std()
        avg_pnl = results_df["total_pnl"].mean()
        stability = (1 - (std_pnl / abs(avg_pnl))) * 100 if avg_pnl != 0 else 0
        
        c1, c2 = st.columns(2)
        c1.metric(get_text("stability"), f"{stability:.1f}%")
        if stability > 70:
            c1.success(get_text("robust_region"))
        else:
            c1.warning(get_text("fragile_peak"))
        c2.info("High stability means small changes in parameters don't crash performance.")
        
    else:
        st.info("Configure ranges and click 'Run Grid Sweep'.")

if __name__ == "__main__":
    main()
