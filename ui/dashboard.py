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
import json
import yaml
import datetime
import os
from core.date_utils import get_session_date_str, get_trade_day
from core.dashboard_data import (
    build_stock_orders_from_trades,
    merge_indicator_frames,
    extend_taifex_recess_continuity,
    resolve_preferred_or_latest_file,
    resolve_stock_orders_file,
)
from core.order_lifecycle_audit import rebuild_options_orders_from_ledger
from core.dashboard_positions import (
    count_futures_entries,
    count_options_entries,
    describe_options_order_truth,
    estimate_options_order_unrealized,
    estimate_theta_unrealized,
    find_latest_open_futures_position,
    find_latest_open_options_position,
    latest_indicator_close,
    option_order_matches_open_position,
    summarize_combo_legs,
)
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


# ── [Audit Debug] Timestamp integrity logger ──
def _debug_ts_integrity(df, label, timestamp_col="timestamp"):
    """Print timestamp duplication stats for debugging dashboard time axis issues."""
    if df is None or df.empty:
        print(f"[TS_DEBUG][{label}] empty")
        return
    if timestamp_col not in df.columns:
        print(f"[TS_DEBUG][{label}] no '{timestamp_col}' column, cols={list(df.columns)[:10]}")
        return
    ts = pd.to_datetime(df[timestamp_col], errors="coerce")
    dup_count = ts.duplicated().sum()
    rows_before = len(df)
    rows_after_dedup = ts.nunique()
    print(
        f"[TS_DEBUG][{label}] rows={rows_before} "
        f"unique_ts={rows_after_dedup} "
        f"dup_ts={dup_count} "
        f"min_ts={ts.min()} "
        f"max_ts={ts.max()}"
    )
    if dup_count > 0:
        dups = df.loc[ts.duplicated(keep=False), [timestamp_col]].head(20)
        print(f"[TS_DEBUG][{label}] DUP TIMESTAMPS (first 20):")
        for _, row in dups.iterrows():
            print(f"    {row[timestamp_col]}")
    # final safety net: sort + dedup row-by-timestamp
    df[timestamp_col] = pd.to_datetime(df[timestamp_col], errors="coerce")
    df.sort_values(timestamp_col, inplace=True)
    df.drop_duplicates(subset=[timestamp_col], keep="last", inplace=True)
    print(f"[TS_DEBUG][{label}] after safety dedup: {len(df)} rows")

# Helper: robustly coerce possibly-array-like values (Series, ndarray, list) to a float scalar
import numpy as _np
import pandas as _pd

OPTIONS_TRUTH_SOURCES = ("broker_combo", "paper_strategy", "ledger_rebuilt")

def _to_num(val, default=0.0):
    try:
        if val is None:
            return float(default)
        # pandas Series (row) or Series-like
        if isinstance(val, _pd.Series):
            if val.empty:
                return float(default)
            return float(val.iloc[-1])
        # numpy arrays or lists/tuples
        if isinstance(val, (list, tuple, _np.ndarray)):
            arr = _np.asarray(val)
            if arr.size == 0:
                return float(default)
            return float(arr.flatten()[-1])
        # fallback
        return float(val)
    except Exception:
        return float(default)

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
    
    # GSD: Large, focused password field for better UX
    st.markdown("""
        <style>
        div[data-baseweb="input"] { height: 60px !important; }
        input[type="password"] { font-size: 24px !important; }
        </style>
        <script>
        // High-reliability Autofocus: Poll until input renders
        var focusAttempts = 0;
        var focusInterval = setInterval(function() {
            var inputs = window.parent.document.querySelectorAll('input[type="password"]');
            if (inputs.length > 0) {
                inputs[0].focus();
                if (focusAttempts++ > 10) clearInterval(focusInterval);
            }
        }, 100);
        </script>
    """, unsafe_allow_html=True)
    
    pwd = st.text_input("🔒 請輸入密碼", type="password", placeholder="點擊此處或直接輸入...", key="password_input")
    
    # JavaScript to auto-focus the password field
    st.markdown("""
    <script>
    // Wait for the page to load, then focus the password input
    document.addEventListener('DOMContentLoaded', function() {
        // Find the password input by its data-testid attribute
        const passwordInput = document.querySelector('input[type="password"]');
        if (passwordInput) {
            passwordInput.focus();
            // Also select all text if there's any
            passwordInput.select();
        }
    });
    
    // Also try after a short delay in case the page loads dynamically
    setTimeout(function() {
        const passwordInput = document.querySelector('input[type="password"]');
        if (passwordInput) {
            passwordInput.focus();
            passwordInput.select();
        }
    }, 100);
    </script>
    """, unsafe_allow_html=True)
    if pwd == os.environ.get("DASHBOARD_PASSWORD", "5888"):
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
# GSD: Align DATE_STR with the ACTIVE trading session date (e.g. after 15:00 today, it's tomorrow's date)
DATE_STR = get_session_date_str(datetime.datetime.now())
# 新增：交易記錄日期，使用 get_trade_day 確保與交易記錄文件一致
TRADE_DATE_STR = get_trade_day(datetime.datetime.now()).strftime("%Y%m%d")
TODAY = datetime.datetime.now().strftime("%Y-%m-%d")

# ── Session detection (used by sidebar + config loading) ──
from core.date_utils import is_night_session
_CURRENT_SESSION_NIGHT = is_night_session(datetime.datetime.now())
FUTURES_CFG_NAME = "futures_night.yaml" if _CURRENT_SESSION_NIGHT else "futures.yaml"

# ── Sidebar Info ──
with st.sidebar:
    st.title("Trading Unified")
    st.markdown(f"🗓️ **交易日 (Trading Day)**")
    # GSD: Always use the latest date string from session helper
    st.code(f"{DATE_STR[:4]}-{DATE_STR[4:6]}-{DATE_STR[6:]}")
    
    # 💡 GSD: Continuous Chart Mode toggle
    cont_mode = st.toggle("🕒 連續圖表模式", value=True, help="顯示最近 24 小時資料，而非僅今日交易日。")
    
    st.markdown(f"🕒 **最後更新**: {datetime.datetime.now().strftime('%H:%M:%S')}")
    # Session indicator
    _session_label = "🌙 夜盤" if _CURRENT_SESSION_NIGHT else "☀️ 日盤"
    _session_color = "#7c3aed" if _CURRENT_SESSION_NIGHT else "#f59e0b"
    st.markdown(f"<span style='color:{_session_color};font-weight:bold'>{_session_label}</span> — 設定檔: `{FUTURES_CFG_NAME}`", unsafe_allow_html=True)
    # ── [GSD 4.13] System Readiness Indicators (Pillar 4) ──
    st.markdown("🚦 **系統狀態 (Readiness)**")
    
    try:
        from core.shioaji_session import get_shared_system_status, SystemReadiness
        status = get_shared_system_status()
    except Exception:
        # Fallback if core is not yet loaded in sys.path
        status = None

    # Map status to UI labels/colors
    if status is None:
        st.info("🕒 BOOTING / INITIALIZING")
    elif status.name == "BOOTING":
        st.info("🕒 BOOTING")
    elif status.name == "MONITORING":
        st.success("✅ MONITORING")
        st.warning("⚠️ TRADING: WARMING UP")
    elif status.name == "TRADING":
        st.success("✅ MONITORING")
        st.success("✅ TRADING READY")
    elif status.name == "DEGRADED":
        st.success("✅ MONITORING")
        st.warning("⚠️ TRADING: PAUSED (STALE FEED)")
        st.error("🚨 DEGRADED")
    
    st.divider()

    if st.button("🔄 強制刷新頁面"):
        st.rerun()

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
FUTURES_CFG_PATH = BASE / "config" / FUTURES_CFG_NAME
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

# ── Filter by latest Trading Day ──
def filter_today(df, ts_col="timestamp"):
    if df is None or df.empty:
        return df
    try:
        from core.date_utils import get_trading_day
        
        # V-Model fix 1: Deduplicate columns first
        if df.columns.duplicated().any():
            df = df.loc[:, ~df.columns.duplicated()].copy()

        if ts_col not in df.columns:
            return df

        # Standardize timestamp
        df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce").dt.tz_localize(None)
        df = df.dropna(subset=[ts_col])

        if df.empty:
            return df

        # GSD Rationale: Group by Trading Day and pick the latest one.
        # Using .apply ensures each timestamp is handled correctly as a scalar.
        df["_tday"] = df[ts_col].apply(lambda x: get_trading_day(x))
        latest_tday = df["_tday"].max()
        filtered_df = df[df["_tday"] == latest_tday].copy()
        filtered_df = filtered_df.drop(columns=["_tday"])

        # 確保排序
        filtered_df = filtered_df.sort_values(ts_col)

        # [Audit Debug] Log timestamp integrity after trading day filter
        _debug_ts_integrity(filtered_df, "filter_today_output", ts_col)

        # V-Model fix 3: 過濾 fallback 假資料
        for col in ["close", "price_mtx"]:
            if col in filtered_df.columns and len(filtered_df) > 0:
                # 排除 0 或負數 (GSD: Data Integrity)
                filtered_df = filtered_df[filtered_df[col] > 0].copy()
                
                if not filtered_df.empty:
                    latest_val = filtered_df[col].iat[-1]
                    try:
                        latest_val_float = float(latest_val)
                        if latest_val_float > 0:
                            # 異常過濾：若價格低於最新價的 30%（寬鬆一點），判定為系統初始化假數據
                            mask = filtered_df[col].gt(latest_val_float * 0.3)
                            filtered_df = filtered_df[mask].copy()
                    except (TypeError, ValueError):
                        continue
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
    
    # GSD: Enhanced visuals for continuous trading day
    # 1. Add vertical line at 15:00 (Start of Trading Day)
    from core.date_utils import is_night_session
    ts_series = pd.to_datetime(df[ts_col], errors="coerce")
    for ts in ts_series:
        if pd.notna(ts) and hasattr(ts, "hour") and ts.hour == 15 and ts.minute == 0:
            fig.add_vline(x=ts, line_width=1, line_dash="dash", line_color="gray", row="all", col=1)

    # 2. Shaded background for night session
    night_mask = is_night_session(df[ts_col])
    if night_mask.any():
        # Find continuous night blocks
        night_starts = df.loc[night_mask & ~night_mask.shift(1).fillna(False), ts_col]
        night_ends = df.loc[night_mask & ~night_mask.shift(-1).fillna(False), ts_col]
        for start, end in zip(night_starts, night_ends):
            fig.add_vrect(
                x0=start, x1=end,
                fillcolor="gray", opacity=0.1,
                layer="below", line_width=0,
                row="all", col=1
            )

    fig.update_layout(height=400, margin=dict(t=10, b=10, l=40, r=20), showlegend=False)
    
    # ── GSD Enhancement: Remove non-trading gaps from time axis ──
    # This prevents the chart from showing long flat lines during gaps
    fig.update_xaxes(
        rangebreaks=[
            dict(bounds=["sat", "mon"]), # Remove weekends
            dict(bounds=[5, 8.75], pattern="hour"),  # 05:00 - 08:45
            dict(bounds=[13.75, 15], pattern="hour"), # 13:45 - 15:00
        ],
        row=1, col=1
    )
    fig.update_xaxes(
        rangebreaks=[
            dict(bounds=["sat", "mon"]),
            dict(bounds=[5, 8.75], pattern="hour"),
            dict(bounds=[13.75, 15], pattern="hour"),
        ],
        row=2, col=1
    )
    
    # 💡 GSD: Explicitly set Y-axis range to follow data closely
    if len(df) > 0:
        p_vals = df[p_col].dropna().to_numpy()
        if len(p_vals) > 0:
            p_min, p_max = p_vals.min(), p_vals.max()
            if p_max > p_min:
                # Add 5% padding instead of 10% for tighter zoom
                padding = (p_max - p_min) * 0.05
                fig.update_yaxes(range=[p_min - padding, p_max + padding], row=1, col=1, autorange=False)
            else:
                # Flat line: use normal range mode (don't force zero)
                fig.update_yaxes(rangemode="normal", row=1, col=1)
    
    # Apply format to all Y axes, but don't force autorange here
    fig.update_yaxes(tickformat=",.0f", fixedrange=False)
    
    # 3. Remove non-trading gaps and improve labels
    fig.update_xaxes(
        tickformat="%m/%d\n%H:%M",
        hoverformat="%Y/%m/%d %H:%M",
        rangebreaks=[
            dict(bounds=[13.75, 15], pattern="hour"),  # 13:45 - 15:00
            dict(bounds=[5, 8.75], pattern="hour"),    # 05:00 - 08:45
            dict(bounds=["sat", "mon"]),               # Weekend
        ]
    )
    return fig

# ── Futures Dual Contract Chart ──
def make_futures_dual_chart(near_df, far_df=None, title="期貨價格走勢", signals=None):
    """繪製期貨雙合約價格圖表
    
    顯示近月合約價格，如果提供遠月資料則同時顯示遠月價格
    
    Args:
        near_df: 近月合約資料 (必須包含 'timestamp' 和 'close' 欄位)
        far_df: 遠月合約資料 (可選，必須包含 'timestamp' 和 'close' 欄位)
        title: 圖表標題
        signals: 交易訊號資料
        
    Returns:
        Plotly Figure 物件
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    
    # 創建子圖 (價格 + 分數)
    fig = make_subplots(
        rows=2, cols=1, 
        shared_xaxes=True, 
        row_heights=[0.7, 0.3], 
        vertical_spacing=0.05
    )
    
    # 1. 近月價格線
    fig.add_trace(
        go.Scatter(
            x=near_df["timestamp"],
            y=near_df["close"],
            name="近月",
            line=dict(width=2, color="#1f77b4"),
            mode="lines"
        ),
        row=1, col=1
    )
    
    # 2. 如果有遠月資料，添加遠月價格線
    if far_df is not None and not far_df.empty:
        # 確保時間戳對齊 - 用完整的遠月資料繪製
        far_min_ts = near_df["timestamp"].min()
        far_max_ts = near_df["timestamp"].max()
        far_visible = far_df[(far_df["timestamp"] >= far_min_ts) & (far_df["timestamp"] <= far_max_ts)]
        if far_visible.empty:
            # 時間軸不重疊時（如遠月資料還在舊日期），取遠月最後 50 筆
            # 並擴展 xaxis 範圍以包含遠月資料，確保兩條線都可見
            far_visible = far_df.tail(50)
            far_visible_tail = far_df.tail(5)
            # 延伸 xaxis 範圍到遠月最後一筆資料時間
            if len(far_visible_tail) > 0:
                fig.update_xaxes(range=[
                    min(near_df["timestamp"].min(), far_visible_tail["timestamp"].min()),
                    max(near_df["timestamp"].max(), far_visible_tail["timestamp"].max())
                ])
        fig.add_trace(
            go.Scatter(
                x=far_visible["timestamp"],
                y=far_visible["close"],
                name="遠月",
                line=dict(width=1.5, color="#ff7f0e", dash="dash"),
                mode="lines",
                connectgaps=False
            ),
            row=1, col=1
        )
    
    # 3. 添加交易訊號
    if signals is not None and not signals.empty and "action" in signals.columns:
        # 買入訊號
        buys = signals[signals["action"].str.contains("BUY", case=False, na=False)]
        if not buys.empty:
            fig.add_trace(
                go.Scatter(
                    x=buys["timestamp"],
                    y=buys["price"],
                    mode="markers",
                    marker=dict(symbol="triangle-up", size=12, color="#00cc66", line=dict(width=1, color="white")),
                    name="BUY"
                ),
                row=1, col=1
            )
        
        # 賣出訊號
        sells = signals[signals["action"].str.contains("SELL", case=False, na=False)]
        if not sells.empty:
            fig.add_trace(
                go.Scatter(
                    x=sells["timestamp"],
                    y=sells["price"],
                    mode="markers",
                    marker=dict(symbol="triangle-down", size=12, color="#ff4444", line=dict(width=1, color="white")),
                    name="SELL"
                ),
                row=1, col=1
            )
        
        # 出場訊號
        exits = signals[signals["action"].str.contains("EXIT|COVER", case=False, na=False)]
        if not exits.empty:
            fig.add_trace(
                go.Scatter(
                    x=exits["timestamp"],
                    y=exits["price"],
                    mode="markers",
                    marker=dict(symbol="diamond", size=10, color="#ffa500", line=dict(width=1, color="white")),
                    name="EXIT"
                ),
                row=1, col=1
            )
    
    # 4. 分數條形圖 (如果近月資料有 score 欄位)
    if "score" in near_df.columns:
        scores = near_df["score"]
        colors = ["#00cc66" if s >= 0 else "#ff4444" for s in scores]
        fig.add_trace(
            go.Bar(
                x=near_df["timestamp"],
                y=scores,
                name="Score",
                marker_color=colors
            ),
            row=2, col=1
        )
    
    # 5. 添加交易時間標記
    from core.date_utils import is_night_session
    import pandas as pd
    
    # 垂直線標記交易日開始 (15:00)
    ts_series = pd.to_datetime(near_df["timestamp"], errors="coerce")
    for ts in ts_series:
        if pd.notna(ts) and hasattr(ts, "hour") and ts.hour == 15 and ts.minute == 0:
            fig.add_vline(x=ts, line_width=1, line_dash="dash", line_color="gray", row="all", col=1)
    
    # 夜盤時段背景著色
    night_mask = is_night_session(near_df["timestamp"])
    if night_mask.any():
        night_starts = near_df.loc[night_mask & ~night_mask.shift(1).fillna(False), "timestamp"]
        night_ends = near_df.loc[night_mask & ~night_mask.shift(-1).fillna(False), "timestamp"]
        for start, end in zip(night_starts, night_ends):
            fig.add_vrect(
                x0=start, x1=end,
                fillcolor="gray", opacity=0.1,
                layer="below", line_width=0,
                row="all", col=1
            )
    
    # 6. 圖表佈局設定
    fig.update_layout(
        height=400,
        margin=dict(t=30, b=10, l=40, r=20),
        title=dict(text=title, x=0.5, xanchor="center"),
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    
    # 7. 移除非交易時段間隔
    fig.update_xaxes(
        rangebreaks=[
            dict(bounds=["sat", "mon"]),  # 移除週末
            dict(bounds=[5, 8.75], pattern="hour"),   # 05:00 - 08:45
            dict(bounds=[13.75, 15], pattern="hour"), # 13:45 - 15:00
        ],
        row=1, col=1
    )
    fig.update_xaxes(
        rangebreaks=[
            dict(bounds=["sat", "mon"]),
            dict(bounds=[5, 8.75], pattern="hour"),
            dict(bounds=[13.75, 15], pattern="hour"),
        ],
        row=2, col=1
    )
    
    return fig

# ── Calendar Spread Chart ──
def make_calendar_spread_chart(spread_df):
    """繪製日曆價差圖表
    
    包含：
    1. 近月/遠月價格走勢
    2. 價差 (spread) 走勢
    3. Spread Z-score 指標
    4. Calendar Condor 策略進出場條件標記
    """
    if spread_df is None or spread_df.empty:
        return None
    
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
        
        # 創建 3 行子圖
        fig = make_subplots(
            rows=3, cols=1,
            shared_xaxes=True,
            row_heights=[0.4, 0.3, 0.3],
            vertical_spacing=0.05,
            subplot_titles=("近月/遠月價格", "價差 (Spread)", "Spread Z-score")
        )
        
        # 1. 近月/遠月價格走勢
        if "Close_near" in spread_df.columns and "Close_far" in spread_df.columns:
            fig.add_trace(
                go.Scatter(
                    x=spread_df["timestamp"],
                    y=spread_df["Close_near"],
                    name="近月",
                    line=dict(color="#1f77b4", width=2),
                    mode="lines"
                ),
                row=1, col=1
            )
            
            fig.add_trace(
                go.Scatter(
                    x=spread_df["timestamp"],
                    y=spread_df["Close_far"],
                    name="遠月",
                    line=dict(color="#ff7f0e", width=2, dash="dash"),
                    mode="lines"
                ),
                row=1, col=1
            )
        
        # 2. 價差走勢
        if "spread" in spread_df.columns:
            fig.add_trace(
                go.Scatter(
                    x=spread_df["timestamp"],
                    y=spread_df["spread"],
                    name="價差 (近月-遠月)",
                    line=dict(color="#2ca02c", width=2),
                    mode="lines"
                ),
                row=2, col=1
            )
            
            # 添加價差移動平均線
            if "spread_ma" in spread_df.columns:
                fig.add_trace(
                    go.Scatter(
                        x=spread_df["timestamp"],
                        y=spread_df["spread_ma"],
                        name="價差 MA(20)",
                        line=dict(color="#9467bd", width=1, dash="dot"),
                        mode="lines"
                    ),
                    row=2, col=1
                )
            
            # 添加價差標準差帶
            if "spread_ma" in spread_df.columns and "spread_std" in spread_df.columns:
                upper_band = spread_df["spread_ma"] + spread_df["spread_std"]
                lower_band = spread_df["spread_ma"] - spread_df["spread_std"]
                
                # 添加標準差帶（半透明）
                fig.add_trace(
                    go.Scatter(
                        x=spread_df["timestamp"].tolist() + spread_df["timestamp"].tolist()[::-1],
                        y=upper_band.tolist() + lower_band.tolist()[::-1],
                        fill="toself",
                        fillcolor="rgba(128, 128, 128, 0.2)",
                        line=dict(color="rgba(128, 128, 128, 0)"),
                        name="±1 Std Dev",
                        showlegend=True
                    ),
                    row=2, col=1
                )
        
        # 3. Spread Z-score
        if "spread_z" in spread_df.columns:
            fig.add_trace(
                go.Scatter(
                    x=spread_df["timestamp"],
                    y=spread_df["spread_z"],
                    name="Spread Z-score",
                    line=dict(color="#d62728", width=2),
                    mode="lines"
                ),
                row=3, col=1
            )
            
            # 添加 Calendar Condor 策略的進出場水平線
            # 進場條件: spread_z > 3.0 (做空價差) 或 spread_z < -3.0 (做多價差)
            # 出場條件: spread_z < -0.5 (做空價差獲利了結) 或 spread_z > 0.5 (做多價差獲利了結)
            # 停損條件: spread_z > 3.5 (做空價差停損) 或 spread_z < -3.5 (做多價差停損)
            
            # 進場水平線
            fig.add_hline(
                y=3.0, line_dash="dash", line_color="red", 
                annotation_text="做空價差進場", annotation_position="top right",
                row=3, col=1
            )
            fig.add_hline(
                y=-3.0, line_dash="dash", line_color="green",
                annotation_text="做多價差進場", annotation_position="bottom right",
                row=3, col=1
            )
            
            # 出場水平線
            fig.add_hline(
                y=-0.5, line_dash="dot", line_color="orange",
                annotation_text="做空價差出場", annotation_position="bottom right",
                row=3, col=1
            )
            fig.add_hline(
                y=0.5, line_dash="dot", line_color="orange",
                annotation_text="做多價差出場", annotation_position="top right",
                row=3, col=1
            )
            
            # 停損水平線
            fig.add_hline(
                y=3.5, line_dash="dash", line_color="darkred",
                annotation_text="做空價差停損", annotation_position="top right",
                row=3, col=1
            )
            fig.add_hline(
                y=-3.5, line_dash="dash", line_color="darkgreen",
                annotation_text="做多價差停損", annotation_position="bottom right",
                row=3, col=1
            )
            
            # 零線
            fig.add_hline(y=0, line_dash="solid", line_color="gray", line_width=1, row=3, col=1)
        
        # 更新佈局
        fig.update_layout(
            height=700,
            margin=dict(t=40, b=20, l=40, r=20),
            legend=dict(orientation="h", y=1.02, x=0.5, xanchor="center"),
            hovermode="x unified",
            title_text="日曆價差分析 (Calendar Spread)"
        )
        
        # 移除非交易時段
        fig.update_xaxes(
            rangebreaks=[
                dict(bounds=["sat", "mon"]),  # 移除週末
                dict(bounds=[5, 8.75], pattern="hour"),   # 05:00 - 08:45
                dict(bounds=[13.75, 15], pattern="hour"), # 13:45 - 15:00
            ],
            tickformat="%m/%d\n%H:%M",
            hoverformat="%Y/%m/%d %H:%M"
        )
        
        # 設置 Y 軸標籤
        fig.update_yaxes(title_text="價格", row=1, col=1, tickformat=",.0f")
        fig.update_yaxes(title_text="價差點數", row=2, col=1, tickformat=",.1f")
        fig.update_yaxes(title_text="Z-score", row=3, col=1, tickformat=",.2f")
        
        return fig
        
    except Exception as e:
        print(f"[Calendar Spread] 繪製圖表錯誤: {e}")
        import traceback
        traceback.print_exc()
        return None

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

def format_options_trades(ledger_df):
    """
    將 flat ledger 轉換成 round-trip 格式，方便理解每筆交易。
    回傳 DataFrame: 序號 | 進場時間 | 出場時間 | Side | 進場價 | 出場價 | 口數 | 出場原因 | 毛利 | 手續費稅 | 淨利
    """
    if ledger_df is None or ledger_df.empty or "Action" not in ledger_df.columns:
        return ledger_df

    trades = []
    pending_entry = None
    trade_num = 0

    for _, row in ledger_df.iterrows():
        action = str(row.get("Action", ""))
        if "ENTRY" in action and "RETRY" not in action and "SUBMITTED" not in action and "CLEARED" not in action:
            # 記錄進場
            pending_entry = {
                "entry_time": str(row.get("Timestamp", "")),
                "side": str(row.get("Side", "")),
                "entry_price": row.get("Price", 0),
                "quantity": row.get("Quantity", 1),
                "entry_note": str(row.get("Note", "")),
            }
        elif pending_entry and any(kw in action for kw in ["EXIT", "THETA_EXIT", "TP1", "TRAIL", "TIME", "REVERSAL", "TRAP", "FILL"]):
            # 出場 → 結算 round-trip
            trade_num += 1
            entry_price = float(pending_entry["entry_price"] or 0)
            exit_price = float(row.get("Price", 0))
            qty = int(pending_entry["quantity"] or 1)
            point_value = 50

            gross_pnl = (exit_price - entry_price) * point_value * qty
            # 摩擦成本
            broker_fee = 20 * 2 * qty
            exchange_fee = 5 * 2 * qty
            tax = (entry_price + exit_price) * point_value * 0.001 * qty
            total_cost = broker_fee + exchange_fee + tax
            net_pnl = gross_pnl - total_cost

            trades.append({
                "#": trade_num,
                "進場時間": pending_entry["entry_time"],
                "出場時間": str(row.get("Timestamp", "")),
                "方向": pending_entry["side"],
                "進場價": round(entry_price, 1),
                "出場價": round(exit_price, 1),
                "口數": qty,
                "出場原因": action,
                "毛利": round(gross_pnl, 0),
                "摩擦成本": round(total_cost, 0),
                "淨利": round(net_pnl, 0),
            })
            pending_entry = None

    # 如果還有未出場的持倉
    if pending_entry:
        trade_num += 1
        trades.append({
            "#": trade_num,
            "進場時間": pending_entry["entry_time"],
            "出場時間": "⏳ 持倉中",
            "方向": pending_entry["side"],
            "進場價": round(pending_entry["entry_price"], 1),
            "出場價": "-",
            "口數": int(pending_entry["quantity"] or 1),
            "出場原因": "-",
            "毛利": "-",
            "摩擦成本": "-",
            "淨利": "-",
        })

    return _format_coerce_floats(pd.DataFrame(trades)) if trades else ledger_df


def format_futures_trades(ledger_df):
    """Round-trip formatter for futures trades. Matches BUY→EXIT pairs."""
    if ledger_df is None or ledger_df.empty or "type" not in ledger_df.columns and "action" not in ledger_df.columns:
        return ledger_df

    action_col = "action" if "action" in ledger_df.columns else "type"
    trades = []
    pending_entry = None
    trade_num = 0

    for _, row in ledger_df.iterrows():
        action = str(row.get(action_col, "")).upper()
        if action in ("BUY", "SELL", "SHORT"):
            pending_entry = {
                "entry_time": str(row.get("timestamp", row.get("Timestamp", ""))),
                "direction": action,
                "entry_price": float(row.get("entry_price", row.get("price", 0)) or 0),
                "lots": int(row.get("lots", row.get("qty", row.get("Quantity", 1)) or 1)),
            }
        elif pending_entry and action in ("EXIT", "COVER", "PARTIAL_EXIT"):
            trade_num += 1
            entry = pending_entry["entry_price"]
            exit_p = float(row.get("price", row.get("exit_price", 0)) or 0)
            lots = pending_entry["lots"]
            direction = pending_entry["direction"]

            # Try to get PnL from CSV first
            gross = row.get("gross_pnl", row.get("pnl_cash", row.get("pnl", None)))
            if gross is not None:
                try:
                    gross = float(gross or 0)
                except (ValueError, TypeError):
                    gross = None
            # Fallback: calculate from price difference
            if gross is None or gross == 0:
                mult = 1 if direction == "BUY" else -1
                gross = (exit_p - entry) * 50 * lots * mult

            cost = row.get("total_cost", row.get("fees", 0))
            try:
                cost = float(cost or 0)
            except (ValueError, TypeError):
                cost = 0
            net = gross - cost if cost > 0 else gross

            # GSD Fix: Add cost basis (進場成本) for better visibility
            cost_basis = entry * 50 * lots
            
            trades.append({
                "#": trade_num,
                "進場時間": pending_entry["entry_time"],
                "出場時間": str(row.get("timestamp", row.get("Timestamp", ""))),
                "方向": pending_entry["direction"],
                "進場價": round(entry, 0),
                "出場價": round(exit_p, 0),
                "口數": lots,
                "進場成本": round(cost_basis, 0),
                "出場原因": action,
                "毛利": round(gross, 0),
                "摩擦成本": round(cost, 0),
                "淨利": round(net, 0),
            })
            pending_entry = None

    if pending_entry:
        trade_num += 1
        cost_basis = pending_entry["entry_price"] * 50 * pending_entry["lots"]
        trades.append({
            "#": trade_num,
            "進場時間": pending_entry["entry_time"],
            "出場時間": "⏳ 持倉中",
            "方向": pending_entry["direction"],
            "進場價": round(pending_entry["entry_price"], 0),
            "出場價": "-",
            "口數": pending_entry["lots"],
            "進場成本": round(cost_basis, 0),
            "出場原因": "-",
            "毛利": "-",
            "摩擦成本": "-",
            "淨利": "-",
        })

    return _format_coerce_floats(pd.DataFrame(trades)) if trades else ledger_df


def _format_coerce_floats(df):
    """Format price and PnL columns to 2 decimal places."""
    if df is None or df.empty or "#" not in df.columns:
        return df
    for col in ["進場價", "出場價"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").apply(
                lambda x: f"{x:.2f}" if pd.notna(x) else "-")
    # GSD Fix: Format 進場成本 with thousand separators
    for col in ["進場成本", "毛利", "摩擦成本", "淨利"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").apply(
                lambda x: f"{x:,.0f}" if pd.notna(x) else "-")
    return df


def format_stock_trades(ledger_df):
    """Round-trip formatter for stock trades. Matches BUY→SELL pairs per ticker."""
    if ledger_df is None or ledger_df.empty or "action" not in ledger_df.columns:
        return ledger_df

    # GSD: Build ticker→name lookup from indicator CSVs
    ticker_names = {}
    for f in FUTURES_MKT.glob("STOCK_*_indicators.csv"):
        try:
            tmp = pd.read_csv(f, nrows=1)
            if "name" in tmp.columns:
                t = f.stem.split("_")[1]
                ticker_names[t] = str(tmp["name"].iloc[0])
        except Exception:
            pass

    trades = []
    # GSD fix: Track pending entries by ticker to support multiple concurrent positions
    pending_entries = {}
    trade_num = 0

    for _, row in ledger_df.iterrows():
        action = str(row.get("action", "")).upper()
        ticker = str(row.get("ticker", ""))
        if action == "BUY":
            pending_entries[ticker] = {
                "entry_time": str(row.get("timestamp", "")),
                "ticker": ticker,
                "name": ticker_names.get(ticker, ""),
                "entry_price": float(row.get("entry_price", row.get("price", 0)) or 0),
                "qty": int(row.get("qty", row.get("Quantity", 0)) or 0),
                "reason": str(row.get("reason", "")),
            }
        elif action == "SELL" and ticker in pending_entries:
            trade_num += 1
            pending = pending_entries.pop(ticker)
            entry = pending["entry_price"]
            exit_p = float(row.get("price", 0) or 0)
            qty = pending["qty"]
            gross = float(row.get("pnl_gross", row.get("pnl_cash", 0)) or 0)
            fees = float(row.get("fees", 0) or 0)
            net = float(row.get("pnl_cash", 0) or 0)
            if net == 0 and gross == 0:
                net = (exit_p - entry) * qty - fees
                gross = (exit_p - entry) * qty

            trades.append({
                "#": trade_num,
                "進場時間": pending["entry_time"],
                "出場時間": str(row.get("timestamp", "")),
                "代號": f"{ticker} {pending['name']}".strip(),
                "進場價": round(entry, 0),
                "出場價": round(exit_p, 0),
                "股數": qty,
                "出場原因": str(row.get("reason", "")),
                "毛利": round(gross, 0),
                "手續費+稅": round(fees, 0),
                "淨利": round(net, 0),
            })

    # Remaining open positions
    for ticker, pending in pending_entries.items():
        trade_num += 1
        trades.append({
            "#": trade_num,
            "進場時間": pending["entry_time"],
            "出場時間": "⏳ 持倉中",
            "代號": f"{ticker} {pending['name']}".strip(),
            "進場價": round(pending["entry_price"], 0),
            "出場價": None,
            "股數": pending["qty"],
            "出場原因": "⏳",
            "毛利": None,
            "手續費+稅": None,
            "淨利": None,
        })

    return _format_coerce_floats(pd.DataFrame(trades)) if trades else ledger_df



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
def load_futures_indicators(full_history=False):
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

            # 2. V-Model fix: Remove case-insensitive duplicate columns by COALESCING them
            col_map = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume", "Amount": "amount"}
            for upper, lower in col_map.items():
                if upper in df.columns and lower in df.columns:
                    # Coalesce: use lower if not null, else upper
                    df[lower] = df[lower].fillna(df[upper])
                    df = df.drop(columns=[upper])
                elif upper in df.columns:
                    # Just rename if only upper exists
                    df = df.rename(columns={upper: lower})
            
            # 3. Final cleanup: Numeric conversion and drop rows with invalid timestamps
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                df = df.dropna(subset=["timestamp"])
            
            # Ensure numeric columns are actually numeric and handle inf
            numeric_cols = ["open", "high", "low", "close", "volume", "score"]
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            
            # Global inf fix for Plotly
            import numpy as np
            df = df.replace([np.inf, -np.inf], np.nan)
            
            return df
        except Exception:
            return None

    # 1. 優先找最近 7 天所有可能的檔案並合併 (確保夜盤跨交易日資料完整)
    import datetime as dt
    now = dt.datetime.now()
    # GSD: Include tomorrow to cover the active trading session after 15:00 rollover
    search_days_raw = [
        (now - dt.timedelta(days=3)).strftime("%Y%m%d"),
        (now - dt.timedelta(days=2)).strftime("%Y%m%d"),
        (now - dt.timedelta(days=1)).strftime("%Y%m%d"),
        now.strftime("%Y%m%d"),
        (now + dt.timedelta(days=1)).strftime("%Y%m%d"),
        (now + dt.timedelta(days=2)).strftime("%Y%m%d"),
        (now + dt.timedelta(days=3)).strftime("%Y%m%d"),
        DATE_STR,  # GSD Fix: Always include the active trading session date
    ]
    # Also find any existing MXF indicators files on disk (catch cross-weekend session dates)
    try:
        from pathlib import Path as _Path
        for f in sorted(FUTURES_MKT.glob("MXF_*_PAPER_indicators.csv")):
            parts = f.stem.split("_")
            if len(parts) >= 2:
                search_days_raw.append(parts[1])
    except Exception:
        pass
    search_days = list(dict.fromkeys(search_days_raw))  # dedupe, preserve order

    all_dfs = []
    for priority, date_part in enumerate(search_days):
        for tag in ["", "_LIVE", "_PAPER", "_DRY"]:
            for prefix in ["TMF", "MXF"]:
                f = FUTURES_MKT / f"{prefix}_{date_part}{tag}_indicators.csv"
                if f.exists():
                    df = _read_and_standardize(f)
                    if df is not None and "timestamp" in df.columns:
                        if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                        if not df.empty:
                            df["__source_priority"] = priority
                            all_dfs.append(df)

    # GSD: Parquet Fallback (Wave 18.3)
    is_fallback = False
    if not all_dfs:
        try:
            from core.data_manager import data_manager
            df_hist = data_manager.load_historical("TXFR1")
            if not df_hist.empty:
                is_fallback = True
                df_hist = df_hist.tail(100).copy()
                
                # V-Model fix: Deduplicate before rename to prevent clashes
                df_hist = df_hist.loc[:, ~df_hist.columns.duplicated()].copy()
                
                # Standardize columns carefully
                if df_hist.index.name == "timestamp" or df_hist.index.name == "ts":
                    df_hist = df_hist.reset_index()
                elif pd.api.types.is_datetime64_any_dtype(df_hist.index):
                    # If index is datetime but not named 'timestamp', reset and rename
                    df_hist = df_hist.reset_index()
                    df_hist = df_hist.rename(columns={"index": "timestamp"})
                else:
                    # If index is not datetime, create a timestamp column from index
                    df_hist = df_hist.reset_index()
                    if "index" in df_hist.columns:
                        df_hist = df_hist.rename(columns={"index": "timestamp"})
                    elif "timestamp" not in df_hist.columns:
                        # Create a dummy timestamp column if none exists
                        df_hist["timestamp"] = pd.to_datetime("2023-01-01")
                
                rename_map = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
                # Only rename if source exists AND target doesn't already exist
                actual_renames = {k: v for k, v in rename_map.items() if k in df_hist.columns and v not in df_hist.columns}
                df_hist = df_hist.rename(columns=actual_renames)
                
                # Ensure timestamp column exists and is datetime
                if "timestamp" not in df_hist.columns:
                    df_hist["timestamp"] = pd.to_datetime("2023-01-01")
                elif not pd.api.types.is_datetime64_any_dtype(df_hist["timestamp"]):
                    df_hist["timestamp"] = pd.to_datetime(df_hist["timestamp"], errors="coerce")
                
                all_dfs.append(df_hist)
        except Exception:
            pass

    result = None
    if all_dfs:
        merged = merge_indicator_frames(all_dfs)

        # [Audit Debug] Timestamp integrity after merge
        _debug_ts_integrity(merged, "merge_output", "timestamp")

        # FINAL GUARD: Ensure no duplicate columns for PyArrow
        if merged.columns.duplicated().any():
            merged = merged.loc[:, ~merged.columns.duplicated()].copy()
        
        if is_fallback:
            result = merged # Skip date filtering if loading from history
        elif full_history:
            cutoff = now - dt.timedelta(hours=24)
            result = merged[merged["timestamp"] >= cutoff].copy()
        else:
            result = filter_today(merged)
    else:
        # 2. 備案：找目錄下最新的 CSV
        try:
            all_files = list(FUTURES_MKT.glob("*_*_indicators.csv"))
            if all_files:
                latest_file = max(all_files, key=os.path.getmtime)
                df = _read_and_standardize(latest_file)
                if df is not None and "timestamp" in df.columns:
                    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                    if full_history:
                        cutoff = now - dt.timedelta(hours=24)
                        result = df[df["timestamp"] >= cutoff].copy()
                    else:
                        result = filter_today(df) if not df.empty else None
        except Exception:
            result = None

    # Stale data detection (trading-day-aware)
    if result is not None and not result.empty and "timestamp" in result.columns:
        # [Audit Debug] Timestamp integrity before extend
        _debug_ts_integrity(result, "pre_extend", "timestamp")
        
        result = extend_taifex_recess_continuity(result, timestamp_col="timestamp")
        
        # [Audit Debug] Timestamp integrity after extend
        _debug_ts_integrity(result, "post_extend", "timestamp")
        try:
            # ── Calendar time staleness ──
            result_ts = result["timestamp"].copy()
            if not pd.api.types.is_datetime64_any_dtype(result_ts):
                result_ts = pd.to_datetime(result_ts, errors="coerce")
            result_ts = result_ts.dropna()
            if not result_ts.empty:
                latest_ts = result_ts.max()
                age_secs = (pd.Timestamp.now() - latest_ts).total_seconds()

                # ── Trading day staleness ──
                from core.date_utils import get_trading_day
                expected_tday = get_trading_day(datetime.datetime.now())
                if "trading_day" in result.columns:
                    latest_tday = pd.to_datetime(result["trading_day"]).max()
                    if pd.notna(latest_tday):
                        latest_tday_date = latest_tday.date()
                    else:
                        latest_tday_date = get_trading_day(latest_ts.to_pydatetime())
                else:
                    latest_tday_date = get_trading_day(latest_ts.to_pydatetime())

                # ── Determine stale type ──
                if latest_tday_date < expected_tday:
                    # Trading day lag (e.g. still on Apr 23 when Apr 27 night session started)
                    st.warning(
                        f"📅 交易日滯後: "
                        f"目前交易日={expected_tday}, "
                        f"資料交易日={latest_tday_date}, "
                        f"最新資料時間={latest_ts.strftime('%m/%d %H:%M')}, "
                        f"資料停滯 {age_secs/60:.0f} 分鐘"
                    )
                elif age_secs > 600:
                    st.warning(f"⚠️ 期貨資料停滯 {age_secs/60:.0f} 分鐘")
                else:
                    # Show normal status in an expander or sidebar
                    pass
        except Exception:
            pass
    
    # ── [Audit Safety Net] Final sort + dedup before returning ──
    if result is not None and not result.empty and "timestamp" in result.columns:
        result["timestamp"] = pd.to_datetime(result["timestamp"], errors="coerce")
        result = result.dropna(subset=["timestamp"])
        result = result.sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last")
    
    return result

@st.cache_data(ttl=30)
def load_far_month_data(product="MXF"):
    """載入遠月合約資料
    
    Args:
        product: 商品代碼 (MXF, TMF)
        
    Returns:
        DataFrame with far month data or None
    """
    import pandas as pd
    import os
    import datetime as dt
    import glob
    
    # [Far Month Live] Priority 1: Read from trading-system's live far-month CSV
    # Format: logs/market_data/MXF_far_YYYYMMDD_PAPER.csv (from _save_far_bar)
    from pathlib import Path
    log_dir = Path("logs/market_data")
    live_far_pattern = f"{product.lower()}_far_*.csv"
    live_far_files = []
    if log_dir.exists():
        # Match any session date + tag combination
        for f in log_dir.glob(live_far_pattern):
            if f.stat().st_size > 80:  # At least a few bars
                live_far_files.append(f)
    if live_far_files:
        live_far_files.sort(key=os.path.getmtime, reverse=True)
        try:
            df_far = pd.read_csv(live_far_files[0])
            if "timestamp" in df_far.columns:
                df_far["timestamp"] = pd.to_datetime(df_far["timestamp"], errors="coerce")
                df_far = df_far.dropna(subset=["timestamp"])
                df_far = df_far.sort_values("timestamp")
                # Standardize column names
                col_map = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
                for upper, lower in col_map.items():
                    if upper in df_far.columns and lower not in df_far.columns:
                        df_far = df_far.rename(columns={upper: lower})
                if len(df_far) >= 2:
                    return df_far
        except Exception as e:
            print(f"Live far CSV read failed: {e}")
    
    # Priority 2: Search static far-month data files (legacy)
    search_patterns = [
        f"./data/{product.lower()}_far_*.csv",
        f"./data/{product.lower()}_far.csv",
        f"./logs/market_data/{product}_*_far_*.csv",
        f"./exports/{product.lower()}_far_*.csv",
    ]
    
    far_files = []
    for pattern in search_patterns:
        far_files.extend(glob.glob(pattern))
    
    # 按修改時間排序，取最新的檔案
    if far_files:
        far_files.sort(key=os.path.getmtime, reverse=True)
        latest_far_file = far_files[0]
        
        try:
            df_far = pd.read_csv(latest_far_file)
            
            # 處理 timestamp 欄位
            if "timestamp" not in df_far.columns:
                if df_far.index.name == "timestamp" or df_far.index.name == "ts":
                    df_far = df_far.reset_index()
                elif "ts" in df_far.columns:
                    df_far = df_far.rename(columns={"ts": "timestamp"})
                elif "datetime" in df_far.columns:
                    df_far = df_far.rename(columns={"datetime": "timestamp"})
                else:
                    # 使用第一欄作為 timestamp
                    df_far = df_far.rename(columns={df_far.columns[0]: "timestamp"})
            
            # 確保 timestamp 是 datetime 類型
            df_far["timestamp"] = pd.to_datetime(df_far["timestamp"], errors="coerce")
            df_far = df_far.dropna(subset=["timestamp"])
            
            # 標準化欄位名稱
            col_map = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
            for upper, lower in col_map.items():
                if upper in df_far.columns and lower not in df_far.columns:
                    df_far = df_far.rename(columns={upper: lower})
            
            # 確保有 close 欄位
            if "close" not in df_far.columns and "Close" in df_far.columns:
                df_far = df_far.rename(columns={"Close": "close"})
            
            # 按時間排序
            df_far = df_far.sort_values("timestamp")
            
            return df_far
            
        except Exception as e:
            print(f"載入遠月資料失敗: {e}")
            return None
    
    return None

@st.cache_data(ttl=30)
def load_calendar_spread_data():
    """載入日曆價差資料 (近月/遠月合約價差)
    
    優先載入預先計算的價差檔案，如果不存在則嘗試從近月/遠月資料計算
    """
    try:
        import pandas as pd
        import numpy as np
        from pathlib import Path
        
        # 優先尋找預先計算的價差檔案
        spread_files = list(Path("data").glob("*spread*.csv"))
        if not spread_files:
            spread_files = list(Path(".").rglob("*calendar*spread*.csv"))
        
        if spread_files:
            # 選擇最新的檔案
            latest_file = max(spread_files, key=lambda p: p.stat().st_mtime)
            df = pd.read_csv(latest_file)
            
            # 標準化 timestamp 欄位
            if "timestamp" not in df.columns:
                if "ts" in df.columns:
                    df = df.rename(columns={"ts": "timestamp"})
                elif df.index.name == "timestamp":
                    df = df.reset_index()
                elif len(df.columns) > 0:
                    df = df.rename(columns={df.columns[0]: "timestamp"})
            
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                df = df.dropna(subset=["timestamp"])
            
            # 確保數值欄位是數值類型
            numeric_cols = ["spread", "spread_z", "spread_ma", "spread_std", 
                           "vwap_z", "price_vs_vwap", "Close_near", "Close_far"]
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            
            # 處理無限值
            df = df.replace([np.inf, -np.inf], np.nan)
            
            print(f"[Calendar Spread] 載入價差資料: {len(df)} 筆, 來自 {latest_file.name}")
            return df
        
        # 如果沒有預先計算的檔案，嘗試從近月/遠月資料計算
        print("[Calendar Spread] 沒有預先計算的價差檔案，嘗試計算...")
        
        # 載入近月資料
        df_near = load_futures_indicators(full_history=True)
        if df_near is None or df_near.empty:
            print("[Calendar Spread] 無法載入近月資料")
            return pd.DataFrame()
        
        # 嘗試尋找遠月資料檔案
        far_files = list(Path("data").glob("*far*.csv"))
        if not far_files:
            # 嘗試在 logs/market_data 中尋找
            far_files = list(Path("logs/market_data").glob("*far*.csv"))
        if not far_files:
            # 嘗試在 exports 中尋找
            far_files = list(Path("exports").glob("*far*.csv"))
        if not far_files:
            # 嘗試尋找 MXF 遠月資料
            far_files = list(Path(".").rglob("*MXF*far*.csv"))
        if not far_files:
            # 嘗試尋找任何包含 "far" 的 CSV 檔案
            far_files = list(Path(".").rglob("*far*.csv"))
        
        # 載入遠月資料
        df_far = pd.read_csv(far_files[0])
        
        # 標準化遠月資料
        if "timestamp" not in df_far.columns:
            if "ts" in df_far.columns:
                df_far = df_far.rename(columns={"ts": "timestamp"})
            elif df_far.index.name == "timestamp":
                df_far = df_far.reset_index()
        
        if "timestamp" in df_far.columns:
            df_far["timestamp"] = pd.to_datetime(df_far["timestamp"], errors="coerce")
            df_far = df_far.dropna(subset=["timestamp"])
        
        # 合併近月和遠月資料
        df_merged = pd.merge(
            df_near[["timestamp", "close"]].rename(columns={"close": "Close_near"}),
            df_far[["timestamp", "close"]].rename(columns={"close": "Close_far"}),
            on="timestamp",
            how="inner"
        )
        
        if df_merged.empty:
            print("[Calendar Spread] 近月/遠月資料沒有重疊的時間戳記")
            return pd.DataFrame()
        
        # 計算價差
        df_merged["spread"] = df_merged["Close_near"] - df_merged["Close_far"]
        
        # 計算滾動統計量 (20期窗口)
        window = 20
        df_merged["spread_ma"] = df_merged["spread"].rolling(window=window, min_periods=window).mean()
        df_merged["spread_std"] = df_merged["spread"].rolling(window=window, min_periods=window).std()
        
        # 計算 Z-score
        safe_spread_std = df_merged["spread_std"].replace(0, np.nan)
        df_merged["spread_z"] = (df_merged["spread"] - df_merged["spread_ma"]) / safe_spread_std
        
        # 計算 VWAP Z-score (使用近月價格)
        df_merged["vwap"] = df_merged["Close_near"].rolling(window=window, min_periods=window).mean()
        df_merged["vwap_std"] = df_merged["Close_near"].rolling(window=window, min_periods=window).std()
        safe_vwap_std = df_merged["vwap_std"].replace(0, np.nan)
        df_merged["vwap_z"] = (df_merged["Close_near"] - df_merged["vwap"]) / safe_vwap_std
        
        # 計算價格 vs VWAP
        df_merged["price_vs_vwap"] = df_merged["Close_near"] - df_merged["vwap"]
        
        print(f"[Calendar Spread] 計算價差資料完成: {len(df_merged)} 筆")
        return df_merged
        
    except Exception as e:
        print(f"[Calendar Spread] 載入價差資料錯誤: {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()

@st.cache_data(ttl=5)
def load_futures_trades():
    """Load today's futures trades CSV.
    Preference order:
      1. exports/trades TMF_{TRADE_DATE_STR}_trades.csv
      2. exports/trades TMF_{DATE_STR}_trades.csv
      3. logs/market_data TMF_{TRADE_DATE_STR}*_trades.csv
      4. logs/market_data TMF_{DATE_STR}*_trades.csv
    Returns a tuple (DataFrame or None, actual_date_str).
    GSD Fix: Return tuple to show which date's file was actually loaded.
    """
    import glob
    # Try canonical exports location first
    for date_str in [TRADE_DATE_STR, DATE_STR]:
        f_exact = FUTURES_TRADES / f"TMF_{date_str}_trades.csv"
        if f_exact.exists():
            try:
                return pd.read_csv(f_exact), date_str
            except Exception:
                pass
    # Fallback: search market_data for any matching pattern (prefer newest)
    for date_str in [TRADE_DATE_STR, DATE_STR]:
        pattern = str(FUTURES_MKT / f"TMF_{date_str}*trades.csv")
        matches = glob.glob(pattern)
        if matches:
            # pick newest by mtime
            matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for m in matches:
                try:
                    return pd.read_csv(m), date_str
                except Exception:
                    continue
    # Final fallback: any TMF_*_trades.csv in exports/trades or market_data
    try:
        ex_matches = list(FUTURES_TRADES.glob("TMF_*_trades.csv"))
        m_matches = list(FUTURES_MKT.glob("TMF_*_trades.csv"))
        all_matches = ex_matches + m_matches
        if all_matches:
            latest = max(all_matches, key=os.path.getmtime)
            try:
                df = pd.read_csv(latest)
                # Extract date from filename (TMF_YYYYMMDD_trades.csv)
                actual_date = latest.stem.split("_")[1] if "_" in latest.stem else "unknown"
                return df, actual_date
            except Exception:
                pass
    except Exception:
        pass
    return None, None

OPTIONS_SUB = "live_trading" if o_live else "paper_trading"

@st.cache_data(ttl=5)
def load_options_indicators(full_history=False):
    # GSD: Load multiple days to cover full trading session
    import datetime as dt
    now = dt.datetime.now()
    # 交易日邏輯：15:00 之後歸屬明天
    t_day_str = get_session_date_str(now)
    
    days = [
        (now - dt.timedelta(days=1)).strftime("%Y%m%d"),
        now.strftime("%Y%m%d"),
        t_day_str
    ]
    # Deduplicate days
    days = sorted(list(set(days)))
    
    all_dfs = []
    source_candidates = [OPTIONS_SUB]
    fallback_sub = "paper_trading" if OPTIONS_SUB == "live_trading" else "live_trading"

    # Prefer the active runtime mode and only fall back to the other mode when
    # the active mode has no indicator files at all. Mixing both widens the MTX
    # chart range with stale sessions and makes live charts look zoomed out.
    for source_index, sub in enumerate(source_candidates):
        source_dfs = []
        for priority, d_str in enumerate(days):
            f = OPTIONS_REPO / "logs" / sub / f"OPTIONS_{d_str}_indicators.csv"
            if f.exists():
                try:
                    df = pd.read_csv(f)
                    if not df.empty:
                        if "timestamp" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                        df["__source_priority"] = priority
                        df["__source_mode"] = sub
                        source_dfs.append(df)
                except Exception:
                    continue
        if source_dfs:
            all_dfs = source_dfs
            break
        if source_index == 0:
            source_candidates.append(fallback_sub)

    # GSD: Parquet Fallback (Wave 18.3)
    if not all_dfs:
        try:
            from core.data_manager import data_manager
            df_hist = data_manager.load_historical("OPTIONS")
            if not df_hist.empty:
                df_hist = df_hist.tail(100).copy()
                if df_hist.index.name != "timestamp":
                    df_hist = df_hist.reset_index().rename(columns={"index": "timestamp"})
                all_dfs.append(df_hist)
        except Exception:
            pass

    result = None
    if all_dfs:
        try:
            merged = merge_indicator_frames(all_dfs)
            
            # [Audit Debug] Options data — timestamp integrity after merge
            _debug_ts_integrity(merged, "options_merge_output", "timestamp")
            
            # Standardize MTX price column name
            if "mtx_close" in merged.columns and "price_mtx" not in merged.columns:
                merged = merged.rename(columns={"mtx_close": "price_mtx"})
            elif "mtx_close" in merged.columns and "price_mtx" in merged.columns:
                merged["price_mtx"] = merged["price_mtx"].fillna(merged["mtx_close"])
                merged = merged.drop(columns=["mtx_close"])
                
            if merged.columns.duplicated().any():
                merged = merged.loc[:, ~merged.columns.duplicated()].copy()
            
            if full_history:
                cutoff = now - dt.timedelta(hours=24)
                result = merged[merged["timestamp"] >= cutoff].copy()
            else:
                # 💡 GSD: We want to see data belonging to the CURRENT trading session
                # If it's 09:00 AM, we want to see data from 15:00 (yesterday) onwards.
                result = filter_today(merged, ts_col="timestamp")
        except Exception:
            pass
            
    if result is None or result.empty:
        # 2. 備案：找目錄下最新的任何指標檔案（防斷鍊）
        try:
            all_opt_files = list((OPTIONS_REPO / "logs").rglob("OPTIONS_*_indicators.csv"))
            if all_opt_files:
                latest_f = max(all_opt_files, key=os.path.getmtime)
                df = pd.read_csv(latest_f)
                if not df.empty:
                    if "timestamp" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                    if full_history:
                        cutoff = now - dt.timedelta(hours=24)
                        result = df[df["timestamp"] >= cutoff].copy()
                    else:
                        result = filter_today(df, ts_col="timestamp")
        except Exception:
            pass
            
    return result

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
    current_date_str = get_session_date_str(datetime.datetime.now())
    f = resolve_preferred_or_latest_file(
        FUTURES_TRADES,
        f"STOCK_{current_date_str}_{mode}_trades.csv",
        f"STOCK_*_{mode}_trades.csv",
    )
    if f and f.exists():
        try:
            return pd.read_csv(f)
        except Exception:
            pass
    return None

@st.cache_data(ttl=5)
def load_stock_orders(mode="PAPER"):
    current_date_str = get_session_date_str(datetime.datetime.now())
    orders_file = resolve_stock_orders_file(FUTURES_TRADES, current_date_str, mode)
    if orders_file and orders_file.exists():
        try:
            with open(orders_file, "r", encoding="utf-8") as f:
                orders_data = json.load(f)
            if orders_data:
                return orders_data
        except Exception:
            pass

    return build_stock_orders_from_trades(load_stock_trades(mode), mode=mode)

@st.cache_data(ttl=5)
def load_stock_indicators(ticker):
    current_date_str = get_session_date_str(datetime.datetime.now())
    f = resolve_preferred_or_latest_file(
        FUTURES_MKT,
        f"STOCK_{ticker}_{current_date_str}_indicators.csv",
        f"STOCK_{ticker}_*_indicators.csv",
    )
    if f and f.exists():
        try:
            df = pd.read_csv(f)
            if df.columns.duplicated().any():
                df = df.loc[:, ~df.columns.duplicated()].copy()
            
            # 處理大小寫不一致的列名
            column_mapping = {}
            for col in df.columns:
                col_lower = col.lower()
                # 基本價格/成交量列
                if col_lower == 'close':
                    column_mapping[col] = 'close'
                elif col_lower == 'open':
                    column_mapping[col] = 'open'
                elif col_lower == 'high':
                    column_mapping[col] = 'high'
                elif col_lower == 'low':
                    column_mapping[col] = 'low'
                elif col_lower == 'volume':
                    column_mapping[col] = 'volume'
                elif col_lower == 'timestamp':
                    column_mapping[col] = 'timestamp'
                elif col_lower == 'ts':
                    column_mapping[col] = 'timestamp'
                elif col_lower == 'name':
                    column_mapping[col] = 'name'
                # 技術指標列
                elif col_lower == 'score':
                    column_mapping[col] = 'score'
                elif col_lower == 'bb_lower':
                    column_mapping[col] = 'bb_lower'
                elif col_lower == 'bb_mid':
                    column_mapping[col] = 'bb_mid'
                elif col_lower == 'bb_upper':
                    column_mapping[col] = 'bb_upper'
                elif col_lower == 'sqz_on':
                    column_mapping[col] = 'sqz_on'
                elif col_lower == 'rsi':
                    column_mapping[col] = 'rsi'
                elif col_lower == 'macd':
                    column_mapping[col] = 'macd'
                elif col_lower == 'macd_signal':
                    column_mapping[col] = 'macd_signal'
                elif col_lower == 'macd_hist':
                    column_mapping[col] = 'macd_hist'
                elif col_lower == 'k_val':
                    column_mapping[col] = 'k_val'
                elif col_lower == 'd_val':
                    column_mapping[col] = 'd_val'
                elif col_lower == 'adx':
                    column_mapping[col] = 'adx'
            
            # 重命名列
            df = df.rename(columns=column_mapping)
            
            # 確保必要的列存在
            required_cols = ['close', 'open', 'high', 'low', 'volume', 'timestamp']
            for col in required_cols:
                if col not in df.columns:
                    df[col] = None
            
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
tab_overview, tab_futures, tab_options, tab_stocks, tab_pipeline, tab_settings = st.tabs([
    "總覽", "期貨 TMF", "選擇權 TXO", "台股 Stocks", "策略管道", "設定"
])

# ════════════════════════════════════════
# Tab 1: 總覽
# ════════════════════════════════════════
with tab_overview:
    col1, col2 = st.columns(2)
    f_df = load_futures_indicators(full_history=cont_mode)
    o_df = load_options_indicators(full_history=cont_mode)

    with col1:
        st.header(f"期貨 MXF ({mode_badge(f_live)})")
        if f_df is not None and not f_df.empty:
            last = f_df.iloc[-1]
            c1, c2, c3 = st.columns(3)
            # Robust coercion to scalar for display
            cl_val = _to_num(last.get('close') if 'close' in last else last.get('Close', 0))
            sc_val = _to_num(last.get('score', 0))
            c1.metric("Close", f"{cl_val:.0f}")
            c2.metric("Score", f"{sc_val:.1f}")
            c3.metric("Bars", len(f_df))
        else:
            st.info("無期貨指標數據")
        ft, ft_date = load_futures_trades()
        # GSD Fix: Show which date's file was actually loaded to avoid confusion during night sessions
        trading_day_str = TRADE_DATE_STR
        trading_day_display = f"{trading_day_str[:4]}-{trading_day_str[4:6]}-{trading_day_str[6:8]}"
        
        futures_entry_count = count_futures_entries(ft)
        if ft is not None and ft_date and ft_date != trading_day_str:
            # Night session: file date differs from trading day
            file_date_display = f"{ft_date[:4]}-{ft_date[4:6]}-{ft_date[6:8]}"
            st.write(f"交易日 {trading_day_display} (檔案日期: {file_date_display}) 交易: {futures_entry_count} 筆")
        else:
            st.write(f"交易日 {trading_day_display} 交易: {futures_entry_count} 筆")

    with col2:
        st.header(f"選擇權 TXO ({mode_badge(o_live)})")
        if o_df is not None and not o_df.empty:
            last = o_df.iloc[-1]
            c1, c2, c3 = st.columns(3)
            # Robust coercion to scalar for display
            mtx_val = _to_num(last.get('price_mtx', 0))
            sc_val = _to_num(last.get('score', 0))
            c1.metric("MTX", f"{mtx_val:.0f}")
            c2.metric("Score", f"{sc_val:.1f}")
            c3.metric("Bars", len(o_df))
        else:
            st.info("無選擇權指標數據")
        ol = load_options_ledger()
        if ol is not None and not ol.empty and "Timestamp" in ol.columns:
            ol["Timestamp"] = pd.to_datetime(ol["Timestamp"], errors="coerce")
            ol = ol.dropna(subset=["Timestamp"])
            
            options_entry_count = count_options_entries(ol, DATE_STR)
            st.write(f"交易日 {DATE_STR[:4]}-{DATE_STR[4:6]}-{DATE_STR[6:8]} 交易: {options_entry_count} 筆")
        else:
            st.write("今日交易: 0 筆")

    # ── 總覽圖：指數走勢（期貨 + 選擇權 MTX 統一 Y 軸）──
    st.header("今日指數走勢")
    fig = go.Figure()
    has_data = False
    
    if f_df is not None and not f_df.empty:
        f_close = f_df["close"] if "close" in f_df.columns else f_df["Close"]
        fig.add_trace(go.Scatter(
            x=f_df["timestamp"].to_numpy(),
            y=f_close.to_numpy(),
            name="TMF (期貨)",
            line=dict(color="#1f77b4", width=2)
        ))
        has_data = True
        
    if o_df is not None and not o_df.empty:
        # Compatibility fix: use mtx_close if price_mtx is missing
        m_col = "price_mtx" if "price_mtx" in o_df.columns else ("mtx_close" if "mtx_close" in o_df.columns else None)
        if m_col:
            fig.add_trace(go.Scatter(
                x=o_df["timestamp"].to_numpy(),
                y=o_df[m_col].to_numpy(),
                name="MTX (選擇權標的)",
                line=dict(color="#ff7f0e", width=1.5, dash="dot")
            ))
            has_data = True
            
    if has_data:
        fig.update_layout(
            height=400, 
            margin=dict(t=10, b=10, l=40, r=20), 
            legend=dict(orientation="h", y=1.05, x=0.5, xanchor="center"),
            hovermode="x unified"
        )
        fig.update_yaxes(title_text="指數點位", tickformat=",.0f", gridcolor="rgba(128,128,128,0.1)")
        fig.update_xaxes(gridcolor="rgba(128,128,128,0.1)")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("等待數據...")

    # ── 總覽 PnL ──
    st.header("今日累計 PnL")
    pc1, pc2, pc3 = st.columns(3)
    ft, _ = load_futures_trades()
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
                # 計算趨勢與噴發偏向 (Bias) - 整合趨勢感
                bull = last.get("bullish_align", False)
                bear = last.get("bearish_align", False)
                mom = last.get("momentum", 0)
                mom_prev = s_df["momentum"].iloc[-2] if len(s_df) > 1 else 0
                
                bias = "⚪中性"
                if mom > 0:
                    if bull:
                        bias = "🚀強勢多" if mom >= mom_prev else "↗️多轉弱"
                    else:
                        bias = "⚠️空反彈" if mom >= mom_prev else "↗️弱反彈"
                elif mom < 0:
                    if bear:
                        bias = "💀強勢空" if mom <= mom_prev else "↘️空轉弱"
                    else:
                        bias = "⚠️多拉回" if mom <= mom_prev else "↘️弱拉回"

                # 處理 volume 值，避免 NaN 錯誤
                volume_val = last.get('volume', last.get('Volume', 0))
                if pd.isna(volume_val):
                    volume_display = "0k"
                else:
                    volume_display = f"{int(volume_val // 1000)}k"
                
                ov_data.append({
                    "代號": ticker,
                    "名稱": last.get("name", "Unknown"),
                    "股價": last.get('close', last.get('Close', 0)),
                    "量": volume_display,
                    "Score": round(last.get('score', 0), 1),
                    "Sqz": "🔒壓" if last.get("sqz_on", False) else "🔓釋",
                    "偏向": bias,
                    "投信": "🔥連買" if last.get("it_buy_rolling_count", 0) >= 2 else "⚪ —",
                    "200MA": "🟢上" if last.get("ema_200_up", False) else "⚪ —"
                })
        
        if ov_data:
            ov_df = pd.DataFrame(ov_data)
            def style_overview(row):
                styles = [''] * len(row)
                if "🔒" in str(row["Sqz"]):
                    styles[5] = 'background-color: #fee2e2; color: #b91c1c; font-weight: bold'
                
                # 噴發偏向著色
                if "🚀" in str(row["偏向"]) or "↗️" in str(row["偏向"]):
                    styles[6] = 'color: #059669; font-weight: bold'
                elif "💀" in str(row["偏向"]) or "↘️" in str(row["偏向"]):
                    styles[6] = 'color: #dc2626; font-weight: bold'

                if "🔥" in str(row["投信"]):
                    styles[7] = 'background-color: #dcfce7; color: #065f46; font-weight: bold'
                if "🟢" in str(row["200MA"]):
                    styles[8] = 'color: #059669; font-weight: bold'
                return styles
            
            # CSS 縮小字體
            st.markdown("""
                <style>
                [data-testid="stMetricValue"] { font-size: 1.4rem !important; }
                [data-testid="stMetricLabel"] { font-size: 0.8rem !important; }
                .styled-table { font-size: 0.8rem !important; }
                </style>
            """, unsafe_allow_html=True)
            
            st.dataframe(ov_df.style.apply(style_overview, axis=1), use_container_width=True, hide_index=True)
        else:
            st.info("等待個股指標數據...")
    else:
        st.info("尚未設定監控名單")

# ════════════════════════════════════════
# Tab 2: 期貨
# ════════════════════════════════════════
with tab_futures:
    st.header(f"期貨 MXF ({mode_badge(f_live)})")

    f_df = load_futures_indicators(full_history=cont_mode)
    if f_df is not None and not f_df.empty:
        last = f_df.iloc[-1]
        fc1, fc2, fc3, fc4, fc5 = st.columns(5)
        
        # 噴發偏向 (Bias) 計算 - 整合趨勢感
        bull = last.get("bullish_align", False)
        bear = last.get("bearish_align", False)
        mom = last.get("momentum", 0)
        mom_prev = f_df["momentum"].iloc[-2] if len(f_df) > 1 else 0
        
        bias = "⚪中性"
        if mom > 0:
            if bull:
                bias = "🚀強勢多" if mom >= mom_prev else "↗️多轉弱"
            else:
                bias = "⚠️空反彈" if mom >= mom_prev else "↗️弱反彈"
        elif mom < 0:
            if bear:
                bias = "💀強勢空" if mom <= mom_prev else "↘️空轉弱"
            else:
                bias = "⚠️多拉回" if mom <= mom_prev else "↘️弱拉回"

        # Robust coercion to scalar for display
        cl_val = _to_num(last.get('close') if 'close' in last else last.get('Close', 0))
        sc_val = _to_num(last.get('score', 0))

        fc1.metric("Close", f"{cl_val:.0f}")
        fc2.metric("Score", f"{sc_val:.1f}")
        bull = last.get("bull_align", last.get("bullish_align", False))
        bear = last.get("bear_align", last.get("bearish_align", False))
        trend = "🟢多頭" if bull else ("🔴空頭" if bear else "⚪中性")
        fc3.metric("趨勢", trend)
        fc4.metric("Sqz狀態", "🔒壓縮" if last.get("sqz_on", False) is True else "🔓釋放")
        fc5.metric("噴發向", bias)

        if "fired" in last and last.get("fired", False) is True:
            st.success("🔥 **FIRE — 壓縮釋放！**")
        
        ft, _ = load_futures_trades()
        
        # 載入遠月資料
        df_far = load_far_month_data("MXF")
        
        # 使用雙合約圖表顯示近月和遠月價格
        if df_far is not None and not df_far.empty:
            st.plotly_chart(
                make_futures_dual_chart(
                    f_df, 
                    df_far, 
                    "MXF 近月/遠月價格 & Score", 
                    signals=ft
                ), 
                use_container_width=True
            )
        else:
            # 如果沒有遠月資料，使用原來的圖表
            st.plotly_chart(
                make_price_score_chart(f_df, "close", "MXF 價格 & Score", signals=ft), 
                use_container_width=True
            )
            st.info("⚠️ 未找到遠月資料，僅顯示近月價格")
        
        st.dataframe(f_df.tail(20), use_container_width=True)
    else:
        st.info("無數據")
    
    # ── Calendar Spread 顯示 ──
    st.header("📊 日曆價差分析 (Calendar Spread)")
    
    with st.expander("📈 價差圖表與策略條件", expanded=True):
        # 載入 calendar spread 資料
        spread_df = load_calendar_spread_data()
        
        if spread_df is not None and not spread_df.empty:
            # 顯示最新價差狀態
            last_spread = spread_df.iloc[-1]
            
            # 創建指標卡片
            col1, col2, col3, col4 = st.columns(4)
            
            with col1:
                if "spread" in last_spread:
                    spread_val = last_spread["spread"]
                    st.metric("價差 (近月-遠月)", f"{spread_val:.1f} pts")
            
            with col2:
                if "spread_z" in last_spread:
                    spread_z = last_spread["spread_z"]
                    # 根據 Z-score 顯示狀態
                    if spread_z > 3.0:
                        status = "🔴 做空價差機會"
                    elif spread_z < -3.0:
                        status = "🟢 做多價差機會"
                    elif abs(spread_z) < 0.5:
                        status = "⚪ 中性區間"
                    else:
                        status = "🟡 觀察中"
                    st.metric("Spread Z-score", f"{spread_z:.2f}", delta=status)
            
            with col3:
                if "Close_near" in last_spread and "Close_far" in last_spread:
                    near_price = last_spread["Close_near"]
                    far_price = last_spread["Close_far"]
                    st.metric("近月價格", f"{near_price:.0f}")
            
            with col4:
                if "Close_far" in last_spread:
                    st.metric("遠月價格", f"{far_price:.0f}")
            
            # 顯示 Calendar Condor 策略條件狀態
            st.subheader("🎯 Calendar Condor 策略條件")
            
            cond_col1, cond_col2, cond_col3 = st.columns(3)
            
            with cond_col1:
                if "spread_z" in last_spread:
                    spread_z = last_spread["spread_z"]
                    # 做空價差條件
                    if spread_z > 3.0:
                        st.success("✅ 做空價差條件觸發")
                        st.caption(f"Spread Z-score: {spread_z:.2f} > 3.0")
                    else:
                        st.info("⏳ 等待做空價差條件")
                        st.caption(f"需要 Spread Z-score > 3.0 (目前: {spread_z:.2f})")
            
            with cond_col2:
                if "spread_z" in last_spread:
                    # 做多價差條件
                    if spread_z < -3.0:
                        st.success("✅ 做多價差條件觸發")
                        st.caption(f"Spread Z-score: {spread_z:.2f} < -3.0")
                    else:
                        st.info("⏳ 等待做多價差條件")
                        st.caption(f"需要 Spread Z-score < -3.0 (目前: {spread_z:.2f})")
            
            with cond_col3:
                if "spread_z" in last_spread:
                    # 出場條件
                    if abs(spread_z) < 0.5:
                        st.success("✅ 出場條件觸發")
                        st.caption(f"Spread Z-score: {spread_z:.2f} 接近 0")
                    else:
                        st.info("⏳ 持倉中")
                        st.caption(f"等待 Spread Z-score 回歸到 ±0.5 內")
            
            # 顯示價差圖表
            spread_chart = make_calendar_spread_chart(spread_df)
            if spread_chart:
                st.plotly_chart(spread_chart, use_container_width=True)
            
            # 顯示價差資料表格
            with st.expander("📋 價差資料明細"):
                # 只顯示重要欄位
                display_cols = ["timestamp", "Close_near", "Close_far", "spread", "spread_z", "spread_ma", "spread_std"]
                available_cols = [col for col in display_cols if col in spread_df.columns]
                
                if available_cols:
                    st.dataframe(spread_df[available_cols].tail(20), use_container_width=True)
                else:
                    st.dataframe(spread_df.tail(20), use_container_width=True)
        else:
            st.warning("⚠️ 無法載入日曆價差資料")
            st.info("""
            可能原因：
            1. 沒有遠月合約資料檔案
            2. 近月/遠月資料時間戳記沒有重疊
            3. 尚未執行 calendar spread 資料收集
            
            解決方法：
            - 執行 `scripts/fetch_calendar_spread_data_fixed.py` 收集遠月資料
            - 檢查 `data/` 目錄是否有 `*spread*.csv` 或 `*far*.csv` 檔案
            """)
    
    ft, _ = load_futures_trades()
    if ft is not None and not ft.empty:
        # --- Unrealized PnL ---
        round_trips = format_futures_trades(ft)
        open_pos = find_latest_open_futures_position(ft)

        if open_pos is not None:
            col1, col2 = st.columns([3, 1])
            with col1:
                st.subheader("📊 未實現損益 (持倉中)")
            with col2:
                if st.button("🔄 更新", key="update_futures_unrealized"):
                    st.cache_data.clear()
                    st.rerun()
            
            cur_price = float(f_df["close"].iloc[-1]) if len(f_df) > 0 else 0
            entry = float(open_pos.entry_price)
            lots = int(open_pos.lots)
            direction = str(open_pos.direction)
            if cur_price > 0 and entry > 0:
                mult = 1 if direction == "BUY" else -1
                unrealized = (cur_price - entry) * 50 * lots * mult
                uc1, uc2, uc3 = st.columns(3)
                uc1.metric("成交成本", f"{open_pos.cost_basis:,.0f} TWD")
                uc2.metric("未實現損益", f"{unrealized:+,.0f} TWD")
                uc3.metric("目前價", f"{cur_price:.0f}", delta=f"{cur_price-entry:+.0f} pts")
                st.caption(f"進場價: {entry:.0f} | 目前: {cur_price:.0f} | {direction} {lots}口 | 報酬率 {(unrealized/(entry*50*lots)*100):+.1f}%")

        st.header("交易記錄 (Round-Trip)")
        round_trips = format_futures_trades(ft)
        if round_trips is not None and not round_trips.empty and "#" in round_trips.columns:
            def style_trades(row):
                pnl = row.get("淨利", "-")
                if pnl != "-" and isinstance(pnl, (int, float)):
                    color = '#dcfce7' if pnl > 0 else ('#fef2f2' if pnl < 0 else '')
                    return [f'background-color: {color}; font-weight: bold'] * len(row)
                return [''] * len(row)
            st.dataframe(round_trips.style.apply(style_trades, axis=1), use_container_width=True, hide_index=True)
        else:
            st.dataframe(ft, use_container_width=True)
        fpnl = calc_futures_pnl(ft)
        fig = make_pnl_chart(fpnl, "期貨累計 PnL (TWD)")
        if fig:
            st.plotly_chart(fig, use_container_width=True)

        with st.expander("📋 原始 Ledger (進階)"):
            st.dataframe(ft, use_container_width=True)

    # ── Order Status Panel ──
    with st.expander("📤 委託單狀態 (Order Lifecycle)", expanded=False):
        orders_path = BASE / "exports" / "trades"
        order_files = list(orders_path.glob(f"TMF_{DATE_STR}_orders.json")) + list(orders_path.glob("TMF_*_orders.json"))
        order_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        if order_files and order_files[0].exists():
            with open(order_files[0], "r", encoding="utf-8") as f:
                orders_data = json.load(f)

            if orders_data:
                # Get LIVE price from the same data source as charts
                f_df = load_futures_indicators(full_history=cont_mode)
                live_price = None
                if f_df is not None and not f_df.empty and "Close" in f_df.columns:
                    live_price = float(f_df["Close"].iloc[-1])

                df_orders = pd.DataFrame(orders_data)

                # Status translation map
                status_map = {
                    "pending_submit": "⏳ 待傳送",
                    "pre_submitted": "📅 預約單",
                    "submitted": "📨 已委託",
                    "partial_filled": "⚡ 部分成交",
                    "filled": "✅ 完全成交",
                    "cancelled": "🚫 已取消",
                    "rejected": "❌ 已退單",
                    "expired": "⏰ 已過期",
                }
                type_map = {
                    "market": "市價",
                    "limit": "限價",
                    "stop": "停損",
                    "stop_limit": "停損限價",
                }

                # Display columns
                display_cols = []
                if "order_id" in df_orders.columns:
                    display_cols.append("order_id")
                if "created_at" in df_orders.columns:
                    display_cols.append("created_at")
                if "side" in df_orders.columns:
                    df_orders["方向"] = df_orders["side"].map({"buy": "買入", "sell": "賣出"})
                    display_cols.append("方向")
                if "order_type" in df_orders.columns:
                    df_orders["委託類型"] = df_orders["order_type"].map(type_map).fillna(df_orders["order_type"])
                    display_cols.append("委託類型")
                if "quantity" in df_orders.columns:
                    display_cols.append("quantity")
                if "filled_quantity" in df_orders.columns:
                    display_cols.append("filled_quantity")
                if "price" in df_orders.columns:
                    display_cols.append("price")
                if "avg_fill_price" in df_orders.columns:
                    display_cols.append("avg_fill_price")
                if "status" in df_orders.columns:
                    df_orders["狀態"] = df_orders["status"].map(status_map).fillna(df_orders["status"])
                    display_cols.append("狀態")
                if "strategy" in df_orders.columns:
                    display_cols.append("strategy")

                # Calculate unrealized PnL using LIVE price
                if live_price and live_price > 0:
                    def _calc_unreal(row):
                        if row.get("status") not in ("filled", "partial_filled"):
                            return None
                        entry = row.get("avg_fill_price", 0) or row.get("price", 0)
                        if not entry or entry <= 0:
                            return None
                        side = row.get("side", "")
                        qty = row.get("filled_quantity", 1) or 1
                        if side == "buy":
                            return (live_price - entry) * 50 * qty
                        elif side == "sell":
                            return (entry - live_price) * 50 * qty
                        return None

                    df_orders["unrealized_pnl"] = df_orders.apply(_calc_unreal, axis=1)

                    def _format_unreal(x):
                        if x is None or (isinstance(x, float) and pd.isna(x)):
                            return "—"
                        elif x > 0:
                            return f"🟢 {x:+,.0f}"
                        elif x < 0:
                            return f"🔴 {x:+,.0f}"
                        else:
                            return "⚪ 0"
                    df_orders["未實現損益"] = df_orders["unrealized_pnl"].apply(_format_unreal)
                    display_cols.append("未實現損益")

                    # Store live price for display
                    df_orders["current_price"] = live_price
                    display_cols.append("current_price")

                if display_cols:
                    st.dataframe(df_orders[display_cols], use_container_width=True, hide_index=True,
                                 column_config={
                                     "order_id": "委託單ID",
                                     "created_at": "建立時間",
                                     "方向": "方向",
                                     "委託類型": st.column_config.TextColumn("委託類型"),
                                     "quantity": "委託量",
                                     "filled_quantity": "成交量",
                                     "price": "限價",
                                     "avg_fill_price": "成交均價",
                                     "狀態": st.column_config.TextColumn("狀態"),
                                     "strategy": "策略",
                                     "未實現損益": st.column_config.TextColumn("未實現損益"),
                                     "current_price": "目前價",
                                 })

                    # Summary stats
                    total = len(df_orders)
                    filled = len(df_orders[df_orders["status"] == "filled"]) if "status" in df_orders.columns else 0
                    pending = len(df_orders[df_orders["status"].isin(["submitted", "pending_submit", "pre_submitted"])]) if "status" in df_orders.columns else 0
                    cancelled = len(df_orders[df_orders["status"] == "cancelled"]) if "status" in df_orders.columns else 0

                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("總委託單", total)
                    c2.metric("✅ 已成交", filled)
                    c3.metric("⏳ 排隊中", pending)
                    c4.metric("🚫 已取消/退單", cancelled)
            else:
                st.info("今日尚無委託單記錄")
        else:
            st.info("委託單檔案尚未建立 (Order Lifecycle 未啟用)")

# ════════════════════════════════════════
# Tab 3: 選擇權
# ════════════════════════════════════════
with tab_options:
    st.header(f"選擇權 TXO ({mode_badge(o_live)})")
    o_df = load_options_indicators(full_history=cont_mode)
    if o_df is not None and not o_df.empty:
        # Debug info (expander)
        with st.expander("🛠️ 數據狀態 (Debug)"):
            st.write(f"資料筆數: {len(o_df)}")
            st.write(f"時間範圍: {o_df['timestamp'].min()} ~ {o_df['timestamp'].max()}")
            if "price_mtx" in o_df.columns:
                st.write(f"MTX 範圍: {o_df['price_mtx'].min():.0f} ~ {o_df['price_mtx'].max():.0f}")
            
            # GSD: Show which files were loaded
            import glob
            st.write("**載入檔案列表:**")
            for sub in ["live_trading", "paper_trading"]:
                pattern = str(OPTIONS_REPO / "logs" / sub / "OPTIONS_*_indicators.csv")
                files = sorted(glob.glob(pattern), reverse=True)[:3]
                for f in files:
                    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime('%Y-%m-%d %H:%M:%S')
                    st.text(f"{os.path.basename(f)} (修改: {mtime})")
        
        if "price_mtx" in o_df.columns:
            last = o_df.iloc[-1]
        
        # 噴發偏向 (Bias) 計算 - 整合選擇權趨勢感
        trend_val = last.get("mid_trend", "")
        bull = (trend_val == "BULL")
        bear = (trend_val == "BEAR")
        mom = last.get("momentum", last.get("mom_mtx", 0))
        mom_prev = o_df["momentum"].iloc[-2] if "momentum" in o_df.columns and len(o_df) > 1 else 0
        
        bias = "⚪中性"
        if mom > 0:
            if bull:
                bias = "🚀強勢多" if mom >= mom_prev else "↗️多轉弱"
            else:
                bias = "⚠️空反彈" if mom >= mom_prev else "↗️弱反彈"
        elif mom < 0:
            if bear:
                bias = "💀強勢空" if mom <= mom_prev else "↘️空轉弱"
            else:
                bias = "⚠️多拉回" if mom <= mom_prev else "↘️弱拉回"

        oc1, oc2, oc3, oc4, oc5, oc6 = st.columns(6)
        # Robust coercion to scalar for display
        mtx_val = _to_num(last.get('price_mtx', 0))
        sc_val = _to_num(last.get('score', 0))

        oc1.metric("MTX", f"{mtx_val:.0f}")
        oc2.metric("Score", f"{sc_val:.1f}")
        trend_label = "🟢BULL" if trend_val == "BULL" else ("🔴BEAR" if trend_val == "BEAR" else "⚪ —")
        oc3.metric("趨勢", trend_label)
        iv = last.get("iv", 0)
        oc4.metric("IV", f"{iv*100:.1f}%" if iv and iv < 1 else f"{iv:.1f}%")
        oc5.metric("Sqz狀態", "🔒壓縮" if last.get("sqz_on", False) is True else "🔓釋放")
        oc6.metric("噴發向", bias)

        if "fired" in last and last.get("fired", False) is True:
            st.success("🔥 **FIRE — 壓縮釋放！**")

        current_spot = float(last.get("price_mtx", 0) or 0)
        current_iv = float(last.get("iv", 0) or 0)
        current_dte_years = float(last.get("dte", 0) or 0) / 365.0 if last.get("dte", 0) else 0.0

        # 顯示當前選擇權持倉
        ol = load_options_ledger()
        if ol is not None and not ol.empty:
            open_option = find_latest_open_options_position(ol)
            if open_option is not None:
                side = str(open_option.side)
                action = str(open_option.action)
                note = str(open_option.note)
                if "iron_condor" in note.lower():
                    pos_label = "🦅 Iron Condor"
                    if "[" in note:
                        pos_label += " " + note.split("[")[1].split("]")[0]
                elif side.upper() == "C":
                    pos_label = "📞 Call"
                elif side.upper() == "P":
                    pos_label = "📉 Put"
                else:
                    pos_label = side
                st.caption(f"當前持倉: **{pos_label}** | 進場: {action} @ {open_option.entry_price:.2f}")

                theta_estimate = None
                if "THETA" in action and current_spot > 0 and current_iv > 0 and current_dte_years > 0:
                    theta_estimate = estimate_theta_unrealized(
                        open_option.note,
                        current_spot=current_spot,
                        current_iv=current_iv,
                        dte_years=current_dte_years,
                        quantity=open_option.quantity,
                    )

                if theta_estimate is not None:
                    ocu1, ocu2, ocu3 = st.columns(3)
                    ocu1.metric("成交成本", f"{theta_estimate['cost_basis']:,.0f} TWD")
                    ocu2.metric("未實現損益", f"{theta_estimate['unrealized_pnl']:+,.0f} TWD")
                    ocu3.metric("目前部位價值", f"{theta_estimate['current_value']*50*theta_estimate['quantity']:,.0f} TWD")
                    st.caption(
                        f"估算: spot {current_spot:.0f} | IV {current_iv:.3f} | DTE {current_dte_years*365:.1f} | "
                        f"信用金 {theta_estimate['entry_credit']:.1f} | 策略 {theta_estimate['strategy']}"
                    )
                elif open_option.entry_price > 0:
                    st.metric("成交成本", f"{open_option.cost_basis:,.0f} TWD")
                    st.caption(f"目前 MTX: {current_spot:.0f}")
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
        st.header("交易記錄 (Round-Trip)")
        round_trips = format_options_trades(ol)
        if round_trips is not None and not round_trips.empty and "#" in round_trips.columns:
            # Style with color for profit/loss
            def style_trades(row):
                styles = [''] * len(row)
                pnl = row.get("淨利", "-")
                if pnl != "-" and isinstance(pnl, (int, float)):
                    color = '#dcfce7' if pnl > 0 else ('#fef2f2' if pnl < 0 else '')
                    styles = [f'background-color: {color}; font-weight: bold'] * len(row)
                return styles
            st.dataframe(round_trips.style.apply(style_trades, axis=1), use_container_width=True, hide_index=True)
        else:
            st.dataframe(ol.tail(30), use_container_width=True)
        opnl = calc_options_pnl(ol)
        fig = make_pnl_chart(opnl, "選擇權累計 PnL (TWD)")
        if fig:
            st.plotly_chart(fig, use_container_width=True)

        # 展開原始 Ledger (進階)
        with st.expander("📋 原始 Ledger (進階)"):
            st.dataframe(ol, use_container_width=True)

    # ── Options Order Status Panel ──
    with st.expander("📤 選擇權委託單狀態 (Order Lifecycle)", expanded=False):
        orders_path = BASE / "exports" / "trades"
        order_files = list(orders_path.glob("OPTIONS_*_orders.json"))
        order_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        if order_files and order_files[0].exists():
            with open(order_files[0], "r", encoding="utf-8") as f:
                orders_data = json.load(f)
            orders_rebuilt_from_ledger = False
            if not orders_data:
                rebuilt_orders = rebuild_options_orders_from_ledger(ol)
                if rebuilt_orders:
                    orders_data = rebuilt_orders
                    orders_rebuilt_from_ledger = True

            if orders_data:
                # [GSD Fix] Get ACTUAL live premium from indicator data, not ledger
                opt_df = load_options_indicators()
                live_premium = None
                current_spot = 0.0
                current_iv = 0.0
                current_dte_years = 0.0
                if opt_df is not None and not opt_df.empty:
                    last_row = opt_df.iloc[-1]
                    current_spot = float(last_row.get("price_mtx", 0) or 0)
                    current_iv = float(last_row.get("iv", 0) or 0)
                    current_dte_years = float(last_row.get("dte", 0) or 0) / 365.0 if last_row.get("dte", 0) else 0.0
                    # Try to get bid/ask mid if available (some versions log it)
                    bid = float(last_row.get("bid", 0))
                    ask = float(last_row.get("ask", 0))
                    if bid > 0 and ask > 0:
                        live_premium = (bid + ask) / 2
                    else:
                        live_premium = float(last_row.get("Close", last_row.get("close", 0)))

                df_orders = pd.DataFrame(orders_data)
                open_option_for_orders = find_latest_open_options_position(ol)
                truth_results = df_orders.apply(
                    lambda row: describe_options_order_truth(
                        row,
                        orders_rebuilt_from_ledger=orders_rebuilt_from_ledger,
                    ),
                    axis=1,
                )
                df_orders["truth_source"] = truth_results.apply(lambda result: result["truth_source"])
                df_orders["真實來源"] = truth_results.apply(lambda result: result["badge"])
                df_orders["degraded_caption"] = truth_results.apply(lambda result: result["degraded_caption"])
                df_orders["show_paper_disclaimer"] = truth_results.apply(lambda result: result["show_paper_disclaimer"])
                df_orders["組合腿摘要"] = df_orders.get("combo_legs", pd.Series([None] * len(df_orders))).apply(summarize_combo_legs)
                display_cols = []
                if "order_id" in df_orders.columns:
                    display_cols.append("order_id")
                if "created_at" in df_orders.columns:
                    display_cols.append("created_at")
                display_cols.append("真實來源")
                if "side" in df_orders.columns:
                    df_orders["方向"] = df_orders["side"].map({"buy": "買入", "sell": "賣出"})
                    display_cols.append("方向")
                if "order_type" in df_orders.columns:
                    type_map_opt = {"market": "市價", "limit": "限價", "stop": "停損", "stop_limit": "停損限價"}
                    df_orders["委託類型"] = df_orders["order_type"].map(type_map_opt).fillna(df_orders["order_type"])
                    display_cols.append("委託類型")
                if "quantity" in df_orders.columns:
                    display_cols.append("quantity")
                if "filled_quantity" in df_orders.columns:
                    display_cols.append("filled_quantity")
                if "price" in df_orders.columns:
                    display_cols.append("price")
                if "avg_fill_price" in df_orders.columns:
                    display_cols.append("avg_fill_price")
                if "status" in df_orders.columns:
                    status_map_opt = {"pending_submit": "⏳ 待傳送", "pre_submitted": "📅 預約單", "submitted": "📨 已委託", "partial_filled": "⚡ 部分成交", "filled": "✅ 完全成交", "cancelled": "🚫 已取消", "rejected": "❌ 已退單", "expired": "⏰ 已過期"}
                    df_orders["狀態"] = df_orders["status"].map(status_map_opt).fillna(df_orders["status"])
                    display_cols.append("狀態")
                if "strategy" in df_orders.columns:
                    display_cols.append("strategy")
                display_cols.append("組合腿摘要")
                display_cols.append("degraded_caption")

                has_open_theta = open_option_for_orders is not None and "THETA" in str(open_option_for_orders.action).upper()

                if (live_premium and live_premium > 0) or has_open_theta or (current_spot > 0 and current_iv > 0 and current_dte_years > 0):
                    # 添加更新按鈕
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.subheader("📊 未實現損益計算")
                    with col2:
                        if st.button("🔄 更新", key="update_options_unrealized"):
                            st.cache_data.clear()
                            st.rerun()

                    if bool(df_orders["show_paper_disclaimer"].any()):
                        st.info("ℹ️ THETA 策略目前顯示的是策略整體估值／紙上生命週期紀錄，不是券商逐腿即時成交回報。")
                    if "broker_combo" in set(df_orders["truth_source"].astype(str)):
                        st.caption("broker_combo 為券商複式單真實來源；paper_strategy / ledger_rebuilt 會保留降級或紙上估值說明。")

                    pricing_results = df_orders.apply(
                        lambda row: estimate_options_order_unrealized(
                            row,
                            open_option_for_orders,
                            live_premium=live_premium or 0.0,
                            current_spot=current_spot,
                            current_iv=current_iv,
                            dte_years=current_dte_years,
                        ),
                        axis=1,
                    )
                    df_orders["unrealized_pnl"] = pricing_results.apply(
                        lambda result: None if result is None else result["unrealized_pnl"]
                    )

                    def _format_unreal(x):
                        if x is None or (isinstance(x, float) and pd.isna(x)):
                            return "—"
                        elif x > 0:
                            return f"🟢 {x:+,.2f}"
                        elif x < 0:
                            return f"🔴 {x:+,.2f}"
                        else:
                            return "⚪ 0"
                    df_orders["未實現損益"] = df_orders["unrealized_pnl"].apply(_format_unreal)
                    display_cols.append("未實現損益")
                    df_orders["current_price"] = pricing_results.apply(
                        lambda result: None if result is None else result["current_price"]
                    )
                    df_orders["目前組合價值"] = df_orders["current_price"]
                    display_cols.append("目前組合價值")

                if display_cols:
                    st.dataframe(df_orders[display_cols], use_container_width=True, hide_index=True,
                                 column_config={
                                      "order_id": "委託單ID",
                                      "created_at": "建立時間",
                                      "真實來源": st.column_config.TextColumn("真實來源"),
                                       "方向": "方向",
                                       "委託類型": st.column_config.TextColumn("委託類型"),
                                       "quantity": "委託量",
                                       "filled_quantity": "成交量",
                                       "price": st.column_config.NumberColumn("限價", format="%.2f"),
                                       "avg_fill_price": st.column_config.NumberColumn("成交均價", format="%.2f"),
                                       "狀態": st.column_config.TextColumn("狀態"),
                                       "strategy": "策略",
                                       "組合腿摘要": st.column_config.TextColumn("組合腿摘要"),
                                       "degraded_caption": st.column_config.TextColumn("狀態說明"),
                                       "未實現損益": st.column_config.TextColumn("未實現損益"),
                                       "目前組合價值": st.column_config.NumberColumn("目前組合價值", format="%.2f"),
                                    })
                    if orders_rebuilt_from_ledger:
                        st.caption("委託單檔案為空，已暫時從交易 ledger 重建今日選擇權委託單狀態。真實來源已標示為 ledger_rebuilt，表示 broker truth 目前不可用。")

                    total = len(df_orders)
                    filled = len(df_orders[df_orders["status"] == "filled"]) if "status" in df_orders.columns else 0
                    pending = len(df_orders[df_orders["status"].isin(["submitted", "pending_submit", "pre_submitted"])]) if "status" in df_orders.columns else 0
                    c1, c2, c3 = st.columns(3)
                    c1.metric("總委託單", total)
                    c2.metric("✅ 已成交", filled)
                    c3.metric("⏳ 排隊中", pending)
            else:
                st.info("今日尚無選擇權委託單記錄")
        else:
            st.info("選擇權委託單檔案尚未建立 (Order Lifecycle 未啟用)")

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
                close = float(last.get('close', last.get('Close', 0)))
                # 處理 volume 值，避免 NaN 錯誤
                vol_val = last.get('volume', last.get('Volume', 0))
                if pd.isna(vol_val):
                    vol = 0
                else:
                    vol = int(vol_val)
                bb_lower = float(last.get('bb_lower', 0))
                bb_upper = float(last.get('bb_upper', 0))
                sqz = "🔒 壓縮" if last.get("sqz_on", False) else "🔓 釋放"
                momentum = float(last.get("momentum", 0) or 0)
                mom_state = int(last.get("mom_state", 1) or 1)
                price_vs_vwap = float(last.get("price_vs_vwap", 0) or 0) * 100
                z_vwap = float(last.get("z_vwap", 0) or 0)
                money_flow_multiplier = float(last.get("money_flow_multiplier", 0) or 0)
                adx = float(last.get("adx", 0) or 0)

                if mom_state == 3:
                    momentum_label = f"🟢 增強 {momentum:.2f}"
                elif mom_state == 2:
                    momentum_label = f"🟡 轉弱 {momentum:.2f}"
                elif mom_state == 1:
                    momentum_label = f"🟠 回升 {momentum:.2f}"
                else:
                    momentum_label = f"🔴 走弱 {momentum:.2f}"

                if bool(last.get("bullish_align", False)):
                    trend_label = "🟢 多頭"
                elif bool(last.get("bearish_align", False)):
                    trend_label = "🔴 空頭"
                else:
                    trend_label = "⚪ 中性"

                if z_vwap >= 2:
                    vwap_sigma_label = f"🔥 +{z_vwap:.1f}σ"
                elif z_vwap <= -2:
                    vwap_sigma_label = f"🧊 {z_vwap:.1f}σ"
                elif z_vwap >= 1:
                    vwap_sigma_label = f"⚠️ +{z_vwap:.1f}σ"
                elif z_vwap <= -1:
                    vwap_sigma_label = f"⚠️ {z_vwap:.1f}σ"
                else:
                    vwap_sigma_label = f"{z_vwap:+.1f}σ"

                if money_flow_multiplier >= 0.35:
                    flow_label = f"🟢 偏買 {money_flow_multiplier:+.2f}"
                elif money_flow_multiplier <= -0.35:
                    flow_label = f"🔴 偏賣 {money_flow_multiplier:+.2f}"
                else:
                    flow_label = f"⚪ 中性 {money_flow_multiplier:+.2f}"
                
                # 計算布林帶位置：下軌=0%、中間約=50%、上軌=100%
                if (
                    not pd.isna(bb_lower)
                    and not pd.isna(bb_upper)
                    and bb_upper > bb_lower
                    and not pd.isna(close)
                    and close > 0
                ):
                    band_position = ((close - bb_lower) / (bb_upper - bb_lower)) * 100
                    if band_position < 0:
                        dist_label = f"🔥 下軌下方 {band_position:.1f}%"
                    elif band_position > 100:
                        dist_label = f"🚀 上軌上方 {band_position:.1f}%"
                    else:
                        dist_label = f"{band_position:.1f}%"
                else:
                    dist_label = "—%"

                monitor_data.append({
                    "代號": ticker,
                    "名稱": last.get("name", "Unknown"),
                    "股價": f"{close:.2f}",
                    "成交量": f"{vol:,}",
                    "動能": momentum_label,
                    "VWAP偏離": f"{price_vs_vwap:+.2f}%",
                    "VWAPσ": vwap_sigma_label,
                    "資金壓力": flow_label,
                    "趨勢": trend_label,
                    "ADX": f"{adx:.1f}",
                    "布林帶位置": dist_label,
                    "壓縮": sqz,
                })
        
        if monitor_data:
            m_df = pd.DataFrame(monitor_data)
            
            def style_monitor(row):
                styles = [''] * len(row)
                col_idx = {name: idx for idx, name in enumerate(m_df.columns)}
                # 壓縮 (紅底白字)
                if "🔒" in str(row.get("壓縮", "")):
                    styles[col_idx["壓縮"]] = 'background-color: #fee2e2; color: #b91c1c; font-weight: bold'
                # 布林帶位置：跌破下軌標綠、突破上軌標橘
                if "🔥" in str(row.get("布林帶位置", "")):
                    styles[col_idx["布林帶位置"]] = 'background-color: #dcfce7; color: #065f46; font-weight: bold'
                elif "🚀" in str(row.get("布林帶位置", "")):
                    styles[col_idx["布林帶位置"]] = 'background-color: #ffedd5; color: #c2410c; font-weight: bold'
                if "🔥" in str(row.get("VWAPσ", "")) or "🧊" in str(row.get("VWAPσ", "")):
                    styles[col_idx["VWAPσ"]] = 'background-color: #fef3c7; color: #92400e; font-weight: bold'
                if "🟢" in str(row.get("資金壓力", "")):
                    styles[col_idx["資金壓力"]] = 'background-color: #dcfce7; color: #065f46; font-weight: bold'
                elif "🔴" in str(row.get("資金壓力", "")):
                    styles[col_idx["資金壓力"]] = 'background-color: #fee2e2; color: #b91c1c; font-weight: bold'
                if "🟢" in str(row.get("動能", "")):
                    styles[col_idx["動能"]] = 'background-color: #dcfce7; color: #065f46; font-weight: bold'
                elif "🔴" in str(row.get("動能", "")):
                    styles[col_idx["動能"]] = 'background-color: #fee2e2; color: #b91c1c; font-weight: bold'
                return styles

            st.dataframe(m_df.style.apply(style_monitor, axis=1), use_container_width=True, hide_index=True)
        else:
            st.info("等待 Monitor 寫入指標數據...")
    
    st.divider()
    # 讀取目前運行的模式
    current_mode = "LIVE" if s_live else "PAPER"
    sl = load_stock_trades(current_mode)
    
    st.header(f"交易記錄 (Round-Trip, {current_mode} 模式)")
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
            m2.metric("勝率", f"{wins}/{total} ({wins/total*100:.0f}%)" if total > 0 else "-")
            m3.metric("摩擦成本", f"{total_fees:,.0f} TWD")
            m4.metric("平均每筆", f"{total_pnl/total:+,.0f} TWD" if total > 0 else "-")

        # Round-trip 明細
        round_trips = format_stock_trades(sl)
        if round_trips is not None and not round_trips.empty and "#" in round_trips.columns:
            # --- Unrealized PnL for open stock positions ---
            open_rows = round_trips[round_trips["出場時間"] == "⏳ 持倉中"]
            if not open_rows.empty:
                st.subheader("📊 未實現損益 (持倉中)")
                cols = st.columns(min(len(open_rows), 4))
                for idx, (_, row) in enumerate(open_rows.iterrows()):
                    ticker = str(row.get("代號", "")).split()[0]  # Extract ticker from "1525 綠電"
                    entry = float(row.get("進場價", 0))
                    qty = int(row.get("股數", 0))
                    cur_price = latest_indicator_close(load_stock_indicators(ticker))
                    if cur_price > 0 and entry > 0 and qty > 0:
                        unrealized = (cur_price - entry) * qty
                        color = "green" if unrealized >= 0 else "red"
                        with cols[idx % 4]:
                            st.metric(f"{row.get('代號', ticker)}", f"{unrealized:+,.0f} TWD",
                                      delta=f"{unrealized:+,.0f} ({(unrealized/(entry*qty)*100):+.1f}%)")
                            st.caption(f"進場: {entry:.0f} | 目前: {cur_price:.0f} | {qty}股")

            def style_stock_trades(row):
                pnl = row.get("淨利", "-")
                if pnl != "-" and isinstance(pnl, (int, float)):
                    color = '#dcfce7' if pnl > 0 else ('#fef2f2' if pnl < 0 else '')
                    return [f'background-color: {color}; font-weight: bold'] * len(row)
                return [''] * len(row)
            st.dataframe(round_trips.style.apply(style_stock_trades, axis=1), use_container_width=True, hide_index=True)
        else:
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

        with st.expander("📋 原始 Ledger (進階)"):
            st.dataframe(sl, use_container_width=True)
    else:
        st.info(f"今日尚無 {current_mode} 交易紀錄")

    # ── Stock Order Status Panel ──
    with st.expander("📤 台股委託單狀態 (Order Lifecycle)", expanded=False):
        orders_data = load_stock_orders(current_mode)

        if orders_data:
            # 添加更新按鈕
            col1, col2 = st.columns([3, 1])
            with col1:
                st.write("**📊 未實現損益計算**")
            with col2:
                if st.button("🔄 更新", key="update_stock_unrealized"):
                    st.cache_data.clear()
                    st.rerun()
            
            # Get LIVE price from the same data source as charts
            live_prices = {}
            for ticker in watchlist:
                s_df = load_stock_indicators(ticker)
                if s_df is not None and not s_df.empty:
                    last = s_df.iloc[-1]
                    close = float(last.get('close', last.get('Close', 0)))
                    live_prices[ticker] = close

            # Process orders for display
            order_rows = []
            for order in orders_data:
                ticker = order.get("ticker", "")
                status = order.get("status", "")
                side = order.get("side", "")
                qty = order.get("qty", 0)
                price = order.get("price", 0.0)
                order_id = order.get("order_id", "")
                timestamp = order.get("timestamp", "")
                filled_qty = order.get("filled_qty", 0)
                filled_price = order.get("filled_price", 0.0)
                order_type = order.get("order_type", "LMT")  # Default to LMT
                
                # Map order type to Chinese
                order_type_map = {
                    "LMT": "限價單",
                    "MKT": "市價單",
                    "MKT_RANGE": "範圍市價單"
                }
                order_type_display = order_type_map.get(order_type, order_type)
                
                # Calculate unrealized PnL for open orders
                unrealized = 0.0
                if status == "OPEN" and ticker in live_prices:
                    current_price = live_prices[ticker]
                    if side == "BUY":
                        unrealized = (current_price - price) * qty
                    elif side == "SELL":
                        unrealized = (price - current_price) * qty
                
                order_rows.append({
                    "委託單號": order_id,
                    "股票代號": ticker,
                    "買賣": side,
                    "委託類型": order_type_display,
                    "狀態": status,
                    "委託數量": qty,
                    "委託價格": price,
                    "已成交數量": filled_qty,
                    "成交均價": filled_price,
                    "未實現損益": f"{unrealized:+,.0f}" if unrealized != 0 else "—",
                    "時間": timestamp
                })
            
            if order_rows:
                orders_df = pd.DataFrame(order_rows)
                
                # Style function for order table
                def style_orders(row):
                    styles = [''] * len(row)
                    # Status colors
                    status = row.get("狀態", "")
                    if status == "FILLED":
                        styles[3] = 'background-color: #dcfce7; color: #065f46; font-weight: bold'
                    elif status == "OPEN":
                        styles[3] = 'background-color: #fef9c3; color: #854d0e; font-weight: bold'
                    elif status == "CANCELLED":
                        styles[3] = 'background-color: #f3f4f6; color: #6b7280; font-weight: bold'
                    elif status == "REJECTED":
                        styles[3] = 'background-color: #fee2e2; color: #b91c1c; font-weight: bold'
                    
                    # Side colors
                    side = row.get("買賣", "")
                    if side == "BUY":
                        styles[2] = 'background-color: #dbeafe; color: #1e40af; font-weight: bold'
                    elif side == "SELL":
                        styles[2] = 'background-color: #fce7f3; color: #9d174d; font-weight: bold'
                    
                    # Unrealized PnL colors
                    unrealized = row.get("未實現損益", "")
                    if "+" in str(unrealized):
                        styles[8] = 'background-color: #dcfce7; color: #065f46; font-weight: bold'
                    elif "-" in str(unrealized):
                        styles[8] = 'background-color: #fee2e2; color: #b91c1c; font-weight: bold'
                    
                    return styles
                
                st.dataframe(orders_df.style.apply(style_orders, axis=1), use_container_width=True, hide_index=True)
                
                # Summary metrics
                open_orders = [o for o in orders_data if o.get("status") == "OPEN"]
                filled_orders = [o for o in orders_data if o.get("status") == "FILLED"]
                cancelled_orders = [o for o in orders_data if o.get("status") == "CANCELLED"]
                
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("總委託單數", len(orders_data))
                col2.metric("待成交", len(open_orders))
                col3.metric("已成交", len(filled_orders))
                col4.metric("已取消", len(cancelled_orders))
            else:
                st.info("委託單列表為空")
        else:
            st.info(f"今日尚無 {current_mode} 台股委託單記錄")

# ════════════════════════════════════════
# Tab 5: 策略管道 (Pipeline)
# ════════════════════════════════════════
with tab_pipeline:
    st.header("📊 策略管道 (Strategy Pipeline)")

    # Strategy Rankings
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("☀️ 日盤排行榜")
        try:
            from core.strategy_registry import get_strategy_ranking
            day_ranking = get_strategy_ranking("day")
            for i, (name, pf) in enumerate(day_ranking, 1):
                emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "  "
                st.write(f"{emoji} {i}. {name} (PF={pf:.1f})")
        except Exception as e:
            st.error(f"Error: {e}")

    with col2:
        st.subheader("🌙 夜盤排行榜")
        try:
            night_ranking = get_strategy_ranking("night")
            for i, (name, pf) in enumerate(night_ranking, 1):
                emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "  "
                st.write(f"{emoji} {i}. {name} (PF={pf:.1f})")
        except Exception as e:
            st.error(f"Error: {e}")

    # Pipeline Status
    st.subheader("🔄 管道狀態")
    try:
        from core.strategy_registry import STRATEGY_PERF
        pipeline_data = [
            {"策略": "Counter-VWAP", "日盤 PF": STRATEGY_PERF["counter_vwap"]["day_pf"], "夜盤 PF": STRATEGY_PERF["counter_vwap"]["night_pf"], "狀態": "✅ Paper"},
            {"策略": "Spring-Upthrust", "日盤 PF": STRATEGY_PERF["spring_upthrust"]["day_pf"], "夜盤 PF": STRATEGY_PERF["spring_upthrust"]["night_pf"], "狀態": "⏳ 回測驗證"},
            {"策略": "Vol-Squeeze", "日盤 PF": STRATEGY_PERF["vol_squeeze"]["day_pf"], "夜盤 PF": STRATEGY_PERF["vol_squeeze"]["night_pf"], "狀態": "⏳ 觀察中"},
            {"策略": "PSAR", "日盤 PF": STRATEGY_PERF["psar"]["day_pf"], "夜盤 PF": STRATEGY_PERF["psar"]["night_pf"], "狀態": "🔴 夜盤 PF<1.0"},
        ]
        st.table(pd.DataFrame(pipeline_data))
    except Exception as e:
        st.error(f"Error loading pipeline: {e}")

    # Circuit Breaker Status
    st.subheader("🛡️ Circuit Breaker 狀態")
    try:
        from core.circuit_breaker import CircuitBreaker
        day_cb = CircuitBreaker(session="day")
        night_cb = CircuitBreaker(session="night")
        c1, c2 = st.columns(2)
        with c1:
            st.write(f"**日盤**: {day_cb.state.session_pnl:.0f} pts, {day_cb.state.consecutive_losses} 連虧, {'🛑 HALTED' if day_cb.is_halted else '✅ OK'}")
        with c2:
            st.write(f"**夜盤**: {night_cb.state.session_pnl:.0f} pts, {night_cb.state.consecutive_losses} 連虧, {'🛑 HALTED' if night_cb.is_halted else '✅ OK'}")
    except Exception as e:
        st.error(f"Error: {e}")

    # Recent Decisions
    st.subheader("📝 最近決策日誌")
    try:
        from core.decision_logger import DecisionLogger
        recent = DecisionLogger.read_decisions(limit=10)
        if recent:
            df_dec = pd.DataFrame([{
                "時間": d.timestamp[:19],
                "類型": d.type,
                "Session": d.session,
                "動作": d.action,
                "細節": d.detail[:50],
            } for d in recent])
            st.dataframe(df_dec, use_container_width=True)
        else:
            st.info("尚無決策記錄")
    except Exception as e:
        st.error(f"Error: {e}")

    # ── Hourly Audit Timeline ──
    st.subheader("🕐 每小時審計時間軸")
    try:
        from pathlib import Path
        audit_dir = Path("logs/market_data")
        today_str = datetime.datetime.now().strftime("%Y%m%d")
        audit_file = audit_dir / f"TMF_{today_str}_signals_audit.csv"

        if audit_file.exists():
            try:
                df_audit = pd.read_csv(audit_file, on_bad_lines='skip')
            except Exception:
                df_audit = pd.DataFrame()

            if df_audit is not None and not df_audit.empty and "signal" in df_audit.columns:
                df_hourly = df_audit[df_audit["signal"] == "HOURLY_AUDIT"]

                if not df_hourly.empty:
                    verdict_map = {
                        "NORMAL": "✅",
                        "COOLDOWN": "🔵",
                        "NO_VALID_SIGNALS": "⚠️",
                        "DATA_FAILURE": "🚨",
                    }
                    df_display = pd.DataFrame({
                        "時間": df_hourly["timestamp"].apply(lambda x: str(x)[-8:]),
                        "Verdict": df_hourly["reason"].map(lambda r: verdict_map.get(r, "❓")),
                        "細節": df_hourly["rejection"].apply(lambda x: str(x)[:60] if pd.notna(x) else ""),
                    })
                    st.dataframe(df_display, use_container_width=True, hide_index=True)

                    verdict_counts = df_hourly["reason"].value_counts()
                    cols = st.columns(min(4, len(verdict_counts)))
                    for i, (verdict, count) in enumerate(verdict_counts.items()):
                        with cols[i % 4]:
                            emoji = verdict_map.get(verdict, "❓")
                            st.metric(f"{emoji} {verdict}", f"{count} 次")
                else:
                    st.info("今日尚無審計記錄（monitor 尚未運行或未到整點）")
        else:
            st.info(f"今日審計檔不存在：{audit_file.name}")
    except Exception as e:
        st.error(f"審計讀取錯誤: {e}")

    # ── Router Trace Dashboard ──
    st.subheader("🔍 Router Trace — 每根 Bar 的策略決策")
    try:
        from pathlib import Path
        import json
        trace_dir = Path("logs/router_trace")
        if trace_dir.exists():
            trace_files = sorted(trace_dir.glob("router_trace_*.jsonl"), reverse=True)
            if trace_files:
                # ── Load + explode ──
                rows = []
                with open(trace_files[0]) as f:
                    for line in f:
                        trace = json.loads(line)
                        ts = trace.get("ts", "?")
                        regime = trace.get("regime", "?")
                        selected = trace.get("selected")
                        for s in trace.get("strategies", []):
                            rows.append({
                                "ts": ts,
                                "regime": regime,
                                "selected": selected,
                                "strategy": s["name"],
                                "triggered": s.get("triggered", False),
                                "edge": s.get("edge_score"),
                                "reason": s.get("skip_reason", "?") if not s.get("triggered") else "✅ TRADE",
                            })
                if rows:
                    df_rt = pd.DataFrame(rows)
                    df_rt["ts_dt"] = pd.to_datetime(df_rt["ts"], errors="coerce")
                    df_rt = df_rt.sort_values("ts_dt")

                    # ── ① 最新狀態 ──
                    latest = df_rt.sort_values("ts_dt").groupby("strategy").tail(1)
                    cols = st.columns(len(latest))
                    for ci, (_, r) in enumerate(latest.iterrows()):
                        c = cols[ci % len(cols)]
                        emoji = "✅" if r["triggered"] else ("⏳" if r["reason"] in ("WATCHING", "FIRE_DETECTED_WAITING") else "⛔")
                        c.metric(f"{r['strategy']}", f"{emoji} {r['reason']}", f"edge={r['edge']}" if pd.notna(r["edge"]) else None)

                    # ── ② Edge Timeline (Plotly) ──
                    try:
                        import plotly.express as px
                        df_plot = df_rt[df_rt["edge"].notna()].copy()
                        if not df_plot.empty:
                            fig = px.line(df_plot, x="ts_dt", y="edge", color="strategy",
                                          title="Edge Score Timeline",
                                          labels={"ts_dt": "時間", "edge": "Edge", "strategy": "策略"})
                            fig.update_layout(height=250, margin=dict(l=10, r=10, t=30, b=10))
                            st.plotly_chart(fig, use_container_width=True)
                    except Exception:
                        pass

                    # ── ③ Skip Reason 分布 ──
                    reason_counts = df_rt.groupby(["strategy", "reason"]).size().unstack(fill_value=0)
                    if not reason_counts.empty:
                        st.bar_chart(reason_counts, height=200)

                    # ── ④ 原始資料（摺疊） ──
                    with st.expander("📋 原始 Router Trace 資料", expanded=False):
                        st.dataframe(df_rt[["ts", "regime", "strategy", "triggered", "edge", "reason"]].tail(50),
                                     use_container_width=True, hide_index=True)
                else:
                    st.info("Router trace 檔案為空")
            else:
                st.info("今日尚無 router trace 資料（monitor 尚未運行）")
        else:
            st.info("Router trace 目錄不存在")
    except Exception as e:
        st.error(f"Router trace 載入錯誤: {e}")

# ════════════════════════════════════════
# Tab 6: 設定
# ════════════════════════════════════════
with tab_settings:
    st.header("⚙️ 系統設定")

    # ── 0. 實盤就緒度檢查 ──
    with st.expander("🚀 實盤就緒度檢查", expanded=True):
        from core.live_readiness import check_all, get_readiness_items, get_readiness_summary
        check_output = check_all()
        readiness_items = get_readiness_items(check_output)
        status, passed, total = get_readiness_summary(check_output)

        st.markdown(f"### {status} ({passed}/{total} 項通過)")

        # Progress bar
        pct = passed / total if total > 0 else 0
        st.progress(pct)

        # Detail table
        for r in readiness_items:
            icon = "✅" if r.passed else "❌"
            st.caption(f"{icon} **{r.name}**: {r.detail}")

        # Action recommendation
        if passed == total:
            st.success("🎉 所有檢查通過！可以考慮進入 Phase 2 小額實盤測試")
            st.info("建議: 先用 1 口 TMF 測試 5 個交易日，設定每日最大虧損 2%")
        elif passed >= total * 0.6:
            remaining = total - passed
            st.warning(f"⚠️ 還有 {remaining} 項未通過，建議繼續 Paper 觀察")
            for r in readiness_items:
                if not r.passed:
                    st.caption(f"❌ 待解決: {r.name} (目前: {r.detail})")
        else:
            st.error("❌ 多數檢查未通過，不建議開啟實盤交易")

        st.divider()
        st.caption("參考文件: `docs/LIVE_TRADING_GUIDE.md`")

    # ── 1. 期貨 TMF 設定 ──
    with st.expander("📈 期貨 TMF 設定", expanded=True):
        from core.strategy_registry import StrategyRegistry
        _reg = StrategyRegistry()
        _reg.discover()
        fut_strats = {item["name"]: item for item in _reg.list_all() if item.get("asset_class") == "futures" and item.get("available", False)}
        current_fut_strat = futures_cfg.get("strategy", {}).get("active_strategy", futures_cfg.get("active_strategy", "counter_vwap"))

        # ── Session indicator: show active vs inactive params ──
        _night_cfg = load_yaml(BASE / "config" / "futures_night.yaml")
        _day_cfg = load_yaml(BASE / "config" / "futures.yaml")
        _day_risk = _day_cfg.get("risk_mgmt", {})
        _night_risk = _night_cfg.get("risk_mgmt", {})
        _active_risk = _night_risk if _CURRENT_SESSION_NIGHT else _day_risk
        st.info(
            f"**{'🌙 夜盤設定使用中' if _CURRENT_SESSION_NIGHT else '☀️ 日盤設定使用中'}** (`{FUTURES_CFG_NAME}`)  \n"
            f"停損: **{_active_risk.get('stop_loss_pts', '?')} pts** (日 {_day_risk.get('stop_loss_pts', '?')} / 夜 {_night_risk.get('stop_loss_pts', '?')})  \n"
            f"ATR 倍數: **{_active_risk.get('atr_multiplier', '?')}x** (日 {_day_risk.get('atr_multiplier', '?')} / 夜 {_night_risk.get('atr_multiplier', '?')})  \n"
            f"VWAP 確認: **{_active_risk.get('exit_vwap_confirm_bars', '?')} bars** (日 {_day_risk.get('exit_vwap_confirm_bars', '?')} / 夜 {_night_risk.get('exit_vwap_confirm_bars', '?')})  \n"
            f"追蹤停損距離: **{_active_risk.get('trailing_stop_distance_pts', '?')} pts** (日 {_day_risk.get('trailing_stop_distance_pts', '?')} / 夜 {_night_risk.get('trailing_stop_distance_pts', '?')})"
        )

        with st.form("futures_settings_form"):
            f_live_new = st.checkbox("啟用期貨實盤交易 (LIVE)", value=futures_cfg.get("live_trading", False))

            # Strategy selector from Registry
            strat_options = list(fut_strats.keys())
            try:
                strat_idx = strat_options.index(current_fut_strat)
            except ValueError:
                strat_idx = 0

            f_strat_new = st.selectbox("核心進場策略", strat_options, index=strat_idx)

            # Show strategy metadata
            meta = fut_strats.get(f_strat_new, {})
            desc = meta.get("description", "無說明")
            pf = meta.get("backtest_pf", 0)
            wr = meta.get("backtest_wr", 0)
            maxdd = meta.get("backtest_maxdd", 0)
            regime = meta.get("market_regime", "")
            st.info(f"💡 **{f_strat_new}**: {desc}  \n"
                    f"📊 PF={pf:.2f} | WR={wr:.1f}% | MaxDD={maxdd:.1f}% | 適用: {regime}")

            st.divider()

            # ── 口數與持倉限制 ──
            st.markdown("##### 📦 口數與持倉限制")
            c1, c2 = st.columns(2)
            f_lots = c1.number_input("每筆交易口數", min_value=1, max_value=10,
                                     value=futures_cfg.get("trade_mgmt", {}).get("lots_per_trade", 2),
                                     help="每次進場的口數。實盤建議從 1 口開始。")
            f_max_pos = c2.number_input("最大持倉口數", min_value=1, max_value=20,
                                        value=futures_cfg.get("trade_mgmt", {}).get("max_positions", 2),
                                        help="同時最大持倉口數。建議 1-2 口控制風險。")

            st.divider()
            f_regime = st.selectbox("市場濾網 (Regime)", ["low", "mid", "high"], index=["low", "mid", "high"].index(futures_cfg.get("strategy", {}).get("regime_filter", "mid")))
            
            fc1, fc2 = st.columns(2)
            f_score = fc1.slider("進場門檻 (Score)", 10, 100, value=futures_cfg.get("strategy", {}).get("entry_score", 20))
            f_atr = fc2.slider("ATR 止損倍數", 1.0, 5.0, value=float(futures_cfg.get("risk_mgmt", {}).get("atr_multiplier", 2.0)), step=0.1)

            st.divider()
            st.markdown("##### 🛡️ 進階安全與成本設定")
            fc3, fc4 = st.columns(2)
            f_stop_fixed = fc3.number_input("固定停損點數 (pts)", min_value=10, max_value=200,
                                           value=int(futures_cfg.get("risk_mgmt", {}).get("stop_loss_pts", 60)),
                                           help="ATR 失效或為 0 時使用的保險底線停損點數。")
            f_fee = fc4.number_input("單邊手續費 (TWD)", min_value=0.0, max_value=100.0,
                                    value=float(futures_cfg.get("execution", {}).get("broker_fee_per_side", 20.0)),
                                    help="用於 PnL 計算。請輸入你的真實券商折扣後手續費。")

            if st.form_submit_button("💾 儲存並重啟期貨模組"):
                futures_cfg["live_trading"] = f_live_new
                futures_cfg["strategy"]["active_strategy"] = f_strat_new
                futures_cfg["strategy"]["regime_filter"] = f_regime
                futures_cfg["strategy"]["entry_score"] = f_score
                futures_cfg["risk_mgmt"]["atr_multiplier"] = f_atr
                futures_cfg["risk_mgmt"]["stop_loss_pts"] = f_stop_fixed
                futures_cfg["execution"]["broker_fee_per_side"] = f_fee
                futures_cfg["trade_mgmt"]["lots_per_trade"] = f_lots
                futures_cfg["trade_mgmt"]["max_positions"] = f_max_pos
                save_yaml(FUTURES_CFG_PATH, futures_cfg)
                trigger_restart()
                st.success(f"期貨設定已更新！策略: {f_strat_new} | 口數: {f_lots} | 最大持倉: {f_max_pos}")
                st.rerun()

    # ── 2. 選擇權 TXO 設定 ──
    with st.expander("🔮 選擇權 TXO 設定", expanded=False):
        current_opt_mode = options_cfg.get("active_mode", options_cfg.get("mode", "V2"))
        opt_strategy_cfg = options_cfg.setdefault("strategy", {})
        opt_risk_cfg = options_cfg.setdefault("risk_mgmt", {})
        opt_exec_cfg = options_cfg.setdefault("execution", {})
        opt_pricing_cfg = options_cfg.setdefault("pricing", {})
        opt_monitoring_cfg = options_cfg.setdefault("monitoring", {})
        opt_mode_cfg = options_cfg.setdefault("modes", {}).get(current_opt_mode, {})
        
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

            st.caption(
                "目前 monitor 生效值："
                f" entry_score={int(opt_strategy_cfg.get('entry_score', options_cfg.get('entry_score', 80)))}"
                f" | stop_loss={float(opt_risk_cfg.get('stop_loss_pct', 0.3)):.0%}"
                f" | tp1={float(opt_mode_cfg.get('tp1_pct', 0.0)):.1f}%"
                f" | trailing={float(opt_mode_cfg.get('trailing_stop_pct', 0.0)):.2f}"
                f" | IV={float(opt_pricing_cfg.get('min_iv', options_cfg.get('min_iv', 0.15))):.2f}"
                f"~{float(opt_pricing_cfg.get('max_iv', options_cfg.get('max_iv', 0.60))):.2f}"
                f" | pricing={opt_pricing_cfg.get('pricing_model', 'black_scholes')}"
                f" | order_mgr={'on' if opt_monitoring_cfg.get('use_order_manager', False) else 'off'}"
            )

            o_score = st.slider(
                "進場門檻 (Score)",
                10,
                100,
                value=int(opt_strategy_cfg.get("entry_score", options_cfg.get("entry_score", 80))),
                help="實際對應 strategies/options/live_options_squeeze_monitor.py 讀取的 strategy.entry_score。",
            )

            o_fire_thresh = st.slider("Fire 門檻 (強趨勢 score)", 10, 100,
                                       value=int(options_cfg.get("strategy", {}).get("fire_score_threshold", 80)),
                                       help="fired=False 但 score 超過此值也允許進場。降低此值可在趨勢行情中進場。")

            # ── 口數與持倉限制 ──
            st.markdown("##### 📦 口數與持倉限制")
            oc1, oc2 = st.columns(2)
            o_lots = oc1.number_input("每筆交易口數", min_value=1, max_value=10,
                                     value=opt_risk_cfg.get("lots_per_trade", 2),
                                     help="基礎進場口數。Runtime 仍可能依 Decision Intelligence 做單次縮放，但不應默默改寫這個基礎值。")
            o_max_pos = oc2.number_input("最大持倉口數", min_value=1, max_value=20,
                                        value=opt_risk_cfg.get("max_positions", 2),
                                        help="同時最大持倉口數。建議 1-2 口控制風險。")

            st.divider()
            oc3, oc4 = st.columns(2)
            o_min_iv = oc3.slider(
                "最低 IV 限制",
                0.1,
                0.5,
                value=float(opt_pricing_cfg.get("min_iv", options_cfg.get("min_iv", 0.15))),
                step=0.01,
            )
            o_max_iv = oc4.slider(
                "最高 IV 限制",
                0.3,
                1.0,
                value=float(opt_pricing_cfg.get("max_iv", options_cfg.get("max_iv", 0.60))),
                step=0.01,
            )

            st.divider()
            st.markdown("##### 🛡️ 進階安全與成本設定")
            oc5, oc6 = st.columns(2)
            o_fee = oc5.number_input("單邊手續費 (TWD)", min_value=0.0, max_value=100.0,
                                    value=float(opt_exec_cfg.get("broker_fee_per_side", 20.0)),
                                    help="券商收取的單口單邊手續費。")
            o_exch = oc6.number_input("單邊交易所費 (TWD)", min_value=0.0, max_value=100.0,
                                     value=float(opt_exec_cfg.get("exchange_fee_per_side", 5.0)),
                                     help="期交所收取的單口單邊費用。")

            if st.form_submit_button("💾 儲存並重啟選擇權模組"):
                options_cfg["live_trading"] = o_live_new
                options_cfg["active_mode"] = o_mode_new
                options_cfg["mode"] = o_mode_new
                opt_strategy_cfg["entry_score"] = o_score
                opt_strategy_cfg["fire_score_threshold"] = o_fire_thresh
                opt_pricing_cfg["min_iv"] = o_min_iv
                opt_pricing_cfg["max_iv"] = o_max_iv
                opt_exec_cfg["broker_fee_per_side"] = o_fee
                opt_exec_cfg["exchange_fee_per_side"] = o_exch
                opt_risk_cfg["lots_per_trade"] = o_lots
                opt_risk_cfg["max_positions"] = o_max_pos
                save_yaml(OPTIONS_CFG_PATH, options_cfg)
                trigger_restart()
                st.success(f"選擇權設定已更新！模式: {o_mode_new} | 口數: {o_lots} | 最大持倉: {o_max_pos} | Fire閾值: {o_fire_thresh}")
                st.rerun()

    # ── 3. 台股 Stocks 設定 ──
    with st.expander("🍎 台股 Stocks 設定", expanded=True):
        stk_inner = stock_cfg.get("stocks", {})
        
        # 將同步按鈕與顯示邏輯整合
        if st.button("🔄 同步外部 CANSLIM 領頭羊名單"):
            try:
                import subprocess
                subprocess.run(["python3", "scripts/sync/sync_external_watchlist.py"], check=True, timeout=30)
                st.success("同步成功！已從雲端獲取最新領頭羊名單。")
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
                # GSD Fix: stock_runner is independent process, must be killed separately
                import subprocess
                subprocess.run(["pkill", "-f", "stock_runner.py"], capture_output=True)
                st.success("台股設定已更新，正在重啟系統...")

    # ── 4. 危險區域 ──
    st.divider()
    with st.expander("🚨 危險區域 (Danger Zone)", expanded=False):
        st.warning("以下操作將永久刪除數據，請謹慎執行。")
        col_d1, col_d2 = st.columns([2, 1])
        with col_d1:
            st.markdown("##### 🗑️ 清空模擬交易數據")
            st.caption("這將歸零所有 Paper Trading 的持倉與歷史紀錄 CSV，方便切換實盤前清空數據。")
        with col_d2:
            if st.button("執行清空", type="primary", use_container_width=True):
                try:
                    import subprocess
                    result = subprocess.run(["python3", "scripts/maintenance/clear_simulation_data.py", "--force"], 
                                                         capture_output=True, text=True)
                    st.success("✅ 數據已清空！")
                    st.toast("Simulation data cleared successfully.")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"清空失敗: {e}")

    st.info("💡 提示: 部分進階設定可直接編輯 `config/*.yaml` 檔案。")

# ── Footer and Refresh ──
refresh = 30
time.sleep(refresh)
st.rerun()
