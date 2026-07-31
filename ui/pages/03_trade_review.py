# 2026-07-31 Antigravity: Multipage Single Trade Review Module
import sys
import os
import json
import pandas as pd
from pathlib import Path
import streamlit as st

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="Trade Review | Trading Unified", page_icon="🔍", layout="wide")

st.title("🔍 交易檢討 (Trade Review)")
st.caption("單筆交易入場/離場生命週期檢討")

fills_path = ROOT / "logs" / "mts_trade_fills.jsonl"
events_path = ROOT / "logs" / "mts_spread_events.jsonl"

try:
    from scripts.generate_daily_report import parse_logs, _format_exit_reason_label
    log_data = parse_logs(str(fills_path), str(events_path), target_date=None)
    completed_trades = log_data.get("completed", [])
except Exception as e:
    completed_trades = []
    st.error(f"解析交易日誌失敗: {e}")

if not completed_trades:
    st.info("💡 尚無已完結交易紀錄可供檢討。系統將在交易平倉後自動產出檢討數據。")
else:
    # Format trade options for selectbox
    trade_options = {}
    for t in reversed(completed_trades):
        t_id = t["trade_id"]
        t_time = t["entry_time"].split("T")[1][:8] if "T" in t["entry_time"] else t["entry_time"]
        t_pnl = t["net_pnl"]
        t_reason = t["exit_reason"]
        
        label = f"[{t_time}] {t_id[-8:]} | 損益: {t_pnl:+,.0f} TWD | 原因: {t_reason}"
        trade_options[label] = t

    selected_label = st.selectbox("🎯 選擇檢討交易 (Select Trade to Review)", options=list(trade_options.keys()))
    trade = trade_options[selected_label]

    st.markdown("---")

    # 1. Summary Cards
    st.subheader(f"📊 交易摘要：`{trade['trade_id']}`")
    
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("建倉時段", "☀️ 日盤" if str(trade.get("entry_session", trade.get("session"))).lower() == "day" else "🌙 夜盤")
    c2.metric("交易動作", trade.get("action", "—"))
    c3.metric("淨利 (TWD)", f"{trade['net_pnl']:+,.0f} TWD")
    
    is_pj = ("COMBINED_EXIT" in str(trade.get("exit_reason", "")).upper() or "POLICY_J" in str(trade.get("exit_reason", "")).upper())
    c4.metric("出場模式", "🛡️ Policy J 組合停利" if is_pj else "分段平倉")

    # 2. Detailed Legs Comparison
    st.subheader("🦵 腿位執行細節 (Leg Execution Details)")
    
    col_entry, col_rel, col_exit = st.columns(3)
    
    with col_entry:
        st.markdown("##### 1. 建倉階段 (ENTRY)")
        st.write(f"**時間:** `{trade.get('entry_time', '—')}`")
        st.write(f"**近月價格:** `{trade.get('near_entry', 0):,.0f}`")
        st.write(f"**遠月價格:** `{trade.get('far_entry', 0):,.0f}`")
        if trade.get("near_entry") and trade.get("far_entry"):
            st.write(f"**進場價差 (Spread):** `{trade['near_entry'] - trade['far_entry']:,.0f}` 點")
        st.write(f"**MTF Score:** `{trade.get('entry_mtf', '—')}`")

    with col_rel:
        st.markdown("##### 2. 第一腿離場 (RELEASE)")
        if is_pj:
            st.info("— 雙腿同步平倉 (無拆腿離場)")
        else:
            st.write(f"**離線腿位:** `{trade.get('release_leg', '—')}`")
            st.write(f"**成交價格:** `{trade.get('release_price', 0):,.0f}`")
            st.write(f"**第一腿 PnL:** `{trade.get('release_pnl', 0):+,.0f} TWD`")
            st.write(f"**觸發原因:** `{trade.get('release_reason', '—')}`")

    with col_exit:
        st.markdown("##### 3. 第二腿 / 組合離場 (EXIT)")
        st.write(f"**離場時間:** `{trade.get('exit_time', '—')}`")
        st.write(f"**成交價格:** `{trade.get('exit_price', 0):,.0f}`")
        st.write(f"**第二腿 / 組合 PnL:** `{trade.get('exit_pnl', 0):+,.0f} TWD`")
        st.write(f"**離場原因:** `{trade.get('exit_reason', '—')}`")

    st.markdown("---")

    # 3. Event Ledger Trajectory
    st.subheader("📜 交易事件軌跡時間軸 (Event Ledger Trajectory)")
    
    ev_list = []
    if events_path.exists():
        try:
            with open(events_path) as f:
                for line in f:
                    if line.strip():
                        ev = json.loads(line.strip())
                        if ev.get("trade_id") == trade["trade_id"]:
                            ev_list.append(ev)
        except Exception:
            pass

    if ev_list:
        df_ev = pd.DataFrame(ev_list)
        # Select key columns
        show_cols = [c for c in ["ts", "event", "exit_reason", "gross_points", "pnl", "risk_mode", "spread_z"] if c in df_ev.columns]
        st.dataframe(df_ev[show_cols], width='stretch', hide_index=True)
    else:
        st.info("尚無該筆交易的詳細事件軌跡紀錄")
