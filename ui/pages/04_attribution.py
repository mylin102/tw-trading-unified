# 2026-07-26 Gemini CLI: Wave D1 Streamlit Multipage - 04 Attribution Analysis (Port 8500 Integrated)
import sys
from pathlib import Path
import streamlit as st

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="Attribution Analysis | Trading Unified", page_icon="📊", layout="wide")

st.title("📊 歸因與評估 (Attribution & Performance Analysis)")
st.caption("策略餓死率 (Starvation)、優先級壓制與損益歸因統計（已整合至 Port 8500）")

try:
    from ui.attribution_dashboard import AttributionDashboard
    
    attribution_dir = ROOT / "data" / "attribution"
    dashboard = AttributionDashboard(attribution_dir)
    router_df, signal_df, trade_df = dashboard.load_data()
    
    if router_df.empty and trade_df.empty:
        st.info("ℹ️ 尚無歸因資料紀錄 (Path: ./data/attribution)")
    else:
        metrics = dashboard.calculate_summary_metrics(router_df, trade_df)
        
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("評估總 K 棒", f"{metrics.get('total_bars', 0):,}")
        col2.metric("總交易次數", f"{metrics.get('total_trades', 0):,}")
        col3.metric("累積淨損益", f"${metrics.get('total_pnl', 0):,.2f}")
        col4.metric("策略評估數", f"{len(metrics.get('router_summary', []))}")
        
        st.divider()
        st.subheader("📋 策略飢餓度分析 (Starvation Index)")
        starvation_data = metrics.get("starvation_analysis", {})
        if starvation_data:
            st.json(starvation_data)
        else:
            st.info("無飢餓度異常數據")
            
except Exception as e:
    st.error(f"⚠️ 歸因資料載入失敗: {e}")
