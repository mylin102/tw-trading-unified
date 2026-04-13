import streamlit as st
import pandas as pd
import plotly.express as px
import sys
from pathlib import Path
import numpy as np

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.strategy_registry import StrategyRegistry # noqa: E402
from core.parameter_optimizer import GridSearchOptimizer # noqa: E402
from core.backtest_engine import AssetProfile, AssetType # noqa: E402
from core.data_manager import data_manager # noqa: E402
from core.i18n import get_text # noqa: E402

# Asset Profiles
FUTURES_PROFILE = AssetProfile(asset_type=AssetType.FUTURES, point_value=200, margin_per_lot=170000, fee_rate=0.00002, tax_rate=0.00002)
STOCK_PROFILE = AssetProfile(asset_type=AssetType.STOCK, point_value=1, margin_per_lot=0, fee_rate=0.001425, tax_rate=0.003)

def main():
    st.title("🔬 Strategy Parameter Optimizer")
    st.caption("Perform multi-threaded grid search to find the most robust parameter combinations.")

    # 1. Strategy & Data Selection
    reg = StrategyRegistry()
    reg.discover()
    
    with st.sidebar:
        st.header("Step 1: Context")
        all_strats = [s["name"] for s in reg.list_all() if s.get("available")]
        selected_name = st.selectbox("Select Strategy", all_strats)
        strat_obj = reg.get(selected_name)
        
        inventory = data_manager.get_inventory()
        selected_ticker = st.selectbox("Historical Data Source", list(inventory.keys()))
        
        max_workers = st.slider("Parallel Workers", 1, 8, 4)
        initial_bal = st.number_input("Initial Balance", 10000, 1000000, 100000)

    # 2. Dynamic Parameter Range Definition
    st.header(f"⚙️ Parameter Ranges: {selected_name}")
    st.info(strat_obj.metadata.get("description", ""))
    
    param_grid = {}
    
    # We'll use columns to let user define Range (Start, End, Step)
    # For now, let's allow up to 3 parameters to sweep
    schema = getattr(strat_obj, "config_schema", None)
    
    if schema:
        # Pydantic based discovery
        for name, field in schema.model_fields.items():
            if field.annotation in [int, float, Optional[int], Optional[float]]:
                with st.expander(f"Sweep: {name}", expanded=True):
                    c1, c2, c3 = st.columns(3)
                    start_v = c1.number_input("Start", value=float(field.default or 0.0), key=f"s_{name}")
                    end_v = c2.number_input("End", value=float(start_v + 10.0), key=f"e_{name}")
                    step_v = c3.number_input("Step", value=1.0, min_value=0.0001, key=f"st_{name}")
                    
                    if st.checkbox(f"Include {name} in Sweep", value=True, key=f"cb_{name}"):
                        param_grid[name] = np.arange(start_v, end_v + step_v/2, step_v).tolist()
    else:
        # Fallback for simple plugins
        with st.expander("Sweep: entry_score", expanded=True):
            c1, c2, c3 = st.columns(3)
            param_grid["entry_score"] = np.arange(c1.number_input("Start", 0, 100, 10), 
                                                 c2.number_input("End", 0, 100, 30), 
                                                 c3.number_input("Step", 1, 20, 5)).tolist()

    # 3. Run Sweep
    if st.button("🚀 Start Grid Search Optimization", type="primary", use_container_width=True):
        if not param_grid:
            st.error("Select at least one parameter to sweep.")
            st.stop()
            
        df = data_manager.load_historical(selected_ticker)
        if df.empty:
            st.error("Failed to load historical data.")
            st.stop()
            
        profile = FUTURES_PROFILE if strat_obj.metadata.get("asset_class") == "futures" else STOCK_PROFILE
        optimizer = GridSearchOptimizer(profile=profile, initial_capital=initial_bal)
        
        with st.status("Running Optimization Sweep...", expanded=True) as status:
            results_df = optimizer.run_sweep(selected_name, df, param_grid, max_workers=max_workers)
            status.update(label="✅ Optimization Complete", state="complete")
        
        # 4. Display Results
        st.divider()
        st.header("📊 Results Analysis")
        
        # Show Top 10 by CAGR
        st.subheader("Top 10 Combinations (by CAGR)")
        if "cagr" in results_df.columns:
            top_df = results_df.sort_values("cagr", ascending=False).head(10)
            st.dataframe(top_df, use_container_width=True)
            
            # 5. Visualization (Heatmap)
            # If we have 2 or more parameters, show a heatmap
            param_names = list(param_grid.keys())
            if len(param_names) >= 2:
                st.subheader("Parameter Heatmap")
                fig = px.density_heatmap(results_df, x=param_names[0], y=param_names[1], z="cagr",
                                       labels={"cagr": "CAGR (%)"},
                                       color_continuous_scale="Viridis",
                                       title=f"Impact of {param_names[0]} vs {param_names[1]} on CAGR")
                st.plotly_chart(fig, use_container_width=True)
            
            # Parallel Coordinates for higher dimensions
            st.subheader("Multi-dimensional Analysis")
            fig_pc = px.parallel_coordinates(results_df, color="cagr",
                                           dimensions=param_names + ["cagr", "sharpe", "win_rate"],
                                           color_continuous_scale=px.colors.diverging.Tealrose)
            st.plotly_chart(fig_pc, use_container_width=True)
        else:
            st.write(results_df)

if __name__ == "__main__":
    main()
