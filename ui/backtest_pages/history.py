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

from core.backtest_storage import tracker # noqa: E402
from core.monte_carlo import run_monte_carlo # noqa: E402
from core.i18n import get_text # noqa: E402

def main():
    st.title("📚 Backtest Experiment History")
    st.caption("Review, compare and manage past backtest runs from the Experiment Database.")

    # 1. Load Registry
    registry = tracker.list_experiments()
    
    if not registry:
        st.info("No experiments found in the database. Run a backtest and save it to see it here!")
        st.stop()

    # 2. Summary Table
    st.header("🧪 All Experiments")
    
    display_data = []
    for exp in registry:
        row = {
            "ID": exp["exp_id"],
            "Time": exp["timestamp"].split(".")[0].replace("T", " "),
            "Strategy": exp["strategy"],
            "Tag": exp.get("tag") or "-",
            "PnL": exp["metrics"].get("total_pnl", 0),
            "WinRate": f"{exp['metrics'].get('win_rate', 0):.1%}",
            "Sharpe": round(exp["metrics"].get("sharpe", 0), 2),
            "MaxDD": f"{exp['metrics'].get('mdd', 0):.1%}",
            "Git": exp.get("git_hash", "???")
        }
        display_data.append(row)
    
    df_reg = pd.DataFrame(display_data)
    st.dataframe(df_reg, use_container_width=True, hide_index=True)

    st.divider()

    # 3. Experiment Detail Review
    st.header("🔍 Detailed Review")
    exp_id = st.selectbox("Select Experiment to Review", [e["exp_id"] for e in registry])
    
    if exp_id:
        load_res = tracker.load_result(exp_id)
        if not load_res:
            st.error("Failed to load experiment data.")
            st.stop()
            
        result, meta = load_res
        
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Configuration")
            st.json(meta["params"])
        with c2:
            st.subheader("Performance")
            st.write(meta["metrics"])

        # PnL Curve
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=result.equity_curve.index, y=result.equity_curve.values, 
                               name="Equity", fill='tozeroy', line=dict(color="#10B981")))
        fig.update_layout(title=f"Equity Curve: {exp_id}", template="plotly_dark")
        st.plotly_chart(fig, use_container_width=True)

        # Trade Log
        st.subheader("Trade Logs")
        st.dataframe(result.trades, use_container_width=True, hide_index=True)
        
        # --- Monte Carlo Stress Test ---
        st.divider()
        st.header("🎲 Monte Carlo Stress Test")
        st.markdown("Randomly reshuffling historical trades to evaluate strategy resilience and risk of ruin.")
        
        # Get initial capital from params if available, else fallback
        init_cap = meta["params"].get("initial_bal", 1000000)
        mc_res = run_monte_carlo(result.trades, initial_capital=init_cap)
        
        if mc_res:
            mc_c1, mc_c2, mc_c3 = st.columns(3)
            mc_c1.metric("Prob. of Ruin", f"{mc_res['prob_of_ruin']:.1%}")
            mc_c2.metric("95% Confidence MDD", f"{mc_res['mdd_95']:.1%}")
            mc_c3.metric("Median MDD", f"{mc_res['mdd_median']:.1%}")

            # Plot paths
            fig_mc = go.Figure()
            n_plot = min(50, mc_res['n_simulations'])
            for i in range(n_plot):
                fig_mc.add_trace(go.Scatter(
                    y=mc_res['paths'][i], 
                    mode='lines', 
                    line=dict(width=0.5, color='rgba(16, 185, 129, 0.2)'),
                    showlegend=False
                ))
            # Plot original path reconstructed from trades
            orig_path = np.zeros(mc_res['n_trades'] + 1)
            orig_path[0] = init_cap
            orig_path[1:] = init_cap + np.cumsum(result.trades[result.trades["action"] == "EXIT"]["pnl"].values)
            
            fig_mc.add_trace(go.Scatter(
                y=orig_path, 
                mode='lines', 
                line=dict(width=2, color='white'),
                name="Historical Path"
            ))
            fig_mc.update_layout(title="Simulated Equity Paths (Sample 50 vs Historical)", template="plotly_dark")
            st.plotly_chart(fig_mc, use_container_width=True)
            
            # MDD Distribution
            fig_dist = go.Figure(data=[go.Histogram(x=mc_res['max_drawdowns'] * 100, 
                                                  nbinsx=30, marker_color='#EF4444')])
            fig_dist.add_vline(x=mc_res['mdd_95'] * 100, line_dash="dash", line_color="white", 
                             annotation_text="95% VaR")
            fig_dist.update_layout(title="Max Drawdown Distribution (%)", template="plotly_dark", 
                                 xaxis_title="Drawdown %", yaxis_title="Frequency")
            st.plotly_chart(fig_dist, use_container_width=True)
        else:
            st.info("Insufficient trade data for Monte Carlo simulation (minimum 2 trades required).")
        
        # Management
        st.divider()
        if st.button("🗑️ Delete Experiment", type="secondary"):
            tracker.delete_experiment(exp_id)
            st.warning(f"Experiment {exp_id} deleted.")
            st.rerun()

if __name__ == "__main__":
    main()
