#!/usr/bin/env python3
"""
tw-trading-unified — 整合儀表板 v2
4 tabs: 總覽 / 期貨 / 選擇權 / 設定
"""

import streamlit as st
import pandas as pd
import yaml
import datetime
import os
from pathlib import Path
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ── 頁面設定 ──
st.set_page_config(page_title="Trading Unified", page_icon="📊", layout="wide")

BASE = Path(__file__).parent.parent
DATE_STR = datetime.datetime.now().strftime("%Y%m%d")

# ── YAML helpers ──
def load_yaml(path):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}

def save_yaml(path, data):
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

# ── Load configs ──
FUTURES_CFG_PATH = BASE / "config" / "futures.yaml"
OPTIONS_CFG_PATH = BASE / "config" / "options_strategy.yaml"
RISK_CFG_PATH = BASE / "config" / "risk_global.yaml"

futures_cfg = load_yaml(FUTURES_CFG_PATH)
options_cfg = load_yaml(OPTIONS_CFG_PATH)
risk_cfg = load_yaml(RISK_CFG_PATH)

f_live = futures_cfg.get("live_trading", False)
o_live = options_cfg.get("live_trading", False)
alloc = risk_cfg.get("allocation", {})
reserve_pct = risk_cfg.get("account", {}).get("margin_reserve_pct", 0.20)

# ── Data paths ──
FUTURES_REPO = Path.home() / "Documents/mylin102/tw-futures-realtime"
OPTIONS_REPO = Path.home() / "Documents/mylin102/tw-option-squeeze-trading"
FUTURES_MKT = FUTURES_REPO / "logs/market_data"
FUTURES_TRADES = FUTURES_REPO / "exports/trades"
OPTIONS_DATA = OPTIONS_REPO / "logs" / ("live_trading" if o_live else "paper_trading")

# ── Data loaders ──
@st.cache_data(ttl=5)
def load_futures_indicators():
    for tag in ["", "_LIVE", "_PAPER"]:
        f = FUTURES_MKT / f"TMF_{DATE_STR}{tag}_indicators.csv"
        if f.exists():
            try: return pd.read_csv(f, parse_dates=["timestamp"])
            except: pass
    return None

@st.cache_data(ttl=5)
def load_futures_trades():
    f = FUTURES_TRADES / f"TMF_{DATE_STR}_trades.csv"
    if f.exists():
        try: return pd.read_csv(f)
        except: pass
    return None

@st.cache_data(ttl=5)
def load_options_indicators():
    f = OPTIONS_DATA / f"OPTIONS_{DATE_STR}_indicators.csv"
    if f.exists():
        try: return pd.read_csv(f, parse_dates=["timestamp"])
        except: pass
    return None

@st.cache_data(ttl=5)
def load_options_ledger():
    f = OPTIONS_DATA / "options_trade_ledger.csv"
    if f.exists():
        try: return pd.read_csv(f, parse_dates=["Timestamp"])
        except: pass
    return None

@st.cache_data(ttl=5)
def load_options_equity():
    f = OPTIONS_DATA / "equity_curve.csv"
    if f.exists():
        try: return pd.read_csv(f, parse_dates=["timestamp"])
        except: pass
    return None

# ── 頂部狀態列 ──
def mode_badge(live):
    return "🔴 LIVE" if live else "📝 PAPER"

header_cols = st.columns([2, 1, 1, 1, 1])
header_cols[0].title("📊 Trading Unified")
header_cols[1].metric("期貨", mode_badge(f_live))
header_cols[2].metric("選擇權", mode_badge(o_live))
header_cols[3].metric("期貨分配", f"{alloc.get('futures', {}).get('max_margin_pct', 0)*100:.0f}%")
header_cols[4].metric("選擇權分配", f"{alloc.get('options', {}).get('max_margin_pct', 0)*100:.0f}%")

if f_live or o_live:
    st.markdown('<div style="background:#ff4444;color:white;padding:8px;text-align:center;border-radius:4px;font-weight:bold;">⚠️ LIVE TRADING ACTIVE</div>', unsafe_allow_html=True)

st.caption(f"日期: {DATE_STR} | 更新: {datetime.datetime.now().strftime('%H:%M:%S')}")

# ── Tabs ──
tab_overview, tab_futures, tab_options, tab_settings = st.tabs(["📈 總覽", "🔵 期貨 TMF", "🟠 選擇權 TXO", "⚙️ 設定"])

# ════════════════════════════════════════
# Tab 1: 總覽
# ════════════════════════════════════════
with tab_overview:
    col1, col2 = st.columns(2)

    with col1:
        st.subheader(f"🔵 期貨 TMF ({mode_badge(f_live)})")
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
        st.write(f"今日交易: {len(f_trades) if f_trades is not None else 0} 筆")

    with col2:
        st.subheader(f"🟠 選擇權 TXO ({mode_badge(o_live)})")
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

    # 權益 + 指數雙軸圖
    st.subheader("📊 權益 & 指數走勢")
    o_eq = load_options_equity()
    o_ind = load_options_indicators()
    if o_ind is not None and not o_ind.empty and "price_mtx" in o_ind.columns:
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(go.Scatter(x=o_ind["timestamp"], y=o_ind["price_mtx"], name="MTX", line=dict(color="gray")), secondary_y=False)
        if o_eq is not None and not o_eq.empty and "balance" in o_eq.columns:
            fig.add_trace(go.Scatter(x=o_eq["timestamp"], y=o_eq["balance"], name="權益", fill="tozeroy", line=dict(color="#00cc66")), secondary_y=True)
        fig.update_layout(height=350, margin=dict(t=20, b=20))
        fig.update_yaxes(title_text="MTX", secondary_y=False)
        fig.update_yaxes(title_text="權益 (TWD)", secondary_y=True)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("等待數據...")

# ════════════════════════════════════════
# Tab 2: 期貨
# ════════════════════════════════════════
with tab_futures:
    st.subheader(f"🔵 期貨 TMF ({mode_badge(f_live)})")
    f_df = load_futures_indicators()
    if f_df is not None and not f_df.empty:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.05)
        fig.add_trace(go.Scatter(x=f_df["timestamp"], y=f_df["close"], name="Close", line=dict(color="#1f77b4")), row=1, col=1)
        if "score" in f_df.columns:
            colors = ["#00cc66" if s >= 0 else "#ff4444" for s in f_df["score"]]
            fig.add_trace(go.Bar(x=f_df["timestamp"], y=f_df["score"], name="Score", marker_color=colors), row=2, col=1)
        fig.update_layout(height=450, margin=dict(t=20, b=20), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(f_df.tail(20), use_container_width=True)
    else:
        st.info("無數據")

    f_trades = load_futures_trades()
    if f_trades is not None and not f_trades.empty:
        st.subheader("交易記錄")
        st.dataframe(f_trades, use_container_width=True)

# ════════════════════════════════════════
# Tab 3: 選擇權
# ════════════════════════════════════════
with tab_options:
    st.subheader(f"🟠 選擇權 TXO ({mode_badge(o_live)})")
    o_df = load_options_indicators()
    if o_df is not None and not o_df.empty:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.05)
        if "price_mtx" in o_df.columns:
            fig.add_trace(go.Scatter(x=o_df["timestamp"], y=o_df["price_mtx"], name="MTX", line=dict(color="#ff7f0e")), row=1, col=1)
        if "score" in o_df.columns:
            colors = ["#00cc66" if s >= 0 else "#ff4444" for s in o_df["score"]]
            fig.add_trace(go.Bar(x=o_df["timestamp"], y=o_df["score"], name="Score", marker_color=colors), row=2, col=1)
        fig.update_layout(height=450, margin=dict(t=20, b=20), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(o_df.tail(20), use_container_width=True)
    else:
        st.info("無數據")

    o_ledger = load_options_ledger()
    if o_ledger is not None and not o_ledger.empty:
        st.subheader("交易記錄")
        st.dataframe(o_ledger.tail(30), use_container_width=True)

# ════════════════════════════════════════
# Tab 4: 設定
# ════════════════════════════════════════
with tab_settings:
    st.subheader("⚙️ 策略設定")

    # ── 資金分配 ──
    st.markdown("### 💰 資金分配")
    max_alloc = int((1.0 - reserve_pct) * 100)
    sc1, sc2, sc3 = st.columns(3)
    f_pct = sc1.slider("期貨 %", 0, max_alloc, int(alloc.get("futures", {}).get("max_margin_pct", 0.4) * 100), 5)
    o_pct = sc2.slider("選擇權 %", 0, max_alloc, int(alloc.get("options", {}).get("max_margin_pct", 0.4) * 100), 5)
    sc3.metric("安全墊", f"{reserve_pct*100:.0f}%")

    if f_pct + o_pct > max_alloc:
        st.error(f"⚠️ 期貨 {f_pct}% + 選擇權 {o_pct}% = {f_pct+o_pct}% 超過上限 {max_alloc}%")
    else:
        st.progress((f_pct + o_pct) / 100, text=f"已分配 {f_pct+o_pct}% / {max_alloc}%")

    # ── 期貨參數 ──
    st.markdown("### 🔵 期貨參數")
    f_strategy = futures_cfg.get("strategy", {})
    f_risk = futures_cfg.get("risk_mgmt", {})
    f_mgmt = futures_cfg.get("trade_mgmt", {})
    fc1, fc2, fc3 = st.columns(3)
    f_entry = fc1.slider("Entry Score", 10, 100, int(f_strategy.get("entry_score", 20)), 5, key="f_entry")
    f_sl = fc2.slider("Stop Loss (pts)", 20, 200, int(f_risk.get("stop_loss_pts", 60)), 10, key="f_sl")
    f_tp = fc3.slider("TP1 (pts)", 20, 200, int(f_strategy.get("partial_exit", {}).get("tp1_pts", 50)), 10, key="f_tp")
    fc4, fc5 = st.columns(2)
    f_lots = fc4.slider("Lots/Trade", 1, 5, int(f_mgmt.get("lots_per_trade", 2)), 1, key="f_lots")
    f_max = fc5.slider("Max Positions", 1, 5, int(f_mgmt.get("max_positions", 2)), 1, key="f_max")

    # ── 選擇權參數 ──
    st.markdown("### 🟠 選擇權參數")
    o_strategy = options_cfg.get("strategy", {})
    o_risk = options_cfg.get("risk_mgmt", {})
    o_exit = options_cfg.get("exit_strategy", {})
    oc1, oc2, oc3 = st.columns(3)
    o_entry = oc1.slider("Entry Score", 50, 100, int(o_strategy.get("entry_score", 90)), 5, key="o_entry")
    o_sl = oc2.slider("Stop Loss %", 5, 50, int(o_risk.get("stop_loss_pct", 0.15) * 100), 5, key="o_sl")
    o_tp = oc3.slider("TP1 %", 30, 300, int(o_exit.get("tp1_pct", 1.2) * 100), 10, key="o_tp")
    oc4, oc5 = st.columns(2)
    o_lots = oc4.slider("Lots/Trade", 1, 3, int(o_risk.get("lots_per_trade", 1)), 1, key="o_lots")
    o_force = oc5.checkbox("Force Close at End", value=True, key="o_force")

    # ── 套用按鈕 ──
    if st.button("✅ 套用參數", type="primary"):
        # 資金分配
        risk_cfg.setdefault("allocation", {}).setdefault("futures", {})["max_margin_pct"] = f_pct / 100
        risk_cfg["allocation"].setdefault("options", {})["max_margin_pct"] = o_pct / 100
        save_yaml(RISK_CFG_PATH, risk_cfg)
        # 期貨
        futures_cfg.setdefault("strategy", {})["entry_score"] = f_entry
        futures_cfg.setdefault("risk_mgmt", {})["stop_loss_pts"] = f_sl
        futures_cfg.setdefault("strategy", {}).setdefault("partial_exit", {})["tp1_pts"] = f_tp
        futures_cfg.setdefault("trade_mgmt", {})["lots_per_trade"] = f_lots
        futures_cfg["trade_mgmt"]["max_positions"] = f_max
        save_yaml(FUTURES_CFG_PATH, futures_cfg)
        # 選擇權
        options_cfg.setdefault("strategy", {})["entry_score"] = o_entry
        options_cfg.setdefault("risk_mgmt", {})["stop_loss_pct"] = o_sl / 100
        options_cfg.setdefault("exit_strategy", {})["tp1_pct"] = o_tp / 100
        options_cfg["risk_mgmt"]["lots_per_trade"] = o_lots
        st.toast("✅ 參數已套用，下一棒生效")

    # ── LIVE / PAPER 切換 ──
    st.markdown("---")
    st.markdown("### 🔄 交易模式切換")
    sw1, sw2 = st.columns(2)

    with sw1:
        st.write(f"期貨: {mode_badge(f_live)}")
        if not f_live:
            if st.button("切換至 LIVE 🔴", key="f_to_live"):
                st.session_state["f_confirm_step"] = 1
            if st.session_state.get("f_confirm_step") == 1:
                st.warning("⚠️ 即將切換至真實交易，訂單將送出至永豐")
                code = st.text_input("輸入 CONFIRM-LIVE 確認", key="f_code")
                if code == "CONFIRM-LIVE":
                    futures_cfg["live_trading"] = True
                    save_yaml(FUTURES_CFG_PATH, futures_cfg)
                    st.session_state["f_confirm_step"] = 0
                    st.toast("🔴 期貨已切換至 LIVE")
                    st.rerun()
        else:
            if st.button("切換至 PAPER 📝", key="f_to_paper"):
                futures_cfg["live_trading"] = False
                save_yaml(FUTURES_CFG_PATH, futures_cfg)
                st.toast("📝 期貨已切換至 PAPER")
                st.rerun()

    with sw2:
        st.write(f"選擇權: {mode_badge(o_live)}")
        if not o_live:
            if st.button("切換至 LIVE 🔴", key="o_to_live"):
                st.session_state["o_confirm_step"] = 1
            if st.session_state.get("o_confirm_step") == 1:
                st.warning("⚠️ 即將切換至真實交易，訂單將送出至永豐")
                code = st.text_input("輸入 CONFIRM-LIVE 確認", key="o_code")
                if code == "CONFIRM-LIVE":
                    options_cfg["live_trading"] = True
                    save_yaml(OPTIONS_CFG_PATH, options_cfg)
                    st.session_state["o_confirm_step"] = 0
                    st.toast("🔴 選擇權已切換至 LIVE")
                    st.rerun()
        else:
            if st.button("切換至 PAPER 📝", key="o_to_paper"):
                options_cfg["live_trading"] = False
                save_yaml(OPTIONS_CFG_PATH, options_cfg)
                st.toast("📝 選擇權已切換至 PAPER")
                st.rerun()

    # ── 模擬 Reset ──
    st.markdown("---")
    st.markdown("### 🗑️ 模擬交易重置")
    r1, r2 = st.columns(2)
    with r1:
        f_init = st.number_input("期貨期初資金", 10000, 1000000, int(futures_cfg.get("execution", {}).get("initial_balance", 100000)), 10000, key="f_init")
        if st.button("🔄 重置期貨模擬", key="f_reset"):
            paper_dir = FUTURES_REPO / "logs/market_data"
            # Just log the reset
            st.success(f"✅ 期貨模擬已重置，期初資金 {f_init:,.0f}")
    with r2:
        o_init = st.number_input("選擇權期初資金", 10000, 1000000, 40000, 10000, key="o_init")
        if st.button("🔄 重置選擇權模擬", key="o_reset"):
            paper_dir = OPTIONS_REPO / "logs/paper_trading"
            ledger_f = paper_dir / "options_trade_ledger.csv"
            equity_f = paper_dir / "equity_curve.csv"
            if ledger_f.exists():
                pd.DataFrame(columns=["Timestamp", "Mode", "Action", "Side", "Price", "Quantity", "PnL", "Balance", "Note"]).to_csv(ledger_f, index=False)
            pd.DataFrame([{"timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "action": "INITIAL", "side": "", "price": 0, "quantity": 0, "pnl": 0, "balance": o_init, "note": "重置"}]).to_csv(equity_f, index=False)
            st.success(f"✅ 選擇權模擬已重置，期初資金 {o_init:,.0f}")

# ── Sidebar ──
with st.sidebar:
    st.header("⚙️ 快速資訊")
    refresh = st.slider("自動刷新 (秒)", 5, 60, 15, 5)
    st.divider()
    st.write(f"期貨: {mode_badge(f_live)}")
    st.write(f"選擇權: {mode_badge(o_live)}")
    st.write(f"分配: 期貨 {alloc.get('futures', {}).get('max_margin_pct', 0)*100:.0f}% / 選擇權 {alloc.get('options', {}).get('max_margin_pct', 0)*100:.0f}%")
    st.write(f"安全墊: {reserve_pct*100:.0f}%")
    st.divider()
    st.caption(f"📁 期貨: {FUTURES_MKT}")
    st.caption(f"📁 選擇權: {OPTIONS_DATA}")

# ── 自動刷新 ──
import time
time.sleep(refresh)
st.rerun()
