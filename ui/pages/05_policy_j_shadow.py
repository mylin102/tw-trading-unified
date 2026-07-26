# 2026-07-26 Gemini CLI: Wave J1.5-C Policy J Shadow Mode Read-Only Visualization Page
import sys
from pathlib import Path
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="Policy J Shadow | Trading Unified", page_icon="🛡️", layout="wide")

st.title("🛡️ Policy J (Shadow Mode) 即時遙測與可視化")
st.caption("總獲利移動停利 (Policy J) 影子遙測紀錄、動態 Peak/Giveback 軌跡與觸發分析 (100% Read-Only)")

from ui.services.policy_j_reader import PolicyJTelemetryReader

reader = PolicyJTelemetryReader()
dates = reader.list_available_session_dates()

if not dates:
    st.info("ℹ️ 尚無 Policy J 影子遙測 JSONL 紀錄。系統將於 SPREAD 雙腿持倉時自動產生紀錄。")
    st.stop()

col_sel1, col_sel2 = st.columns([1, 2])
with col_sel1:
    selected_date = st.selectbox("📅 交易日 Session Date", options=dates, index=0)

snapshots = reader.load_snapshots(selected_date)

if not snapshots:
    st.info(f"ℹ️ 交易日 {selected_date} 無有效遙測紀錄。")
    st.stop()

# Extract trade IDs
df_snap = pd.DataFrame(snapshots)
trade_ids = sorted(list(df_snap["trade_id"].dropna().unique()))

with col_sel2:
    selected_trade = st.selectbox("🏷️ 持倉生命週期 Trade ID", options=["(全覽 ALL)"] + trade_ids, index=0)

if selected_trade != "(全覽 ALL)":
    filtered_snapshots = [s for s in snapshots if s.get("trade_id") == selected_trade]
else:
    filtered_snapshots = snapshots

if not filtered_snapshots:
    st.info("無匹配之持倉紀錄。")
    st.stop()

df = pd.DataFrame(filtered_snapshots)

# Calculate key metrics
last_rec = filtered_snapshots[-1]
shadow_mode = last_rec.get("mode", "SHADOW_ONLY")
exec_blocked = last_rec.get("execution_blocked", True)
current_signal = last_rec.get("shadow_signal", "NO_SIGNAL")
first_trigger_count = int(df["first_trigger_event"].sum()) if "first_trigger_event" in df.columns else 0

# Signal Badge Colors
signal_colors = {
    "NO_SIGNAL": "#9e9e9e",
    "MONITORING": "#2196f3",
    "ARMED": "#9c27b0",
    "WOULD_EXIT_BOTH": "#f44336",
}
signal_bg = signal_colors.get(current_signal, "#9e9e9e")

st.divider()

# ── Status Cards ──
c1, c2, c3, c4 = st.columns(4)
with c1:
    st.metric("運行模式 Mode", shadow_mode)
with c2:
    st.metric("下單安全門禁 Execution", "🔴 HARD-LOCKED (BLOCKED)" if exec_blocked else "🟢 ENABLED")
with c3:
    st.markdown(
        f"**影子訊號 Signal**<br><span style='background-color:{signal_bg};color:white;padding:4px 10px;border-radius:4px;font-weight:bold;'>{current_signal}</span>",
        unsafe_allow_html=True,
    )
with c4:
    st.metric("首次觸發次數 First Triggers", f"{first_trigger_count} 次")

st.divider()

# ── Numerical Summary Metrics ──
m1, m2, m3, m4, m5 = st.columns(5)
net_pnl = last_rec.get("estimated_net_exit_pnl_twd")
peak_pnl = last_rec.get("peak_net_exit_pnl_twd")
act_pnl = last_rec.get("activation_net_pnl_twd", 300.0)
gb_pnl = last_rec.get("giveback_twd", 100.0)

m1.metric("即時預估淨利 (TWD)", f"${net_pnl:,.1f}" if net_pnl is not None else "N/A")
m2.metric("波段最高淨利 Peak", f"${peak_pnl:,.1f}" if peak_pnl is not None else "N/A")
m3.metric("啟動門檻 Activation", f"${act_pnl:,.0f}")
m4.metric("回吐門檻 Giveback", f"${gb_pnl:,.0f}")
dyn_thresh = (peak_pnl - gb_pnl) if peak_pnl is not None else None
m5.metric("動態出場點 Exit Thresh", f"${dyn_thresh:,.1f}" if dyn_thresh is not None else "N/A")

st.divider()

# ── Plotly Dynamic Trajectory Chart ──
st.subheader("📈 PnL / Peak / Dynamic Exit Threshold 動態軌跡")

fig = go.Figure()

# 1. Estimated Net Exit PnL Line
if "estimated_net_exit_pnl_twd" in df.columns:
    fig.add_trace(go.Scatter(
        x=df["sequence_no"],
        y=df["estimated_net_exit_pnl_twd"],
        mode="lines+markers",
        name="預估淨利 (Estimated Net Exit PnL)",
        line=dict(color="#2196f3", width=2),
    ))

# 2. Peak Net Exit PnL Line
if "peak_net_exit_pnl_twd" in df.columns:
    fig.add_trace(go.Scatter(
        x=df["sequence_no"],
        y=df["peak_net_exit_pnl_twd"],
        mode="lines",
        name="最高淨利 (Running Peak)",
        line=dict(color="#4caf50", width=2, dash="dash"),
    ))

# 3. Dynamic Exit Threshold Line (Peak - Giveback)
if "peak_net_exit_pnl_twd" in df.columns and "giveback_twd" in df.columns:
    df["dynamic_exit_thresh"] = df["peak_net_exit_pnl_twd"] - df["giveback_twd"]
    fig.add_trace(go.Scatter(
        x=df["sequence_no"],
        y=df["dynamic_exit_thresh"],
        mode="lines",
        name="動態離場門檻 (Peak - Giveback)",
        line=dict(color="#ff9800", width=2, dash="dot"),
    ))

# 4. Activation Threshold Horizontal Line
fig.add_hline(
    y=act_pnl,
    line_dash="dash",
    line_color="#9c27b0",
    annotation_text=f"Activation ({act_pnl:.0f} TWD)",
    annotation_position="bottom right",
)

# 5. First-Trigger Event Markers
if "first_trigger_event" in df.columns:
    first_triggers = df[df["first_trigger_event"] == True]
    if not first_triggers.empty:
        fig.add_trace(go.Scatter(
            x=first_triggers["sequence_no"],
            y=first_triggers["estimated_net_exit_pnl_twd"],
            mode="markers",
            name="首次影子離場觸發 (First Trigger)",
            marker=dict(symbol="star", size=14, color="#f44336", line=dict(width=2, color="white")),
        ))

fig.update_layout(
    title="Policy J 影子模式動態軌跡 (Sequence No vs Net PnL TWD)",
    xaxis_title="事件序號 Sequence No",
    yaxis_title="淨損益 Net PnL (TWD)",
    hovermode="x unified",
    height=450,
)

st.plotly_chart(fig, use_container_width=True)

# ── Raw Telemetry Data Inspection Table ──
with st.expander("🔍 查看影子遙測數據明細 (Raw Telemetry Snapshots)", expanded=False):
    st.dataframe(df, use_container_width=True)
