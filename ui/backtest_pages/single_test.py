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

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.signal_generator import generate_signals # noqa: E402
from strategies.futures.squeeze_futures.engine.vectorized import simulate_trades_vectorized, calculate_metrics # noqa: E402
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze # noqa: E402
from strategies.futures.entry_strategies import STRATEGIES # noqa: E402

from core.i18n import get_text # noqa: E402

# ── Paths ──
BASE = ROOT
CONFIG_PATH = BASE / "config" / "futures.yaml"
FUTURES_MKT = BASE / "logs" / "market_data"
TAIFEX_RAW = BASE / "data" / "taifex_raw"

# ── Logic ──
def apply_params_to_config(strategy_name: str, entry_score: int, atr_mult: float):
    """Update futures.yaml with new params and create a backup."""
    if not CONFIG_PATH.exists():
        st.error(f"Config file not found: {CONFIG_PATH}")
        return False
    
    # 1. Create Backup
    backup_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = CONFIG_PATH.with_suffix(f".backup_{backup_time}")
    shutil.copy(CONFIG_PATH, backup_path)
    
    # 2. Update Content
    with open(CONFIG_PATH, "r") as f:
        cfg = yaml.safe_load(f)
    
    if "strategy" not in cfg:
        cfg["strategy"] = {}
    cfg["strategy"]["entry_score"] = entry_score
    if strategy_name not in cfg["strategy"]:
        cfg["strategy"][strategy_name] = {}
    cfg["strategy"][strategy_name]["atr_mult"] = atr_mult
    
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False)
    
    return backup_path

def rollback_config():
    """Restore from the latest backup file."""
    backups = sorted(CONFIG_PATH.parent.glob("futures.yaml.backup_*"))
    if not backups:
        st.error("No backups found to rollback.")
        return False
    
    latest_backup = backups[-1]
    shutil.copy(latest_backup, CONFIG_PATH)
    os.remove(latest_backup) # Consume the backup
    return latest_backup

# Cache version key — bump this to invalidate all cached data
_CACHE_VERSION = "v2"

@st.cache_data(ttl=300, show_spinner=False)
def load_backtest_data(source_type: str, date_str: str = None, _cache_version: str = _CACHE_VERSION):
    """
    Load data for backtesting with caching.
    source_type: 'today', 'specific', 'q1'
    """
    def _read_csv(path):
        if not path.exists():
            return None
        df = pd.read_csv(path)

        if "timestamp" in df.columns or "ts" in df.columns:
            ts_col = "timestamp" if "timestamp" in df.columns else "ts"
            # 統一處理：先轉字串，移除時區偏移 (+HH:MM) 或 UTC 標誌 (Z)
            df[ts_col] = df[ts_col].astype(str).str.replace(r"[+-]\d{2}:\d{2}$", "", regex=True).str.replace("Z", "")
            df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce")

            # 只有當確實被轉換為 datetime 類型時，才進行 tz_localize(None)
            if pd.api.types.is_datetime64_any_dtype(df[ts_col]):
                if getattr(df[ts_col].dt, "tz", None) is not None:
                    df[ts_col] = df[ts_col].dt.tz_localize(None)

            df = df.set_index(ts_col)

        # 標準化 OHLCV 欄位大小寫（在 calculate_futures_squeeze 之前）
        col_map = {}
        for c in df.columns:
            cl = c.lower()
            if cl in ["open", "high", "low", "close", "volume"] and c != cl.capitalize():
                col_map[c] = cl.capitalize()
        if col_map:
            df = df.rename(columns=col_map)

        # 標準化 OHLCV 欄位並重算指標
        df = calculate_futures_squeeze(df)

        return df

    if source_type == "today":
        today_str = datetime.now().strftime("%Y%m%d")
        for tag in ["_PAPER", "_LIVE", ""]:
            f = FUTURES_MKT / f"TMF_{today_str}{tag}_indicators.csv"
            df = _read_csv(f)
            if df is not None:
                return df
    elif source_type == "specific" and date_str:
        f = FUTURES_MKT / f"TMF_{date_str}_indicators.csv"
        # 兼容 _PAPER 或 _LIVE 後綴
        if not f.exists():
            for tag in ["_PAPER", "_LIVE"]:
                f_alt = FUTURES_MKT / f"TMF_{date_str}{tag}_indicators.csv"
                if f_alt.exists():
                    f = f_alt
                    break
        return _read_csv(f)
    elif source_type == "q1":
        f = TAIFEX_RAW / "TMF_5m_taifex.csv"
        return _read_csv(f)
    return None

def main():
    st.title(f"📊 {get_text('nav_single')}")

    # 1. Sidebar Controls (wrapped in form for Streamlit 1.45+)
    with st.sidebar:
        st.header(get_text("data_source"))
        src_opts = [get_text("today_ind"), get_text("specific_date"), get_text("q1_data")]
        # Default to Q1 data (always available)
        src = st.radio(get_text("select_source"), src_opts, index=2)

        date_val = None
        if src == get_text("specific_date"):
            date_val = st.text_input(get_text("enter_date"), datetime.now().strftime("%Y%m%d"))

        source_map = {get_text("today_ind"): "today", get_text("specific_date"): "specific", get_text("q1_data"): "q1"}
        df = load_backtest_data(source_map[src], date_val)

        if df is None:
            st.error("No data available for the selected source. Try 'Q1 Historical Data'.")
            st.stop()

        st.success(get_text("loaded_bars", len(df)))

        with st.form("single_backtest_form"):
            st.divider()
            st.header(get_text("strategy_settings"))
            strat_name = st.selectbox(get_text("select_strategy"), list(STRATEGIES.keys()))
            # 顯示策略說明 (打磨)
            strat_entry = STRATEGIES[strat_name]
            desc = strat_entry.get("desc", "No description available.") if isinstance(strat_entry, dict) else "No description available."
            st.info(desc)

            st.divider()
            st.header(get_text("params"))
            atr_mult = st.slider(get_text("atr_mult"), 0.0, 5.0, 2.0, 0.1)
            entry_score = st.slider(get_text("entry_score"), 0, 100, 20, 5)
            lots = st.number_input(get_text("lots"), 1, 10, 2)
            initial_bal = st.number_input(get_text("initial_bal"), 10000, 1000000, 100000)

            run_btn = st.form_submit_button(get_text("btn_run_single"), type="primary", use_container_width=True)

    # 2. Execution
    if run_btn:
        # Defensive: ensure standard OHLCV column names
        for lower, upper in [("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"), ("volume", "Volume")]:
            if lower in df.columns and upper not in df.columns:
                df = df.rename(columns={lower: upper})
        if "Open" not in df.columns:
            st.error(f"Missing 'Open' column. Available columns: {list(df.columns)[:15]}...")
            st.stop()

        cfg = {
            "strategy": {
                "regime_filter": "mid",
                "entry_score": entry_score,
                strat_name: {"atr_mult": atr_mult}
            }
        }
        
        with st.spinner(get_text("gen_signals")):
            longs, shorts = generate_signals(df, strat_name, cfg)
        
        with st.spinner(get_text("sim_trades")):
            open_arr = df["Open"].values
            close_arr = df["Close"].values
            high_arr = df["High"].values
            low_arr = df["Low"].values
            vwap_arr = df["vwap"].values if "vwap" in df.columns else np.zeros(len(df))
            atr_arr = df["atr"].values if "atr" in df.columns else np.full(len(df), 30.0)

            entries, exits, positions, pnl, reasons = simulate_trades_vectorized(
                open_arr, close_arr, high_arr, low_arr, vwap_arr, atr_arr,
                longs, shorts, 
                initial_balance=initial_bal,
                point_value=10.0,
                fee_per_side=10.0,
                exchange_fee=2.0,
                tax_rate=0.00002,
                max_positions=1,
                lots_per_trade=lots,
                slippage=1.0,
                stop_loss_pts=30,
                atr_mult=atr_mult,
                tp1_pts=30,
                tp1_lots=1,
                exit_on_vwap=True
            )
            
            res = calculate_metrics(pnl, entries, exits, positions, initial_bal)

        # 3. Results Display
        st.header(get_text("results"))
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric(get_text("profit"), f"{res['total_pnl']:+,.0f} TWD")
        m2.metric(get_text("win_rate"), f"{res['win_rate']:.1f}%")
        m3.metric(get_text("pf"), f"{res['profit_factor']:.2f}")
        m4.metric(get_text("mdd"), f"{res['max_drawdown']:,.0f} TWD")
        m5.metric(get_text("trades"), res['total_trades'])

        equity = initial_bal + np.cumsum(pnl)
        df_equity = pd.DataFrame({"equity": equity}, index=df.index)
        df_equity["peak"] = df_equity["equity"].cummax()
        df_equity["drawdown"] = df_equity["peak"] - df_equity["equity"]
        df_equity["in_dd"] = df_equity["drawdown"] > 0
        
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df_equity.index, y=df_equity["equity"], name="Equity", line=dict(color="#10B981", width=2)))
        
        dd_starts = df_equity[(df_equity["in_dd"]) & (~df_equity["in_dd"].shift(1).fillna(False))].index
        dd_ends = df_equity[(~df_equity["in_dd"]) & (df_equity["in_dd"].shift(1).fillna(False))].index
        
        for i in range(min(len(dd_starts), len(dd_ends))):
            fig.add_vrect(
                x0=dd_starts[i], x1=dd_ends[i],
                fillcolor="red", opacity=0.15, layer="below", line_width=0
            )

        fig.update_layout(title=get_text("equity_curve"), template="plotly_dark", height=450)
        st.plotly_chart(fig, use_container_width=True)

        st.subheader(get_text("trade_log"))
        trade_indices = np.where(pnl != 0)[0]
        if len(trade_indices) > 0:
            full_trades_df = pd.DataFrame({
                "Time": df.index[trade_indices],
                "PnL": pnl[trade_indices],
                "Position": positions[trade_indices],
                "Reason": [["ENTRY", "TP1", "EXIT", "STOP"][r] for r in reasons[trade_indices]]
            })
            st.dataframe(full_trades_df, use_container_width=True)

            st.divider()
            st.header("⚙️ Strategy Deployment")
            sc1, sc2 = st.columns(2)
            with sc1:
                if st.button(get_text("btn_apply"), type="primary", use_container_width=True):
                    backup = apply_params_to_config(strat_name, entry_score, atr_mult)
                    if backup:
                        st.success(get_text("config_updated", backup.name))
            with sc2:
                if st.button(get_text("btn_rollback"), use_container_width=True):
                    restored = rollback_config()
                    if restored:
                        st.warning(get_text("config_restored", restored.name))
        else:
            st.info(get_text("no_trades"))
    else:
        st.info("Adjust parameters and click 'Run Backtest'.")

if __name__ == "__main__":
    main()
