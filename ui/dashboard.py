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
from dotenv import load_dotenv
import plotly.graph_objects as go

load_dotenv(Path(__file__).parent.parent / ".env")
from plotly.subplots import make_subplots
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="Trading Unified", page_icon="📊", layout="wide")

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
DATE_STR = datetime.datetime.now().strftime("%Y%m%d")
# 夜盤跨日：00:00~05:00 看前一天的檔案
if datetime.datetime.now().hour < 5:
    _yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
    TODAY = _yesterday.strftime("%Y-%m-%d")
    DATE_STR = _yesterday.strftime("%Y%m%d")

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
futures_cfg = load_yaml(FUTURES_CFG_PATH)
options_cfg = load_yaml(OPTIONS_CFG_PATH)
risk_cfg = load_yaml(RISK_CFG_PATH)
f_live = futures_cfg.get("live_trading", False)
o_live = options_cfg.get("live_trading", False)
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
        
        # 過濾 fallback 假資料
        for col in ["close", "price_mtx"]:
            if col in filtered_df.columns and len(filtered_df) > 1:
                latest_val = filtered_df[col].iloc[-1]
                if latest_val > 0:
                    filtered_df = filtered_df[filtered_df[col] > latest_val * 0.5]
        return filtered_df
    except Exception as e:
        st.error(f"資料過濾錯誤: {e}")
        return df

# ── Chart builder (unified style) ──
def make_price_score_chart(df, price_col, title, ts_col="timestamp"):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.05)
    # 再次確保繪圖時使用的是小寫欄位名 (應對 f_df 與 o_df)
    p_col = price_col.lower() if price_col.lower() in df.columns else price_col
    fig.add_trace(go.Scatter(x=df[ts_col], y=df[p_col], name=price_col, line=dict(width=1.5)), row=1, col=1)
    if "score" in df.columns:
        colors = ["#00cc66" if s >= 0 else "#ff4444" for s in df["score"]]
        fig.add_trace(go.Bar(x=df[ts_col], y=df["score"], name="Score", marker_color=colors), row=2, col=1)
    fig.update_layout(height=400, margin=dict(t=10, b=10, l=40, r=20), showlegend=False)
    # 移除手動 range，讓 Plotly 自動縮放
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


def make_pnl_chart(pnl_df, title):
    if pnl_df is None or pnl_df.empty:
        return None
    pnl_df["timestamp"] = pd.to_datetime(pnl_df["timestamp"], format="mixed")
    color = "#00cc66" if pnl_df["pnl"].iloc[-1] >= 0 else "#ff4444"
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=pnl_df["timestamp"], y=pnl_df["pnl"], fill="tozeroy", line=dict(color=color, width=1.5), name="PnL"))
    fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5)
    fig.update_layout(height=250, margin=dict(t=10, b=10, l=40, r=20), title_text=title, title_font_size=14, yaxis_tickformat=",.0f")
    return fig

@st.cache_data(ttl=5)
def load_futures_indicators():
    def _read_and_standardize(path):
        try:
            df = pd.read_csv(path)
            # 1. 處理 timestamp
            if "timestamp" not in df.columns:
                if df.index.name == "timestamp" or df.index.name == "ts":
                    df = df.reset_index()
                elif "ts" in df.columns:
                    df = df.rename(columns={"ts": "timestamp"})
                else:
                    df = df.rename(columns={df.columns[0]: "timestamp"})
            
            # 2. 統一 OHLC 欄位為小寫
            col_map = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            return df
        except:
            return None

    # 1. 優先找今天所有可能的檔案並合併
    all_dfs = []
    for tag in ["", "_LIVE", "_PAPER", "_DRY"]:
        f = FUTURES_MKT / f"TMF_{DATE_STR}{tag}_indicators.csv"
        if f.exists():
            df = _read_and_standardize(f)
            if df is not None:
                all_dfs.append(df)
    
    if all_dfs:
        merged = pd.concat(all_dfs).drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
        return filter_today(merged)
    
    # 2. 備案：找目錄下最新的一個 CSV
    try:
        all_files = list(FUTURES_MKT.glob("TMF_*_indicators.csv"))
        if all_files:
            latest_file = max(all_files, key=os.path.getmtime)
            df = _read_and_standardize(latest_file)
            return filter_today(df) if df is not None else None
    except:
        pass
    return None

@st.cache_data(ttl=5)
def load_futures_trades():
    for d in [FUTURES_TRADES]:
        f = d / f"TMF_{DATE_STR}_trades.csv"
        if f.exists():
            try: return pd.read_csv(f)
            except: pass
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
    if best:
        try: return filter_today(pd.read_csv(best[0]), ts_col="timestamp")
        except: pass
    
    # 2. 備案：找目錄下最新的任何指標檔案
    try:
        all_opt_files = list((OPTIONS_REPO / "logs").rglob("OPTIONS_*_indicators.csv"))
        if all_opt_files:
            latest_f = max(all_opt_files, key=os.path.getmtime)
            return filter_today(pd.read_csv(latest_f), ts_col="timestamp")
    except:
        pass
    return None

@st.cache_data(ttl=5)
def load_options_ledger():
    f = OPTIONS_REPO / "logs" / OPTIONS_SUB / "options_trade_ledger.csv"
    if f.exists():
        try: return pd.read_csv(f, parse_dates=["Timestamp"])
        except: pass
    return None

@st.cache_data(ttl=5)
def load_options_equity():
    f = OPTIONS_REPO / "logs" / OPTIONS_SUB / "equity_curve.csv"
    if f.exists():
        try: return pd.read_csv(f, parse_dates=["timestamp"])
        except: pass
    return None

# ── Header ──
def mode_badge(live):
    return "🔴 LIVE" if live else "📝 PAPER"

hc = st.columns([2, 1, 1, 1, 1])
hc[0].title("📊 Trading Unified")
hc[1].metric("期貨", mode_badge(f_live))
hc[2].metric("選擇權", mode_badge(o_live))
hc[3].metric("期貨分配", f"{alloc.get('futures', {}).get('max_margin_pct', 0)*100:.0f}%")
hc[4].metric("選擇權分配", f"{alloc.get('options', {}).get('max_margin_pct', 0)*100:.0f}%")

if f_live or o_live:
    st.markdown('<div style="background:#ff4444;color:white;padding:8px;text-align:center;border-radius:4px;font-weight:bold;">⚠️ LIVE TRADING ACTIVE</div>', unsafe_allow_html=True)
import subprocess
def _monitor_status():
    try:
        r = subprocess.run(["pgrep", "-f", "main.py"], capture_output=True)
        return "🟢 Running" if r.returncode == 0 else "🔴 Stopped"
    except:
        return "⚪ Unknown"

st.caption(f"日期: {TODAY} | 更新: {datetime.datetime.now().strftime('%H:%M:%S')} | Monitor: {_monitor_status()}")

# ── Tabs ──
tab_overview, tab_futures, tab_options, tab_settings = st.tabs(["📈 總覽", "🔵 期貨 TMF", "🟠 選擇權 TXO", "⚙️ 設定"])

# ════════════════════════════════════════
# Tab 1: 總覽
# ════════════════════════════════════════
with tab_overview:
    col1, col2 = st.columns(2)
    f_df = load_futures_indicators()
    o_df = load_options_indicators()

    with col1:
        st.subheader(f"🔵 期貨 TMF ({mode_badge(f_live)})")
        if f_df is not None and not f_df.empty:
            last = f_df.iloc[-1]
            c1, c2, c3 = st.columns(3)
            # 兼容大小寫
            cl_val = last.get('close') if 'close' in last else last.get('Close', 0)
            sc_val = last.get('score', 0)
            c1.metric("Close", f"{cl_val:.0f}")
            c2.metric("Score", f"{sc_val:.1f}")
            c3.metric("Bars", len(f_df))
        else:
            st.info("無期貨指標數據")
        ft = load_futures_trades()
        st.write(f"今日交易: {len(ft) if ft is not None else 0} 筆")

    with col2:
        st.subheader(f"🟠 選擇權 TXO ({mode_badge(o_live)})")
        if o_df is not None and not o_df.empty:
            last = o_df.iloc[-1]
            c1, c2, c3 = st.columns(3)
            c1.metric("MTX", f"{last.get('price_mtx', 0):.0f}")
            c2.metric("Score", f"{last.get('score', 0):.1f}")
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
    st.subheader("📊 今日指數走勢")
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    has_data = False
    if f_df is not None and not f_df.empty:
        # 兼容大小寫
        f_close = f_df["close"] if "close" in f_df.columns else f_df["Close"]
        fig.add_trace(go.Scatter(x=f_df["timestamp"], y=f_close, name="TMF", line=dict(color="#1f77b4", width=1.5)), secondary_y=False)
        has_data = True
    if o_df is not None and not o_df.empty and "price_mtx" in o_df.columns:
        fig.add_trace(go.Scatter(x=o_df["timestamp"], y=o_df["price_mtx"], name="MTX (Options)", line=dict(color="#ff7f0e", width=1.5)), secondary_y=True)
        has_data = True
    if has_data:
        fig.update_layout(height=350, margin=dict(t=10, b=10, l=40, r=20), legend=dict(orientation="h", y=1.02))
        fig.update_yaxes(title_text="TMF", tickformat=",.0f", secondary_y=False)
        fig.update_yaxes(title_text="MTX", tickformat=",.0f", secondary_y=True)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("等待數據...")

    # ── 總覽 PnL ──
    st.subheader("💰 今日累計 PnL")
    pc1, pc2 = st.columns(2)
    ft = load_futures_trades()
    fpnl = calc_futures_pnl(ft)
    ol = load_options_ledger()
    opnl = calc_options_pnl(ol)
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

# ════════════════════════════════════════
# Tab 2: 期貨
# ════════════════════════════════════════
with tab_futures:
    st.subheader(f"🔵 期貨 TMF ({mode_badge(f_live)})")
    f_df = load_futures_indicators()
    if f_df is not None and not f_df.empty:
        last = f_df.iloc[-1]
        fc1, fc2, fc3, fc4 = st.columns(4)
        # 兼容大小寫
        cl_val = last.get('close') if 'close' in last else last.get('Close', 0)
        sc_val = last.get('score', 0)
        fc1.metric("Close", f"{cl_val:.0f}")
        fc2.metric("Score", f"{sc_val:.1f}")
        bull = last.get("bull_align", last.get("bullish_align", False))
        bear = last.get("bear_align", last.get("bearish_align", False))
        trend = "🟢 多頭排列" if bull else ("🔴 空頭排列" if bear else "⚪ 中性")
        fc3.metric("趨勢", trend)
        fc4.metric("Squeeze", "🔒 壓縮中" if last.get("sqz_on", False) else "🔓 已釋放")
        st.plotly_chart(make_price_score_chart(f_df, "close", "TMF 價格 & Score"), use_container_width=True)
        st.dataframe(f_df.tail(20), use_container_width=True)
    else:
        st.info("無數據")
    ft = load_futures_trades()
    if ft is not None and not ft.empty:
        st.subheader("交易記錄")
        st.dataframe(ft, use_container_width=True)
        fpnl = calc_futures_pnl(ft)
        fig = make_pnl_chart(fpnl, "期貨累計 PnL (TWD)")
        if fig:
            st.plotly_chart(fig, use_container_width=True)

# ════════════════════════════════════════
# Tab 3: 選擇權
# ════════════════════════════════════════
with tab_options:
    st.subheader(f"🟠 選擇權 TXO ({mode_badge(o_live)})")
    o_df = load_options_indicators()
    if o_df is not None and not o_df.empty and "price_mtx" in o_df.columns:
        last = o_df.iloc[-1]
        oc1, oc2, oc3, oc4 = st.columns(4)
        oc1.metric("MTX", f"{last.get('price_mtx', 0):.0f}")
        oc2.metric("Score", f"{last.get('score', 0):.1f}")
        trend = last.get("mid_trend", "")
        trend_label = "🟢 BULL" if trend == "BULL" else ("🔴 BEAR" if trend == "BEAR" else "⚪ —")
        oc3.metric("趨勢", trend_label)
        iv = last.get("iv", 0)
        oc4.metric("IV", f"{iv*100:.1f}%" if iv and iv < 1 else f"{iv:.1f}%")
        st.plotly_chart(make_price_score_chart(o_df, "price_mtx", "MTX 價格 & Score"), use_container_width=True)
        st.dataframe(o_df.tail(20), use_container_width=True)
    else:
        st.info("無數據")
    ol = load_options_ledger()
    if ol is not None and not ol.empty:
        st.subheader("交易記錄")
        st.dataframe(ol.tail(30), use_container_width=True)
        opnl = calc_options_pnl(ol)
        fig = make_pnl_chart(opnl, "選擇權累計 PnL (TWD)")
        if fig:
            st.plotly_chart(fig, use_container_width=True)

# ════════════════════════════════════════
# Tab 4: 設定
# ════════════════════════════════════════
with tab_settings:
    st.subheader("⚙️ 策略設定")

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
    o_force = oc5.checkbox("Force Close at End", value=options_cfg.get("modes", {}).get(options_cfg.get("active_mode", "V2"), {}).get("force_close_at_end", False), key="o_force")

    if st.button("✅ 套用參數", type="primary"):
        risk_cfg.setdefault("allocation", {}).setdefault("futures", {})["max_margin_pct"] = f_pct / 100
        risk_cfg["allocation"].setdefault("options", {})["max_margin_pct"] = o_pct / 100
        save_yaml(RISK_CFG_PATH, risk_cfg)
        futures_cfg.setdefault("strategy", {})["entry_score"] = f_entry
        futures_cfg.setdefault("risk_mgmt", {})["stop_loss_pts"] = f_sl
        futures_cfg.setdefault("strategy", {}).setdefault("partial_exit", {})["tp1_pts"] = f_tp
        futures_cfg.setdefault("trade_mgmt", {})["lots_per_trade"] = f_lots
        futures_cfg["trade_mgmt"]["max_positions"] = f_max
        save_yaml(FUTURES_CFG_PATH, futures_cfg)
        options_cfg.setdefault("strategy", {})["entry_score"] = o_entry
        options_cfg.setdefault("risk_mgmt", {})["stop_loss_pct"] = o_sl / 100
        options_cfg.setdefault("exit_strategy", {})["tp1_pct"] = o_tp / 100
        options_cfg["risk_mgmt"]["lots_per_trade"] = o_lots
        save_yaml(OPTIONS_CFG_PATH, options_cfg)
        st.toast("✅ 參數已套用，下一棒生效")

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
                    trigger_restart()
                    st.rerun()
        else:
            if st.button("切換至 PAPER 📝", key="f_to_paper"):
                futures_cfg["live_trading"] = False
                save_yaml(FUTURES_CFG_PATH, futures_cfg)
                st.toast("📝 期貨已切換至 PAPER")
                trigger_restart()
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
                    trigger_restart()
                    st.rerun()
        else:
            if st.button("切換至 PAPER 📝", key="o_to_paper"):
                options_cfg["live_trading"] = False
                save_yaml(OPTIONS_CFG_PATH, options_cfg)
                st.toast("📝 選擇權已切換至 PAPER")
                trigger_restart()
                st.rerun()

    st.markdown("---")
    st.markdown("### 🗑️ 模擬交易重置")
    r1, r2 = st.columns(2)
    with r1:
        f_init = st.number_input("期貨期初資金", 10000, 1000000, int(futures_cfg.get("execution", {}).get("initial_balance", 100000)), 10000, key="f_init")
        if st.button("🔄 重置期貨模擬", key="f_reset"):
            for f in (BASE / "exports/trades").glob("TMF_*_trades.*"):
                f.unlink()
            st.success(f"✅ 期貨模擬已重置，期初資金 {f_init:,.0f}")
    with r2:
        o_init = st.number_input("選擇權期初資金", 10000, 1000000, 40000, 10000, key="o_init")
        if st.button("🔄 重置選擇權模擬", key="o_reset"):
            paper_dir = OPTIONS_REPO / "logs/paper_trading"
            ledger_f = paper_dir / "options_trade_ledger.csv"
            if ledger_f.exists():
                pd.DataFrame(columns=["Timestamp", "Mode", "Action", "Side", "Price", "Quantity", "PnL", "Balance", "Note"]).to_csv(ledger_f, index=False)
            for f in paper_dir.glob("OPTIONS_*_indicators.csv"):
                f.unlink()
            pd.DataFrame([{"timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "action": "INITIAL", "side": "", "price": 0, "quantity": 0, "pnl": 0, "balance": o_init, "note": "重置"}]).to_csv(paper_dir / "equity_curve.csv", index=False)
            st.success(f"✅ 選擇權模擬已重置，期初資金 {o_init:,.0f}")

# ── Sidebar ──
with st.sidebar:
    st.header("⚙️ 快速資訊")
    refresh = st.slider("自動刷新 (秒)", 5, 60, 15, 5)
    st.divider()
    st.write(f"期貨: {mode_badge(f_live)}")
    st.write(f"選擇權: {mode_badge(o_live)}")
    st.write(f"分配: 期貨 {alloc.get('futures', {}).get('max_margin_pct', 0)*100:.0f}% / 選擇權 {alloc.get('options', {}).get('max_margin_pct', 0)*100:.0f}%")
    st.divider()
    st.caption(f"📁 期貨: {FUTURES_MKT}")
    st.caption(f"📁 選擇權: {OPTIONS_DATA}")

import time
time.sleep(refresh)
st.rerun()
