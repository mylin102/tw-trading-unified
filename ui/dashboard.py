#!/usr/bin/env python3
"""
tw-trading-unified — 整合儀表板 v2
4 tabs: 總覽 / 期貨 / 選擇權 / 設定
"""
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).parent.parent))

import streamlit as st
import pandas as pd
import yaml
import datetime
import os
from core.date_utils import get_session_date_str
import subprocess
import time
from pathlib import Path
from dotenv import load_dotenv
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from streamlit_autorefresh import st_autorefresh

# V-Model fix: Clear cache on startup to avoid stale duplicate data
if "_cache_cleared" not in st.session_state:
    st.session_state._cache_cleared = True
    st.cache_data.clear()

# Ensure project root is in sys.path for strategy imports
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load environment variables early
load_dotenv(Path(__file__).parent.parent / ".env")

st.set_page_config(page_title="Trading Unified", page_icon="📊", layout="wide")

# ── Custom CSS ──
st.markdown("""
    <style>
    /* Force modern sans-serif font stack */
    html, body, [class*="css"]  {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    }
    /* Ensure numbers are easy to compare vertically */
    [data-testid="stMetricValue"], .stMarkdown code, .stTable {
        font-variant-numeric: tabular-nums;
        font-family: 'Inter', 'Roboto Mono', monospace;
    }
    /* Muted divider line style */
    hr {
        margin-top: 1rem;
        margin-bottom: 1rem;
        border: 0;
        border-top: 1px solid rgba(49, 51, 63, 0.1);
    }
    </style>
    """, unsafe_allow_html=True)

# ── 密碼保護 ──
def check_password():
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False
    if st.session_state["authenticated"]:
        return True
    pwd = st.text_input("🔒 請輸入密碼", type="password")
    if pwd == os.environ.get("DASHBOARD_PASSWORD", "trading2026"):
        st.session_state["authenticated"] = True
        st.rerun()
    elif pwd:
        st.error("密碼錯誤")
    return False

if not check_password():
    st.stop()

# 每 30 秒自動刷新（登入後才啟用）
st_autorefresh(interval=30_000, key="data_refresh")

BASE = Path(__file__).parent.parent
TODAY = datetime.datetime.now().strftime("%Y-%m-%d")
DATE_STR = get_session_date_str()  # Single source: shared with main.py

# ── YAML helpers ──
def load_yaml(path):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}

def save_yaml(path, data):
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

# ── Configs ──
FUTURES_CFG_PATH = BASE / "config" / "futures.yaml"
OPTIONS_CFG_PATH = BASE / "config" / "options_strategy.yaml"
RISK_CFG_PATH = BASE / "config" / "risk_global.yaml"
STOCK_CFG_PATH = BASE / "config" / "stocks.yaml"
futures_cfg = load_yaml(FUTURES_CFG_PATH)
options_cfg = load_yaml(OPTIONS_CFG_PATH)
risk_cfg = load_yaml(RISK_CFG_PATH)
stock_cfg = load_yaml(STOCK_CFG_PATH)
f_live = futures_cfg.get("live_trading", False)
o_live = options_cfg.get("live_trading", False)
s_live = stock_cfg.get("live_trading", False)
alloc = risk_cfg.get("allocation", {})
reserve_pct = risk_cfg.get("account", {}).get("margin_reserve_pct", 0.20)

# ── Paths ──
RESTART_FLAG = BASE / ".restart"

def trigger_restart():
    RESTART_FLAG.touch()
    st.toast("🔄 正在重啟 monitor（約 30 秒）...")
OPTIONS_REPO = BASE / "strategies" / "options"
FUTURES_MKT = BASE / "logs" / "market_data"
FUTURES_TRADES = BASE / "exports" / "trades"
OPTIONS_DATA = OPTIONS_REPO / "logs" / ("live_trading" if o_live else "paper_trading")

# ── Filter today only ──
def filter_today(df, ts_col="timestamp"):
    if df is None or df.empty:
        return df
    try:
        # V-Model fix 1: Deduplicate columns first (narwhals/Plotly require unique columns)
        if df.columns.duplicated().any():
            seen_cols = set()
            keep_cols = []
            for col in df.columns:
                if col not in seen_cols:
                    seen_cols.add(col)
                    keep_cols.append(col)
            df = df[keep_cols].copy()

        # V-Model fix 2: Ensure ts_col exists and is unique
        if ts_col not in df.columns:
            return df

        # 統一處理：先轉字串，移除時區偏移 (+HH:MM) 或 UTC 標誌 (Z)
        df[ts_col] = df[ts_col].astype(str).str.replace(r"[+-]\d{2}:\d{2}$", "", regex=True).str.replace("Z", "")
        # 強制轉為 datetime 且去時區 (naive)
        df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce").dt.tz_localize(None)
        df = df.dropna(subset=[ts_col])

        if df.empty:
            return df

        # 改進：自動抓取資料中的最新日期
        latest_date = df[ts_col].dt.date.max()
        filtered_df = df[df[ts_col].dt.date == latest_date].copy()

        # 確保排序
        filtered_df = filtered_df.sort_values(ts_col)

        # V-Model fix 3: 過濾 fallback 假資料 with full scalar safety
        for col in ["close", "price_mtx"]:
            if col in filtered_df.columns and len(filtered_df) > 1:
                # Ensure scalar: use .iat[-1] which always returns scalar
                latest_val = filtered_df[col].iat[-1]
                # Convert to float safely
                try:
                    latest_val_float = float(latest_val)
                except (TypeError, ValueError):
                    continue  # Skip this column if can't convert
                
                if pd.notna(latest_val_float) and latest_val_float > 0:
                    threshold = latest_val_float * 0.5
                    # Use .gt() for safe Series comparison
                    mask = filtered_df[col].gt(threshold)
                    filtered_df = filtered_df[mask].copy()
        return filtered_df
    except Exception as e:
        st.error(f"資料過濾錯誤: {e}")
        return df

# ── Chart builder (unified style) ──
def make_price_score_chart(df, price_col, title, ts_col="timestamp", signals=None):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.05)
    # V-Model fix: Convert to numpy arrays to avoid narwhals duplicate check
    p_col = price_col.lower() if price_col.lower() in df.columns else price_col
    
    # 1. Price Line
    fig.add_trace(go.Scatter(
        x=df[ts_col].to_numpy(),
        y=df[p_col].to_numpy(),
        name=price_col,
        line=dict(width=1.5, color="#1f77b4")
    ), row=1, col=1)

    # 2. Add Signal Markers if available
    if signals is not None and not signals.empty and "action" in signals.columns:
        # Buy signals (Green Triangles Up)
        buys = signals[signals["action"].str.contains("BUY", case=False, na=False)]
        if not buys.empty:
            fig.add_trace(go.Scatter(
                x=buys[ts_col],
                y=buys["price"],
                mode="markers",
                marker=dict(symbol="triangle-up", size=12, color="#00cc66", line=dict(width=1, color="white")),
                name="BUY"
            ), row=1, col=1)

        # Sell signals (Red Triangles Down)
        sells = signals[signals["action"].str.contains("SELL", case=False, na=False)]
        if not sells.empty:
            fig.add_trace(go.Scatter(
                x=sells[ts_col],
                y=sells["price"],
                mode="markers",
                marker=dict(symbol="triangle-down", size=12, color="#ff4444", line=dict(width=1, color="white")),
                name="SELL"
            ), row=1, col=1)

        # Exit signals (Orange Diamonds)
        exits = signals[signals["action"].str.contains("EXIT|COVER", case=False, na=False)]
        if not exits.empty:
            fig.add_trace(go.Scatter(
                x=exits[ts_col],
                y=exits["price"],
                mode="markers",
                marker=dict(symbol="diamond", size=10, color="#ffa500", line=dict(width=1, color="white")),
                name="EXIT"
            ), row=1, col=1)

    # 3. Score Bar Chart
    if "score" in df.columns:
        scores = df["score"].to_numpy()
        colors = ["#00cc66" if s >= 0 else "#ff4444" for s in scores]
        fig.add_trace(go.Bar(
            x=df[ts_col].to_numpy(),
            y=scores,
            name="Score",
            marker_color=colors
        ), row=2, col=1)
    
    fig.update_layout(height=400, margin=dict(t=10, b=10, l=40, r=20), showlegend=False)
    fig.update_yaxes(tickformat=",.0f")
    return fig

# ── PnL helpers ──
def calc_futures_pnl(trades_df):
    """從期貨 trades CSV 的 pnl_cash 欄位累計"""
    if trades_df is None or trades_df.empty:
        return None
    if "pnl_cash" not in trades_df.columns:
        return None
    trades_df["pnl_cash"] = pd.to_numeric(trades_df["pnl_cash"], errors="coerce").fillna(0)
    exits = trades_df[trades_df["pnl_cash"] != 0].copy()
    if exits.empty:
        return None
    exits["cum_pnl"] = exits["pnl_cash"].cumsum()
    return exits[["timestamp", "cum_pnl"]].rename(columns={"cum_pnl": "pnl"})

def calc_options_pnl(ledger_df):
    """從選擇權 ledger 的 PnL 欄位累計"""
    if ledger_df is None or ledger_df.empty:
        return None
    if "PnL" not in ledger_df.columns:
        return None
    ledger_df["PnL"] = pd.to_numeric(ledger_df["PnL"], errors="coerce").fillna(0)
    exits = ledger_df[ledger_df["PnL"] != 0].copy()
    if exits.empty:
        return None
    exits["cum_pnl"] = exits["PnL"].cumsum()
    return exits[["Timestamp", "cum_pnl"]].rename(columns={"Timestamp": "timestamp", "cum_pnl": "pnl"})

def calc_stock_pnl(trades_df):
    """從台股 trades 的 pnl_cash 欄位累計"""
    if trades_df is None or trades_df.empty:
        return None
    if "pnl_cash" not in trades_df.columns:
        return None
    trades_df["pnl_cash"] = pd.to_numeric(trades_df["pnl_cash"], errors="coerce").fillna(0)
    exits = trades_df[trades_df["pnl_cash"] != 0].copy()
    if exits.empty:
        return None
    exits["cum_pnl"] = exits["pnl_cash"].cumsum()
    return exits[["timestamp", "cum_pnl"]].rename(columns={"cum_pnl": "pnl"})


def make_pnl_chart(pnl_df, title):
    if pnl_df is None or pnl_df.empty:
        return None
    pnl_df["timestamp"] = pd.to_datetime(pnl_df["timestamp"], format="mixed")
    color = "#00cc66" if pnl_df["pnl"].iloc[-1] >= 0 else "#ff4444"
    fig = go.Figure()
    # V-Model fix: Convert to numpy arrays to avoid narwhals duplicate check
    fig.add_trace(go.Scatter(
        x=pnl_df["timestamp"].to_numpy(),
        y=pnl_df["pnl"].to_numpy(),
        fill="tozeroy",
        line=dict(color=color, width=1.5),
        name="PnL"
    ))
    fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5)
    fig.update_layout(height=250, margin=dict(t=10, b=10, l=40, r=20), title_text=title, title_font_size=14, yaxis_tickformat=",.0f")
    return fig

@st.cache_data(ttl=5)
def load_futures_indicators():
    def _read_and_standardize(path):
        try:
            df = pd.read_csv(path)
            # V-Model fix: Immediate deduplication after reading
            if df.columns.duplicated().any():
                df = df.loc[:, ~df.columns.duplicated()].copy()
            
            # 1. 處理 timestamp
            if "timestamp" not in df.columns:
                if df.index.name == "timestamp" or df.index.name == "ts":
                    df = df.reset_index()
                elif "ts" in df.columns:
                    df = df.rename(columns={"ts": "timestamp"})
                else:
                    df = df.rename(columns={df.columns[0]: "timestamp"})

            # 2. V-Model fix: Remove case-insensitive duplicate columns BEFORE renaming
            #    CSV files may have BOTH "Open,High,Low,Close" AND "open,high,low,close"
            col_map = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume", "Amount": "amount"}
            for upper, lower in col_map.items():
                if upper in df.columns and lower in df.columns:
                    # Drop the uppercase, keep lowercase (they have same data)
                    df = df.drop(columns=[upper])

            # 3. 統一 OHLC 欄位為小寫
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            return df
        except Exception:
            return None

    # 1. 優先找今天所有可能的檔案並合併
    #    夜盤 15:00 後也要讀明天的檔案
    import datetime as dt
    now = dt.datetime.now()
    today_files = [DATE_STR]
    if now.hour >= 15:
        tomorrow = (now + dt.timedelta(days=1)).strftime("%Y%m%d")
        today_files.append(tomorrow)
    
    all_dfs = []
    for date_part in today_files:
        for tag in ["", "_LIVE", "_PAPER", "_DRY"]:
            f = FUTURES_MKT / f"TMF_{date_part}{tag}_indicators.csv"
            if f.exists():
                df = _read_and_standardize(f)
                if df is not None and "timestamp" in df.columns:
                    # 確保 timestamp 是 datetime
                    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                    # 如果是明天的檔案，只取 >=15:00 的夜盤資料
                    if date_part != DATE_STR:
                        df = df[df["timestamp"].dt.hour >= 15]
                    if not df.empty:
                        all_dfs.append(df)
    
    if all_dfs:
        common_cols = set(all_dfs[0].columns)
        for df in all_dfs[1:]:
            common_cols &= set(df.columns)
        if "timestamp" not in common_cols:
            common_cols.add("timestamp")
        cleaned_dfs = [df[list(common_cols)] for df in all_dfs]
        merged = pd.concat(cleaned_dfs).drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
        result = filter_today(merged)
    else:
        # 2. 備案：找目錄下最新的 CSV
        try:
            all_files = list(FUTURES_MKT.glob("TMF_*_indicators.csv"))
            if all_files:
                latest_file = max(all_files, key=os.path.getmtime)
                df = _read_and_standardize(latest_file)
                if df is not None and "timestamp" in df.columns:
                    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                    if DATE_STR not in str(latest_file) and now.hour >= 15:
                        df = df[df["timestamp"].dt.hour >= 15]
                    result = filter_today(df) if not df.empty else None
        except Exception:
            result = None

    # Stale data detection
    if result is not None and not result.empty and "timestamp" in result.columns:
        try:
            result_ts = result["timestamp"].copy()
            if not pd.api.types.is_datetime64_any_dtype(result_ts):
                result_ts = pd.to_datetime(result_ts, errors="coerce")
            result_ts = result_ts.dropna()
            if not result_ts.empty:
                age_secs = (pd.Timestamp.now() - result_ts.max()).total_seconds()
                if age_secs > 600:
                    st.warning(f"⚠️ 期貨資料停滯 {age_secs/60:.0f} 分鐘")
        except Exception:
            pass
    return result

@st.cache_data(ttl=5)
def load_futures_trades():
    for d in [FUTURES_TRADES]:
        f = d / f"TMF_{DATE_STR}_trades.csv"
        if f.exists():
            try:
                return pd.read_csv(f)
            except Exception:
                pass
    return None

OPTIONS_SUB = "live_trading" if o_live else "paper_trading"

@st.cache_data(ttl=5)
def load_options_indicators():
    # 1. 優先找今天的
    best = None
    for sub in ["live_trading", "paper_trading"]:
        f = OPTIONS_REPO / "logs" / sub / f"OPTIONS_{DATE_STR}_indicators.csv"
        if f.exists() and f.stat().st_mtime > (best[1] if best else 0):
            best = (f, f.stat().st_mtime)

    # 2. 備案：找目錄下最新的任何指標檔案（防斷鍊）
    if not best:
        try:
            all_opt_files = list((OPTIONS_REPO / "logs").rglob("OPTIONS_*_indicators.csv"))
            if all_opt_files:
                latest_f = max(all_opt_files, key=os.path.getmtime)
                best = (latest_f, latest_f.stat().st_mtime)
        except Exception:
            pass

    if best:
        try:
            df = pd.read_csv(best[0])
            if df.columns.duplicated().any():
                df = df.loc[:, ~df.columns.duplicated()].copy()
            result = filter_today(df, ts_col="timestamp")

            # Stale data detection: if latest bar is > 10 min old, warn
            if result is not None and not result.empty and "timestamp" in result.columns:
                try:
                    result["timestamp"] = pd.to_datetime(result["timestamp"], errors="coerce")
                    result = result.dropna(subset=["timestamp"])
                    if not result.empty:
                        latest_bar = result["timestamp"].max()
                        age_secs = (pd.Timestamp.now() - latest_bar).total_seconds()
                        if age_secs > 600:  # 10 minutes
                            st.warning(f"⚠️ 資料停滯 {age_secs/60:.0f} 分鐘 — main.py 可能未運行")
                except Exception:
                    pass

            return result
        except Exception:
            pass
    return None

@st.cache_data(ttl=5)
def load_options_ledger():
    f = OPTIONS_REPO / "logs" / OPTIONS_SUB / "options_trade_ledger.csv"
    if f.exists():
        try:
            return pd.read_csv(f, parse_dates=["Timestamp"])
        except Exception:
            pass
    return None

@st.cache_data(ttl=5)
def load_options_equity():
    f = OPTIONS_REPO / "logs" / OPTIONS_SUB / "equity_curve.csv"
    if f.exists():
        try:
            return pd.read_csv(f, parse_dates=["timestamp"])
        except Exception:
            pass
    return None

@st.cache_data(ttl=5)
def load_stock_trades(mode="PAPER"):
    f = FUTURES_TRADES / f"STOCK_{DATE_STR}_{mode}_trades.csv"
    if f.exists():
        try:
            return pd.read_csv(f)
        except Exception:
            pass
    return None

@st.cache_data(ttl=5)
def load_stock_indicators(ticker):
    f = FUTURES_MKT / f"STOCK_{ticker}_{DATE_STR}_indicators.csv"
    if f.exists():
        try:
            df = pd.read_csv(f)
            if df.columns.duplicated().any():
                df = df.loc[:, ~df.columns.duplicated()].copy()
            if "ts" in df.columns:
                df = df.rename(columns={"ts": "timestamp"})
            elif "Date" in df.columns:
                df = df.rename(columns={"Date": "timestamp"})
            return df
        except Exception:
            pass
    return None

# ── Header ──
def mode_badge(live):
    return "🔴 LIVE" if live else "📝 PAPER"

hc = st.columns([1.5, 1, 1, 1, 1.5])
hc[0].title("Trading Unified")
hc[1].metric("期貨 TMF", mode_badge(f_live))
hc[2].metric("選擇權 TXO", mode_badge(o_live))
hc[3].metric("台股 Stocks", mode_badge(s_live))
hc[4].caption(f"📅 {TODAY}")

if f_live or o_live or s_live:
    st.markdown('<div style="background:#ff4444;color:white;padding:8px;text-align:center;border-radius:4px;font-weight:bold;">⚠️ LIVE TRADING ACTIVE</div>', unsafe_allow_html=True)

def _monitor_status():
    try:
        r = subprocess.run(["pgrep", "-f", "main.py"], capture_output=True)
        return "🟢 Running" if r.returncode == 0 else "🔴 Stopped"
    except Exception:
        return "⚪ Unknown"

st.caption(f"更新: {datetime.datetime.now().strftime('%H:%M:%S')} | Monitor: {_monitor_status()}")

# ── Tabs ──
tab_overview, tab_futures, tab_options, tab_stocks, tab_settings = st.tabs(["總覽", "期貨 TMF", "選擇權 TXO", "台股 Stocks", "設定"])

# ════════════════════════════════════════
# Tab 1: 總覽
# ════════════════════════════════════════
with tab_overview:
    col1, col2 = st.columns(2)
    f_df = load_futures_indicators()
    o_df = load_options_indicators()

    with col1:
        st.header(f"期貨 TMF ({mode_badge(f_live)})")
        if f_df is not None and not f_df.empty:
            last = f_df.iloc[-1]
            c1, c2, c3 = st.columns(3)
            # V-Model fix: Handle duplicate columns by taking first match
            cl_val = last.get('close') if 'close' in last else last.get('Close', 0)
            if hasattr(cl_val, 'iloc'):
                cl_val = float(cl_val.iloc[0]) if len(cl_val) > 0 else 0.0
            else:
                cl_val = float(cl_val or 0)
            sc_val = last.get('score', 0)
            if hasattr(sc_val, 'iloc'):
                sc_val = float(sc_val.iloc[0]) if len(sc_val) > 0 else 0.0
            else:
                sc_val = float(sc_val or 0)
            c1.metric("Close", f"{cl_val:.0f}")
            c2.metric("Score", f"{sc_val:.1f}")
            c3.metric("Bars", len(f_df))
        else:
            st.info("無期貨指標數據")
        ft = load_futures_trades()
        st.write(f"今日交易: {len(ft) if ft is not None else 0} 筆")

    with col2:
        st.header(f"選擇權 TXO ({mode_badge(o_live)})")
        if o_df is not None and not o_df.empty:
            last = o_df.iloc[-1]
            c1, c2, c3 = st.columns(3)
            # V-Model fix: Handle duplicate columns by taking first match
            mtx_val = last.get('price_mtx', 0)
            if hasattr(mtx_val, 'iloc'):
                mtx_val = float(mtx_val.iloc[0]) if len(mtx_val) > 0 else 0.0
            else:
                mtx_val = float(mtx_val or 0)
            sc_val = last.get('score', 0)
            if hasattr(sc_val, 'iloc'):
                sc_val = float(sc_val.iloc[0]) if len(sc_val) > 0 else 0.0
            else:
                sc_val = float(sc_val or 0)
            c1.metric("MTX", f"{mtx_val:.0f}")
            c2.metric("Score", f"{sc_val:.1f}")
            c3.metric("Bars", len(o_df))
        else:
            st.info("無選擇權指標數據")
        ol = load_options_ledger()
        if ol is not None and not ol.empty and "Timestamp" in ol.columns:
            ol["Timestamp"] = pd.to_datetime(ol["Timestamp"], errors="coerce")
            ol = ol.dropna(subset=["Timestamp"])
            today_l = ol[ol["Timestamp"].dt.strftime("%Y%m%d") == DATE_STR]
            entries = today_l[today_l["Action"].str.contains("ENTRY", na=False)]
            st.write(f"今日進場: {len(entries)} 筆")
        else:
            st.write("今日交易: 0 筆")

    # ── 總覽圖：指數走勢（雙軸：期貨 + 選擇權 MTX）──
    st.header("今日指數走勢")
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    has_data = False
    if f_df is not None and not f_df.empty:
        # V-Model fix: Convert to numpy arrays to avoid narwhals duplicate check
        f_close = f_df["close"] if "close" in f_df.columns else f_df["Close"]
        fig.add_trace(go.Scatter(
            x=f_df["timestamp"].to_numpy(),
            y=f_close.to_numpy(),
            name="TMF",
            line=dict(color="#1f77b4", width=1.5)
        ), secondary_y=False)
        has_data = True
    if o_df is not None and not o_df.empty and "price_mtx" in o_df.columns:
        fig.add_trace(go.Scatter(
            x=o_df["timestamp"].to_numpy(),
            y=o_df["price_mtx"].to_numpy(),
            name="MTX (Options)",
            line=dict(color="#ff7f0e", width=1.5)
        ), secondary_y=True)
        has_data = True
    if has_data:
        fig.update_layout(height=350, margin=dict(t=10, b=10, l=40, r=20), legend=dict(orientation="h", y=1.02))
        fig.update_yaxes(title_text="TMF", tickformat=",.0f", secondary_y=False)
        fig.update_yaxes(title_text="MTX", tickformat=",.0f", secondary_y=True)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("等待數據...")

    # ── 總覽 PnL ──
    st.header("今日累計 PnL")
    pc1, pc2, pc3 = st.columns(3)
    ft = load_futures_trades()
    fpnl = calc_futures_pnl(ft)
    ol = load_options_ledger()
    opnl = calc_options_pnl(ol)
    sl = load_stock_trades()
    spnl = calc_stock_pnl(sl)
    with pc1:
        if fpnl is not None and not fpnl.empty:
            val = fpnl["pnl"].iloc[-1]
            st.metric("期貨 PnL", f"{val:+,.0f} TWD")
        else:
            st.metric("期貨 PnL", "0 TWD")
    with pc2:
        if opnl is not None and not opnl.empty:
            val = opnl["pnl"].iloc[-1]
            st.metric("選擇權 PnL", f"{val:+,.0f} TWD")
        else:
            st.metric("選擇權 PnL", "0 TWD")
    with pc3:
        if spnl is not None and not spnl.empty:
            val = spnl["pnl"].iloc[-1]
            st.metric("台股 PnL", f"{val:+,.0f} TWD")
        else:
            st.metric("台股 PnL", "0 TWD")

    # ── 總覽：台股快訊 ──
    st.header("台股快訊 (Watchlist Quick View)")
    watchlist = stock_cfg.get("stocks", {}).get("watchlist", [])
    if watchlist:
        ov_data = []
        for ticker in watchlist:
            s_df = load_stock_indicators(ticker)
            if s_df is not None and not s_df.empty:
                last = s_df.iloc[-1]
                ov_data.append({
                    "代號": ticker,
                    "名稱": last.get("name", "Unknown"),
                    "股價": last.get('close', last.get('Close', 0)),
                    "成交量": f"{int(last.get('volume', last.get('Volume', 0))):,}",
                    "Score": round(last.get('score', 0), 1),
                    "Squeeze": "🔒 壓縮" if last.get("sqz_on", False) else "🔓 釋放",
                    "投信": "🔥 連買" if last.get("it_buy_rolling_3_min", 0) > 0 else "⚪ —",
                    "200MA": "🟢 向上" if last.get("ema_200_up", False) else "⚪ 走平/向下"
                })
        
        if ov_data:
            ov_df = pd.DataFrame(ov_data)
            def style_overview(row):
                styles = [''] * len(row)
                if "🔒" in str(row["Squeeze"]):
                    styles[5] = 'background-color: #fee2e2; color: #b91c1c; font-weight: bold'
                if "🔥" in str(row["投信"]):
                    styles[6] = 'background-color: #dcfce7; color: #065f46; font-weight: bold'
                if "🟢" in str(row["200MA"]):
                    styles[7] = 'color: #059669; font-weight: bold'
                return styles
            st.dataframe(ov_df.style.apply(style_overview, axis=1), use_container_width=True, hide_index=True)
        else:
            st.info("等待個股指標數據...")
    else:
        st.info("尚未設定監控名單")

# ════════════════════════════════════════
# Tab 2: 期貨
# ════════════════════════════════════════
with tab_futures:
    st.header(f"期貨 TMF ({mode_badge(f_live)})")

    f_df = load_futures_indicators()
    if f_df is not None and not f_df.empty:
        last = f_df.iloc[-1]
        fc1, fc2, fc3, fc4 = st.columns(4)
        # V-Model fix: Handle duplicate columns by taking first match
        cl_val = last.get('close') if 'close' in last else last.get('Close', 0)
        if hasattr(cl_val, 'iloc'):
            cl_val = float(cl_val.iloc[0]) if len(cl_val) > 0 else 0.0
        else:
            cl_val = float(cl_val or 0)
        sc_val = last.get('score', 0)
        if hasattr(sc_val, 'iloc'):
            sc_val = float(sc_val.iloc[0]) if len(sc_val) > 0 else 0.0
        else:
            sc_val = float(sc_val or 0)
        fc1.metric("Close", f"{cl_val:.0f}")
        fc2.metric("Score", f"{sc_val:.1f}")
        bull = last.get("bull_align", last.get("bullish_align", False))
        bear = last.get("bear_align", last.get("bearish_align", False))
        trend = "🟢 多頭排列" if bull else ("🔴 空頭排列" if bear else "⚪ 中性")
        fc3.metric("趨勢", trend)
        fc4.metric("Squeeze", "🔒 壓縮中" if last.get("sqz_on", False) else "🔓 已釋放")

        if "fired" in last and last.get("fired", False):
            st.success("🔥 **FIRE — 壓縮釋放！**")
        
        ft = load_futures_trades()
        st.plotly_chart(make_price_score_chart(f_df, "close", "TMF 價格 & Score", signals=ft), use_container_width=True)
        st.dataframe(f_df.tail(20), use_container_width=True)
    else:
        st.info("無數據")
    ft = load_futures_trades()
    if ft is not None and not ft.empty:
        st.header("交易記錄")
        st.dataframe(ft, use_container_width=True)
        fpnl = calc_futures_pnl(ft)
        fig = make_pnl_chart(fpnl, "期貨累計 PnL (TWD)")
        if fig:
            st.plotly_chart(fig, use_container_width=True)

# ════════════════════════════════════════
# Tab 3: 選擇權
# ════════════════════════════════════════
with tab_options:
    st.header(f"選擇權 TXO ({mode_badge(o_live)})")
    o_df = load_options_indicators()
    if o_df is not None and not o_df.empty and "price_mtx" in o_df.columns:
        last = o_df.iloc[-1]
        oc1, oc2, oc3, oc4 = st.columns(4)
        # V-Model fix: Handle duplicate columns by taking first match
        mtx_val = last.get('price_mtx', 0)
        if hasattr(mtx_val, 'iloc'):
            mtx_val = float(mtx_val.iloc[0]) if len(mtx_val) > 0 else 0.0
        else:
            mtx_val = float(mtx_val or 0)
        sc_val = last.get('score', 0)
        if hasattr(sc_val, 'iloc'):
            sc_val = float(sc_val.iloc[0]) if len(sc_val) > 0 else 0.0
        else:
            sc_val = float(sc_val or 0)
        oc1.metric("MTX", f"{mtx_val:.0f}")
        oc2.metric("Score", f"{sc_val:.1f}")
        trend = last.get("mid_trend", "")
        trend_label = "🟢 BULL" if trend == "BULL" else ("🔴 BEAR" if trend == "BEAR" else "⚪ —")
        oc3.metric("趨勢", trend_label)
        iv = last.get("iv", 0)
        oc4.metric("IV", f"{iv*100:.1f}%" if iv and iv < 1 else f"{iv:.1f}%")

        if "fired" in last and last.get("fired", False):
            st.success("🔥 **FIRE — 壓縮釋放！**")

        # 顯示當前選擇權持倉
        ol = load_options_ledger()
        if ol is not None and not ol.empty:
            # 找最後一筆非 EXIT 的交易
            active = ol[~ol["Action"].str.contains("EXIT|TP1", na=False)]
            if not active.empty:
                last_pos = active.iloc[-1]
                side = str(last_pos.get("Side", ""))
                action = str(last_pos.get("Action", ""))
                if "iron_condor" in side.lower():
                    pos_label = "🦅 Iron Condor"
                    # 從 Note 取腿資訊
                    note = str(last_pos.get("Note", ""))
                    if "[" in note:
                        pos_label += " " + note.split("[")[1].split("]")[0]
                elif side.upper() == "C":
                    pos_label = "📞 Call"
                elif side.upper() == "P":
                    pos_label = "📉 Put"
                else:
                    pos_label = side
                st.caption(f"當前持倉: **{pos_label}** | 進場: {action} @ {last_pos.get('Price', '')}")
            else:
                st.caption("目前無持倉")
        else:
            st.caption("目前無持倉")
        
        ol = load_options_ledger()
        # Pre-process ledger to match signal expected format if not empty
        sig_df = None
        if ol is not None and not ol.empty:
            sig_df = ol.rename(columns={"Timestamp": "timestamp", "Action": "action", "Price": "price"})
            
        st.plotly_chart(make_price_score_chart(o_df, "price_mtx", "MTX 價格 & Score", signals=sig_df), use_container_width=True)
        st.dataframe(o_df.tail(20), use_container_width=True)
    else:
        st.info("無數據")
    ol = load_options_ledger()
    if ol is not None and not ol.empty:
        st.header("交易記錄")
        st.dataframe(ol.tail(30), use_container_width=True)
        opnl = calc_options_pnl(ol)
        fig = make_pnl_chart(opnl, "選擇權累計 PnL (TWD)")
        if fig:
            st.plotly_chart(fig, use_container_width=True)

# ════════════════════════════════════════
# Tab 4: 台股 Stocks
# ════════════════════════════════════════
with tab_stocks:
    st.header(f"🍎 台股 Stocks ({mode_badge(s_live)})")
    
    st.subheader("Watchlist 實時監控牆")
    watchlist = stock_cfg.get("stocks", {}).get("watchlist", [])
    
    if not watchlist:
        st.info("Watchlist 為空")
    else:
        monitor_data = []
        for ticker in watchlist:
            s_df = load_stock_indicators(ticker)
            if s_df is not None and not s_df.empty:
                last = s_df.iloc[-1]
                monitor_data.append({
                    "代號": ticker,
                    "名稱": last.get("name", "Unknown"),
                    "股價": last.get('close', last.get('Close', 0)),
                    "成交量": f"{int(last.get('volume', last.get('Volume', 0))):,}",
                    "Score": round(last.get('score', 0), 1),
                    "Squeeze": "🔒 壓縮" if last.get("sqz_on", False) else "🔓 釋放",
                    "投信動能": "🔥 連買" if last.get("it_buy_rolling_3_min", 0) > 0 else "⚪ —",
                    "200MA 趨勢": "🟢 向上" if last.get("ema_200_up", False) else "⚪ 走平/向下"
                })
        
        if monitor_data:
            m_df = pd.DataFrame(monitor_data)
            
            def style_monitor(row):
                styles = [''] * len(row)
                # Squeeze 顏色 (紅底白字代表壓縮)
                if "🔒" in str(row["Squeeze"]):
                    styles[5] = 'background-color: #fee2e2; color: #b91c1c; font-weight: bold'
                # 投信顏色
                if "🔥" in str(row["投信動能"]):
                    styles[6] = 'background-color: #dcfce7; color: #065f46; font-weight: bold'
                # 200MA 顏色 (綠色代表多頭向上)
                if "🟢" in str(row["200MA 趨勢"]):
                    styles[7] = 'color: #059669; font-weight: bold'
                return styles

            st.dataframe(m_df.style.apply(style_monitor, axis=1), use_container_width=True, hide_index=True)
        else:
            st.info("等待 Monitor 寫入指標數據...")
    
    st.divider()
    # 讀取目前運行的模式
    current_mode = "LIVE" if s_live else "PAPER"
    sl = load_stock_trades(current_mode)
    
    st.header(f"交易記錄 ({current_mode} 模式)")
    if sl is not None and not sl.empty:
        # 摘要指標
        sells = sl[sl["action"] == "SELL"].copy()
        if not sells.empty:
            sells["pnl_cash"] = pd.to_numeric(sells["pnl_cash"], errors="coerce").fillna(0)
            m1, m2, m3, m4 = st.columns(4)
            total_pnl = sells["pnl_cash"].sum()
            total_fees = pd.to_numeric(sells.get("fees", 0), errors="coerce").fillna(0).sum()
            wins = (sells["pnl_cash"] > 0).sum()
            total = len(sells)
            m1.metric("淨損益", f"{total_pnl:+,.0f} TWD")
            m2.metric("勝率", f"{wins}/{total} ({wins/total*100:.0f}%)" if total > 0 else "—")
            m3.metric("摩擦成本", f"{total_fees:,.0f} TWD")
            m4.metric("平均每筆", f"{total_pnl/total:+,.0f} TWD" if total > 0 else "—")

        # 交易明細表
        display_cols = [c for c in ["timestamp", "ticker", "action", "entry_price", "price", "qty", "reason", "strategy", "pnl_gross", "fees", "pnl_cash"] if c in sl.columns]
        col_rename = {"timestamp": "時間", "ticker": "代號", "action": "動作", "entry_price": "進場價",
                      "price": "出場價", "qty": "股數", "reason": "原因", "strategy": "策略",
                      "pnl_gross": "毛利", "fees": "手續費+稅", "pnl_cash": "淨損益"}
        display = sl[display_cols].sort_values("timestamp", ascending=False).head(30)
        display = display.rename(columns={c: col_rename.get(c, c) for c in display.columns})
        st.dataframe(display, use_container_width=True, hide_index=True)

        spnl = calc_stock_pnl(sl)
        fig = make_pnl_chart(spnl, f"台股累計 PnL ({current_mode})")
        if fig:
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info(f"今日尚無 {current_mode} 交易紀錄")

# ════════════════════════════════════════
# Tab 5: 設定
# ════════════════════════════════════════
with tab_settings:
    st.header("⚙️ 系統設定")

    # ── 0. 實盤就緒度檢查 ──
    with st.expander("🚀 實盤就緒度檢查", expanded=True):
        from core.live_readiness import check_all, get_readiness_summary
        results = check_all()
        status, passed, total = get_readiness_summary(results)

        st.markdown(f"### {status} ({passed}/{total} 項通過)")

        # Progress bar
        pct = passed / total if total > 0 else 0
        st.progress(pct)

        # Detail table
        for r in results:
            icon = "✅" if r.passed else "❌"
            st.caption(f"{icon} **{r.name}**: {r.value} — {r.detail}")

        # Action recommendation
        if passed == total:
            st.success("🎉 所有檢查通過！可以考慮進入 Phase 2 小額實盤測試")
            st.info("建議: 先用 1 口 TMF 測試 5 個交易日，設定每日最大虧損 2%")
        elif passed >= total * 0.6:
            remaining = total - passed
            st.warning(f"⚠️ 還有 {remaining} 項未通過，建議繼續 Paper 觀察")
            for r in results:
                if not r.passed:
                    st.caption(f"❌ 待解決: {r.name} (目前: {r.value})")
        else:
            st.error("❌ 多數檢查未通過，不建議開啟實盤交易")

        st.divider()
        st.caption("參考文件: `docs/LIVE_TRADING_GUIDE.md`")

    # ── 1. 期貨 TMF 設定 ──
    with st.expander("📈 期貨 TMF 設定", expanded=True):
        from strategies.futures.elite_strategies import ELITE_STRATEGIES as FUT_STRATS
        current_fut_strat = futures_cfg.get("active_strategy", "counter_vwap")

        with st.form("futures_settings_form"):
            f_live_new = st.checkbox("啟用期貨實盤交易 (LIVE)", value=futures_cfg.get("live_trading", False))

            # 策略選擇
            strat_options = list(FUT_STRATS.keys())
            try:
                strat_idx = strat_options.index(current_fut_strat)
            except ValueError:
                strat_idx = 0

            f_strat_new = st.selectbox("核心進場策略", strat_options, index=strat_idx,
                                       help="系統將使用此策略進行即時信號判斷。")

            # 顯示當前策略說明
            st.info(f"💡 **策略說明**: {FUT_STRATS.get(f_strat_new, {}).get('desc', '無說明')}")

            st.divider()

            # ── 口數與持倉限制 ──
            st.markdown("##### 📦 口數與持倉限制")
            c1, c2 = st.columns(2)
            f_lots = c1.number_input("每筆交易口數", min_value=1, max_value=10,
                                     value=futures_cfg.get("trade_mgmt", {}).get("lots_per_trade", 2),
                                     help="每次進場的口數。實盤建議從 1 口開始。")
            f_max_pos = c2.number_input("最大持倉口數", min_value=1, max_value=10,
                                        value=futures_cfg.get("trade_mgmt", {}).get("max_positions", 2),
                                        help="同時最大持倉口數。建議 1-2 口控制風險。")

            st.divider()
            f_regime = st.selectbox("市場濾網 (Regime)", ["low", "mid", "high"], index=["low", "mid", "high"].index(futures_cfg.get("strategy", {}).get("regime_filter", "mid")))
            
            fc1, fc2 = st.columns(2)
            f_score = fc1.slider("進場門檻 (Score)", 10, 100, value=futures_cfg.get("strategy", {}).get("entry_score", 20))
            f_atr = fc2.slider("ATR 止損倍數", 1.0, 5.0, value=float(futures_cfg.get("risk_mgmt", {}).get("atr_multiplier", 2.0)), step=0.1)

            if st.form_submit_button("💾 儲存並重啟期貨模組"):
                futures_cfg["live_trading"] = f_live_new
                futures_cfg["active_strategy"] = f_strat_new
                futures_cfg["strategy"]["regime_filter"] = f_regime
                futures_cfg["strategy"]["entry_score"] = f_score
                futures_cfg["risk_mgmt"]["atr_multiplier"] = f_atr
                futures_cfg["trade_mgmt"]["lots_per_trade"] = f_lots
                futures_cfg["trade_mgmt"]["max_positions"] = f_max_pos
                save_yaml(FUTURES_CFG_PATH, futures_cfg)
                trigger_restart()
                st.success(f"期貨設定已更新！策略: {f_strat_new} | 口數: {f_lots} | 最大持倉: {f_max_pos}")
                st.rerun()

    # ── 2. 選擇權 TXO 設定 ──
    with st.expander("🔮 選擇權 TXO 設定", expanded=False):
        # 假設選擇權也有類似的策略結構，目前從 config 讀取
        current_opt_mode = options_cfg.get("mode", "V2")
        
        with st.form("options_settings_form"):
            o_live_new = st.checkbox("啟用選擇權實盤交易 (LIVE)", value=options_cfg.get("live_trading", False))
            
            o_mode_new = st.selectbox("交易模式", ["V1", "V2", "V3"], index=["V1", "V2", "V3"].index(current_opt_mode),
                                      help="V1: 當沖, V2: 波段(月選), V3: 夜盤當沖")
            
            # 模式說明
            mode_desc = {
                "V1": "近月合約當日沖銷，適合高波動行情。",
                "V2": "月選合約波段持有 (≥14天 DTE)，降低時間價值衰減。",
                "V3": "專攻夜盤波動，開盤進場收盤前出場。"
            }
            st.info(f"💡 **模式說明**: {mode_desc.get(o_mode_new)}")

            o_score = st.slider("進場門檻 (Score)", 10, 100, value=options_cfg.get("entry_score", 80))

            o_fire_thresh = st.slider("Fire 門檻 (強趨勢 score)", 10, 100,
                                       value=int(options_cfg.get("strategy", {}).get("fire_score_threshold", 80)),
                                       help="fired=False 但 score 超過此值也允許進場。降低此值可在趨勢行情中進場。")

            # ── 口數與持倉限制 ──
            st.markdown("##### 📦 口數與持倉限制")
            oc1, oc2 = st.columns(2)
            o_lots = oc1.number_input("每筆交易口數", min_value=1, max_value=10,
                                     value=options_cfg.get("risk_mgmt", {}).get("lots_per_trade", 2),
                                     help="每次進場的口數。實盤建議從 1 口開始。")
            o_max_pos = oc2.number_input("最大持倉口數", min_value=1, max_value=10,
                                        value=options_cfg.get("risk_mgmt", {}).get("max_positions", 2),
                                        help="同時最大持倉口數。建議 1-2 口控制風險。")

            st.divider()
            oc3, oc4 = st.columns(2)
            o_min_iv = oc3.slider("最低 IV 限制", 0.1, 0.5, value=float(options_cfg.get("min_iv", 0.15)), step=0.01)
            o_max_iv = oc4.slider("最高 IV 限制", 0.3, 1.0, value=float(options_cfg.get("max_iv", 0.60)), step=0.01)

            if st.form_submit_button("💾 儲存並重啟選擇權模組"):
                options_cfg["live_trading"] = o_live_new
                options_cfg["mode"] = o_mode_new
                options_cfg["entry_score"] = o_score
                options_cfg["strategy"]["fire_score_threshold"] = o_fire_thresh
                options_cfg["min_iv"] = o_min_iv
                options_cfg["max_iv"] = o_max_iv
                options_cfg["risk_mgmt"]["lots_per_trade"] = o_lots
                options_cfg["risk_mgmt"]["max_positions"] = o_max_pos
                save_yaml(OPTIONS_CFG_PATH, options_cfg)
                trigger_restart()
                st.success(f"選擇權設定已更新！模式: {o_mode_new} | 口數: {o_lots} | 最大持倉: {o_max_pos} | Fire閾值: {o_fire_thresh}")
                st.rerun()

    # ── 3. 台股 Stocks 設定 ──
    with st.expander("🍎 台股 Stocks 設定", expanded=True):
        stk_inner = stock_cfg.get("stocks", {})
        
        # 將同步按鈕與顯示邏輯整合
        if st.button("🔄 同步 Squeeze Screener 推薦名單"):
            try:
                import subprocess
                subprocess.run(["python3", "scripts/sync_watchlist.py"], check=True)
                st.success("同步成功！")
                st.rerun()
            except Exception as e:
                st.error(f"同步失敗: {e}")

        with st.form("stock_settings_form"):
            s_live_new = st.checkbox("啟用台股實盤交易 (LIVE)", value=stock_cfg.get("live_trading", False))
            
            # --- 改為直式多行輸入 ---
            current_watchlist = stk_inner.get("watchlist", ["2330", "2454"])
            watchlist_area = st.text_area("監控名單 (每行一個代號)", 
                                         value="\n".join(current_watchlist),
                                         height=200,
                                         help="請輸入股票代號，例如 2330。支援一行一個標的。")
            
            c1, c2, c2b = st.columns(3)
            from strategies.stocks.entry_strategies import STOCK_STRATEGIES
            strat_options = list(STOCK_STRATEGIES.keys())
            current_strat = stk_inner.get("strategy", "momentum_breakout")
            strat_idx = strat_options.index(current_strat) if current_strat in strat_options else 0
            strat_new = c1.selectbox("策略", strat_options, index=strat_idx,
                                     help=STOCK_STRATEGIES.get(current_strat, {}).get("desc", ""))
            budget_new = c2.number_input("總分配資金 (TWD)", value=stk_inner.get("total_portfolio_budget", 100000), step=10000)
            capital_new = c2b.number_input("單筆預算 (TWD)", value=stk_inner.get("capital_per_trade", 20000), step=1000)
            
            c3, c4, c5 = st.columns(3)
            sl_new = c3.slider("停損 (%)", 1.0, 10.0, value=float(stk_inner.get("stop_loss_pct", 0.02)*100), step=0.5) / 100.0
            tp_new = c4.slider("停利 (%)", 2.0, 20.0, value=float(stk_inner.get("take_profit_pct", 0.10)*100), step=0.5) / 100.0
            ts_new = c5.slider("移動停損 (%)", 0.5, 5.0, value=float(stk_inner.get("trailing_stop_pct", 0.01)*100), step=0.1) / 100.0
            
            if st.form_submit_button("💾 儲存並重啟台股模組"):
                # 處理多行輸入，轉回 list
                new_tickers = [t.strip() for t in watchlist_area.split("\n") if t.strip()]
                new_stocks = {
                    "watchlist": new_tickers,
                    "strategy": strat_new,
                    "total_portfolio_budget": budget_new,
                    "capital_per_trade": capital_new,
                    "entry_score": stk_inner.get("entry_score", 20),
                    "atr_mult": stk_inner.get("atr_mult", 2.0),
                    "stop_loss_pct": sl_new,
                    "take_profit_pct": tp_new,
                    "trailing_stop_pct": ts_new,
                }
                # 保留 bear_defense 等既有區塊
                if "bear_defense" in stk_inner:
                    new_stocks["bear_defense"] = stk_inner["bear_defense"]
                if "fallback_strategy" in stk_inner:
                    new_stocks["fallback_strategy"] = stk_inner["fallback_strategy"]
                new_stock_cfg = {
                    "live_trading": s_live_new,
                    "stocks": new_stocks,
                }
                save_yaml(STOCK_CFG_PATH, new_stock_cfg)
                trigger_restart()
                st.success("台股設定已更新，正在重啟系統...")

    st.divider()
    st.info("期貨與選擇權設定請編輯對應的 YAML 檔案")

# ── Footer and Refresh ──
refresh = 30
time.sleep(refresh)
st.rerun()
