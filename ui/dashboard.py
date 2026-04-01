#!/usr/bin/env python3
"""
tw-trading-unified — 整合儀表板
期貨 + 選擇權雙策略監控

Usage:
    streamlit run ui/dashboard.py --server.port 8500
"""

import streamlit as st
import pandas as pd
import yaml
import datetime
from pathlib import Path

# ── 頁面設定 ──
st.set_page_config(
    page_title="Trading Unified Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

BASE = Path(__file__).parent.parent
DATE_STR = datetime.datetime.now().strftime("%Y%m%d")

# ── Config ──
def load_yaml(path):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {}

futures_cfg = load_yaml(BASE / "config" / "futures.yaml")
options_cfg = load_yaml(BASE / "config" / "options_strategy.yaml")

# ── 資料路徑 ──
# 期貨 — 從原 repo 的 logs 讀取
FUTURES_LOG_DIR = Path.home() / "Documents/mylin102/tw-futures-realtime/logs/market_data"
FUTURES_TRADE_DIR = Path.home() / "Documents/mylin102/tw-futures-realtime/exports/trades"

# 選擇權 — 根據 live_trading 決定
OPTIONS_IS_LIVE = options_cfg.get("live_trading", False)
if OPTIONS_IS_LIVE:
    OPTIONS_DATA_DIR = Path.home() / "Documents/mylin102/tw-option-squeeze-trading/logs/live_trading"
else:
    OPTIONS_DATA_DIR = Path.home() / "Documents/mylin102/tw-option-squeeze-trading/logs/paper_trading"

# ── Sidebar ──
with st.sidebar:
    st.header("⚙️ 設定")
    refresh_rate = st.slider("自動重新整理 (秒)", 5, 60, 15, 5)
    st.divider()

    st.subheader("📋 模式")
    f_mode = "LIVE" if futures_cfg.get("live_trading") else "PAPER"
    o_mode = "LIVE" if OPTIONS_IS_LIVE else "PAPER"
    st.write(f"期貨 TMF: **{f_mode}**")
    st.write(f"選擇權 TXO: **{o_mode}**")

    st.divider()
    st.subheader("選擇權參數")
    st.write(f"Entry Score: ≥ {options_cfg.get('strategy', {}).get('entry_score', '?')}")
    st.write(f"Stop Loss: {options_cfg.get('risk_mgmt', {}).get('stop_loss_pct', '?')}")
    st.write(f"TP1: {options_cfg.get('exit_strategy', {}).get('tp1_pct', '?')}")

    st.divider()
    st.subheader("期貨參數")
    st.write(f"Entry Score: ≥ {futures_cfg.get('strategy', {}).get('entry_score', '?')}")
    st.write(f"Stop Loss: {futures_cfg.get('risk_mgmt', {}).get('stop_loss_pts', '?')} pts")


# ── 資料載入 ──
@st.cache_data(ttl=5)
def load_futures_indicators():
    for prefix in ["TMF"]:
        for suffix in ["_indicators.csv", f"_{DATE_STR}_indicators.csv"]:
            f = FUTURES_LOG_DIR / f"{prefix}_{DATE_STR}{suffix.replace(DATE_STR + '_', '')}"
            if f.exists():
                try:
                    return pd.read_csv(f, parse_dates=["timestamp"])
                except Exception:
                    pass
        # 嘗試 LIVE / PAPER 版本
        for tag in ["LIVE", "PAPER"]:
            f = FUTURES_LOG_DIR / f"{prefix}_{DATE_STR}_{tag}_indicators.csv"
            if f.exists():
                try:
                    return pd.read_csv(f, parse_dates=["timestamp"])
                except Exception:
                    pass
    return None


@st.cache_data(ttl=5)
def load_futures_trades():
    f = FUTURES_TRADE_DIR / f"TMF_{DATE_STR}_trades.csv"
    if f.exists():
        try:
            return pd.read_csv(f)
        except Exception:
            pass
    return None


@st.cache_data(ttl=5)
def load_options_indicators():
    f = OPTIONS_DATA_DIR / f"OPTIONS_{DATE_STR}_indicators.csv"
    if f.exists():
        try:
            return pd.read_csv(f, parse_dates=["timestamp"])
        except Exception:
            pass
    return None


@st.cache_data(ttl=5)
def load_options_ledger():
    f = OPTIONS_DATA_DIR / "options_trade_ledger.csv"
    if f.exists():
        try:
            return pd.read_csv(f, parse_dates=["Timestamp"])
        except Exception:
            pass
    return None


# ── 主畫面 ──
st.title("📊 Trading Unified Dashboard")
st.caption(f"日期: {DATE_STR} | 更新: {datetime.datetime.now().strftime('%H:%M:%S')}")

tab_overview, tab_futures, tab_options = st.tabs(["📈 總覽", "🔵 期貨 TMF", "🟠 選擇權 TXO"])

# ── Tab 1: 總覽 ──
with tab_overview:
    col1, col2 = st.columns(2)

    with col1:
        st.subheader(f"🔵 期貨 TMF ({f_mode})")
        f_df = load_futures_indicators()
        if f_df is not None and not f_df.empty:
            last = f_df.iloc[-1]
            c1, c2, c3 = st.columns(3)
            c1.metric("Close", f"{last.get('close', 0):.0f}")
            c2.metric("Score", f"{last.get('score', 0):.1f}")
            c3.metric("Bars", len(f_df))
        else:
            st.info("無期貨指標數據")

        f_trades = load_futures_trades()
        if f_trades is not None and not f_trades.empty:
            st.write(f"今日交易: {len(f_trades)} 筆")
        else:
            st.write("今日交易: 0 筆")

    with col2:
        st.subheader(f"🟠 選擇權 TXO ({o_mode})")
        o_df = load_options_indicators()
        if o_df is not None and not o_df.empty:
            last = o_df.iloc[-1]
            c1, c2, c3 = st.columns(3)
            c1.metric("MTX", f"{last.get('price_mtx', 0):.0f}")
            c2.metric("Score", f"{last.get('score', 0):.1f}")
            c3.metric("Bars", len(o_df))
        else:
            st.info("無選擇權指標數據")

        o_ledger = load_options_ledger()
        if o_ledger is not None and not o_ledger.empty:
            today = o_ledger[o_ledger["Timestamp"].dt.strftime("%Y%m%d") == DATE_STR]
            entries = today[today["Action"].str.contains("ENTRY", na=False)]
            st.write(f"今日進場: {len(entries)} 筆")
        else:
            st.write("今日交易: 0 筆")

# ── Tab 2: 期貨 ──
with tab_futures:
    st.subheader(f"🔵 期貨 TMF 詳細 ({f_mode})")
    f_df = load_futures_indicators()
    if f_df is not None and not f_df.empty:
        st.line_chart(f_df.set_index("timestamp")[["close"]], use_container_width=True)
        if "score" in f_df.columns:
            st.line_chart(f_df.set_index("timestamp")[["score"]], use_container_width=True)
        st.dataframe(f_df.tail(20), use_container_width=True)
    else:
        st.info("無數據")

    f_trades = load_futures_trades()
    if f_trades is not None and not f_trades.empty:
        st.subheader("交易記錄")
        st.dataframe(f_trades, use_container_width=True)

# ── Tab 3: 選擇權 ──
with tab_options:
    st.subheader(f"🟠 選擇權 TXO 詳細 ({o_mode})")
    o_df = load_options_indicators()
    if o_df is not None and not o_df.empty:
        if "price_mtx" in o_df.columns:
            st.line_chart(o_df.set_index("timestamp")[["price_mtx"]], use_container_width=True)
        if "score" in o_df.columns:
            st.line_chart(o_df.set_index("timestamp")[["score"]], use_container_width=True)
        st.dataframe(o_df.tail(20), use_container_width=True)
    else:
        st.info("無數據")

    o_ledger = load_options_ledger()
    if o_ledger is not None and not o_ledger.empty:
        st.subheader("交易記錄")
        st.dataframe(o_ledger.tail(30), use_container_width=True)

# ── 自動刷新 ──
import time
time.sleep(refresh_rate)
st.rerun()
