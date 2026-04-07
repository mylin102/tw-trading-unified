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

from backtest.sweep_engine import run_portfolio_grid_sweep, run_multi_asset_backtest # noqa: E402
from backtest.monte_carlo import run_monte_carlo_drawdown # noqa: E402
from ui.backtest_pages.single_test import load_backtest_data # noqa: E402
from strategies.futures.elite_strategies import ELITE_STRATEGIES as STRATEGIES  # noqa: E402
from core.i18n import get_text # noqa: E402

def main():
    st.title(f"🔬 {get_text('nav_sweep')}")

    # 1. Sidebar Controls
    with st.sidebar:
        st.header(get_text("data_source"))
        src_opts = [get_text("today_ind"), get_text("specific_date"), get_text("q1_data")]
        # Default to Q1 data (always available)
        src = st.radio(get_text("select_source"), src_opts, key="sweep_src", index=2)

        date_val = None
        if src == get_text("specific_date"):
            date_val = st.text_input(get_text("enter_date"), datetime.now().strftime("%Y%m%d"), key="sweep_date")

        source_map = {get_text("today_ind"): "today", get_text("specific_date"): "specific", get_text("q1_data"): "q1"}
        df = load_backtest_data(source_map[src], date_val)

        if df is None:
            st.error("No data available. Try 'Q1 Historical Data'.")
            st.stop()

        st.success(get_text("loaded_bars", len(df)))

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

        # Elite strategy-specific params
        if strat_name == "counter_vwap":
            confirm_range = st.multiselect("Confirm Bars Range", [3, 5, 7, 10], default=[5, 7])
            atr_sl_range = st.multiselect("ATR SL Mult Range", [1.5, 2.0, 2.5, 3.0], default=[2.0, 2.5])
            sweep_params["confirm_bars"] = confirm_range
            sweep_params["atr_sl_mult"] = atr_sl_range
        elif strat_name == "spring_upthrust":
            bb_range = st.multiselect("BB Mult Range", [1.5, 2.0, 2.5], default=[2.0])
            kc_range = st.multiselect("KC Mult Range", [0.8, 1.0, 1.2], default=[1.0])
            sweep_params["bb_mult"] = bb_range
            sweep_params["kc_mult"] = kc_range

        total_combinations = np.prod([len(v) for v in sweep_params.values()])
        st.warning(f"Total Combinations: {total_combinations}")
        if total_combinations > 200:
            st.error("Too many combinations! Please reduce ranges to keep it under 200.")
            st.stop()

    # 2. Execution
    if st.button(get_text("btn_run_sweep"), type="primary", use_container_width=True):
        base_cfg = {"strategy": {strat_name: {}}}

        with st.spinner(f"Scanning {total_combinations} combinations..."):
            # Wrap single ticker as single-asset portfolio for sweep engine
            single_asset_dfs = {"TMF": df}
            results_df, trades_dict = run_portfolio_grid_sweep(
                single_asset_dfs, strat_name, sweep_params, base_cfg,
                capital_per_trade=100000.0
            )

            st.session_state["sweep_results"] = results_df
            st.session_state["sweep_trades"] = trades_dict
            st.session_state["sweep_strat_used"] = strat_name

    # 3. Visualization
    if "sweep_results" in st.session_state and st.session_state.get("sweep_strat_used") == strat_name:
        results_df = st.session_state["sweep_results"]
        trades_dict = st.session_state.get("sweep_trades", {})

        # Engine returns PascalCase columns: Total_PnL, Total_Trades, etc.
        pnl_col = "Total_PnL" if "Total_PnL" in results_df.columns else "total_pnl"

        st.header(get_text("perf_heatmap"))

        available_params = [k for k in sweep_params.keys() if len(sweep_params[k]) > 1]

        if len(available_params) >= 2:
            y_axis = st.selectbox("Y-Axis Param", available_params, index=0)
            x_axis = st.selectbox("X-Axis Param", [p for p in available_params if p != y_axis], index=0)
            pivot_df = results_df.groupby([y_axis, x_axis])[pnl_col].mean().reset_index()
            pivot_pnl = pivot_df.pivot(index=y_axis, columns=x_axis, values=pnl_col)
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
        best_idx = results_df[pnl_col].idxmax()
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
        std_pnl = results_df[pnl_col].std()
        avg_pnl = results_df[pnl_col].mean()
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
