import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import yaml
import shutil
import os
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.strategy_registry import StrategyRegistry # noqa: E402
from core.backtest_engine import BacktestEngine, AssetProfile, AssetType # noqa: E402
from core.backtest_storage import tracker # noqa: E402
from core.data_sentinel import data_sentinel # noqa: E402
from core.data_manager import data_manager # noqa: E402
from core.monte_carlo import run_monte_carlo # noqa: E402
from core.i18n import get_text # noqa: E402
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze # noqa: E402

# Initialize Registry
REGISTRY = StrategyRegistry()
REGISTRY.discover()

# Asset Profiles
FUTURES_PROFILE = AssetProfile(
    asset_type=AssetType.FUTURES,
    point_value=200,
    margin_per_lot=170000,
    fee_rate=0.00002,
    tax_rate=0.00002
)

STOCK_PROFILE = AssetProfile(
    asset_type=AssetType.STOCK,
    point_value=1,
    margin_per_lot=0,
    fee_rate=0.001425,
    tax_rate=0.003
)

# ── Paths ──
BASE = ROOT
FUTURES_MKT = BASE / "logs" / "market_data"
TAIFEX_RAW = BASE / "data" / "taifex_raw"

# Cache version key
_CACHE_VERSION = "v7"

@st.cache_data(ttl=300, show_spinner=False)
def load_backtest_data(source_type: str, ticker: str = "TXFR1", _cache_version: str = _CACHE_VERSION):
    """Load data for backtesting with caching."""
    def _read_csv(path):
        if not path.exists(): return None
        try:
            df = pd.read_csv(path)
            if "timestamp" in df.columns or "ts" in df.columns:
                ts_col = "timestamp" if "timestamp" in df.columns else "ts"
                df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce")
                df = df.set_index(ts_col)
            
            # Standardize OHLCV
            col_map = {c: c.capitalize() for c in df.columns if c.lower() in ["open", "high", "low", "close", "volume"]}
            df = df.rename(columns=col_map)
            
            # Defensive OHLCV
            for col in ["Open", "High", "Low", "Close"]:
                if col not in df.columns and "Close" in df.columns:
                    df[col] = df["Close"]
            return df
        except Exception as e:
            st.error(f"Error loading CSV: {e}")
            return None

    if source_type == "today":
        files = sorted(FUTURES_MKT.glob("TMF_*_indicators.csv"), reverse=True)
        if files: return _read_csv(files[0])
    elif source_type == "q1":
        f = TAIFEX_RAW / "TMF_5m_taifex.csv"
        df = _read_csv(f)
        # Auto-calculate indicators for raw Q1 data
        if df is not None and "ema_filter" not in df.columns:
            try:
                df = calculate_futures_squeeze(df, bb_length=20)
            except Exception:
                pass  # Return raw data if indicator calculation fails
        return df
    elif source_type == "parquet":
        return data_manager.load_historical(ticker)
    return None

def render_dynamic_params(strategy):
    """Generate UI inputs based on strategy config_schema or defaults."""
    st.divider()
    st.subheader(get_text("params"))
    
    params = {}
    schema = getattr(strategy, "config_schema", None)
    if schema:
        for name, field in schema.model_fields.items():
            label = name.replace("_", " ").title()
            if field.annotation is bool:
                params[name] = st.checkbox(label, value=field.default or False)
            elif field.annotation in [int, float, Optional[int], Optional[float]]:
                params[name] = st.number_input(label, value=float(field.default or 0.0))
    else:
        # Defaults for legacy/common plugins
        params["entry_score"] = st.slider(get_text("entry_score"), 0, 100, 20, 5)
        params["atr_mult"] = st.slider(get_text("atr_mult"), 0.0, 5.0, 2.0, 0.1)
    
    return params

def main():
    st.title(f"📊 {get_text('nav_single')}")

    # 1. Sidebar Control
    with st.sidebar:
        st.header(get_text("data_source"))
        src_options = ["Historical DB (Parquet)", "Q1 (CSV)", "Today (CSV)"]
        src_label = st.radio(get_text("select_source"), src_options, index=0)
        source_map = {"Today (CSV)": "today", "Q1 (CSV)": "q1", "Historical DB (Parquet)": "parquet"}
        
        target_ticker = "TXFR1"
        if src_label == "Historical DB (Parquet)":
            inventory = data_manager.get_inventory()
            if inventory:
                target_ticker = st.selectbox("Select DB Ticker", list(inventory.keys()))
            else:
                st.warning("Historical DB is empty. Use 'Data Management' to expand.")
        
        load_btn = st.button("📥 Load Data", type="primary", use_container_width=True)
        if load_btn or "bt_df" in st.session_state:
            if load_btn:
                df = load_backtest_data(source_map[src_label], ticker=target_ticker)
                st.session_state["bt_df"] = df
            else:
                df = st.session_state["bt_df"]

            if df is None or df.empty:
                st.error("No data loaded. Check files in data/ or logs/market_data/.")
                st.stop()

            st.success(get_text("loaded_bars", len(df)))

            # --- Data Health Audit ---
            with st.expander("🛡️ Data Health"):
                gaps = data_sentinel.audit_gaps(df)
                if not gaps:
                    st.success("Data is complete (No gaps)")
                else:
                    st.warning(f"Detected {len(gaps)} gaps")
                    if st.button("🔧 Repair with Backfiller"):
                        with st.spinner("Repairing gaps via Shioaji..."):
                            import subprocess
                            subprocess.run(["python3", "scripts/sync/unified_backfiller.py"], check=True)
                            st.rerun()
            st.divider()

            with st.form("backtest_config"):
                st.header(get_text("strategy_settings"))
                all_strats = REGISTRY.list_all()
                available_strats = [s["name"] for s in all_strats if s.get("available")]
                
                if not available_strats:
                    st.error("No available strategies found.")
                    st.form_submit_button("Retry", disabled=True)
                    st.stop()
                    
                selected_name = st.selectbox(get_text("select_strategy"), available_strats)
                strat_obj = REGISTRY.get(selected_name)
                meta = strat_obj.metadata
                st.info(meta.get("description", "No description"))
                
                custom_params = render_dynamic_params(strat_obj)
                
                st.divider()
                initial_bal = st.number_input(get_text("initial_bal"), 10000, 1000000, 100000)
                
                run_btn = st.form_submit_button(get_text("btn_run_single"), type="primary", use_container_width=True)
                
                # 2. Execution (inside form to access run_btn)
                if run_btn:
                    profile = FUTURES_PROFILE if meta.get("asset_class") == "futures" else STOCK_PROFILE
                    engine = BacktestEngine(profile=profile, initial_capital=initial_bal)
                    full_config = {"params": custom_params}
                    
                    with st.spinner(f"Simulating {selected_name}..."):
                        result = engine.run(df, strat_obj, config=full_config)
                    
                    # 3. Results Display
                    if result.metrics and result.metrics.get("trade_count", 0) > 0:
                        m = result.metrics
                        st.header(get_text("results"))
            
                        # --- Save Experiment ---
                        with st.expander("💾 Save this Experiment"):
                            c1, c2 = st.columns([3, 1])
                            exp_tag = c1.text_input("Tag (optional)", placeholder="e.g. baseline, test_v1", key="exp_tag")
                            if c2.button("Save", use_container_width=True):
                                exp_id = tracker.save_experiment(result, params=custom_params, tag=exp_tag)
                                st.success(f"Saved: {exp_id}")
                        
                        st.divider()
                        c1, c2, c3, c4, c5, c6 = st.columns(6)
                        c1.metric(get_text("total_pnl"), f"{m['total_pnl']:+,.0f}")
                        c2.metric(get_text("win_rate"), f"{m['win_rate']:.1%}")
                        c3.metric("Sharpe", f"{m['sharpe']:.2f}")
                        c4.metric("MaxDD", f"{m['mdd']:.1%}")
                        c5.metric("CAGR", f"{m.get('cagr', 0):.1%}")
                        c6.metric("Profit Factor", f"{m.get('profit_factor', 0):.2f}")

                        # --- Quick Monte Carlo Preview ---
                        st.divider()
                        with st.expander("🎲 Monte Carlo Risk Audit", expanded=True):
                            mc = run_monte_carlo(result.trades, initial_capital=initial_bal)
                            if mc:
                                sc1, sc2, sc3 = st.columns(3)
                                sc1.metric("Prob. of Ruin", f"{mc['prob_of_ruin']:.1%}")
                                sc2.metric("95% VaR MDD", f"{mc['mdd_95']:.1%}", 
                                          delta=f"{mc['mdd_95'] - m['mdd']:.1%}", delta_color="inverse")
                                sc3.metric("Median MDD", f"{mc['mdd_median']:.1%}")
                                st.caption("Risk VaR shows how much worse the MDD can get if trade order is unfavorable.")
                            else:
                                st.info("Generating more trades will enable deeper risk analysis.")

                        # PnL Chart
                        fig = go.Figure()
                        fig.add_trace(go.Scatter(x=result.equity_curve.index, y=result.equity_curve.values, 
                                               name="Equity", fill='tozeroy', line=dict(color="#10B981")))
                        fig.update_layout(title=get_text("equity_curve"), template="plotly_dark", height=450)
                        st.plotly_chart(fig, use_container_width=True)

                        # Trade Log
                        st.subheader(get_text("trade_log"))
                        st.dataframe(result.trades, use_container_width=True, hide_index=True)
                    else:
                        st.warning("⚠️ No trades generated during this period.")
                        if not result.equity_curve.empty:
                            fig = go.Figure()
                            fig.add_trace(go.Scatter(x=result.equity_curve.index, y=result.equity_curve.values, name="Equity", line=dict(color="#94A3B8")))
                            fig.update_layout(title="Equity Curve (No Trades)", template="plotly_dark")
                            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Click 'Load Data' to begin.")
            st.stop()

if __name__ == "__main__":
    main()
