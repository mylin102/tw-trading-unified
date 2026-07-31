# 2026-07-31 Antigravity: Multipage Attribution & Performance Analysis
import sys
from pathlib import Path
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="Attribution Analysis | Trading Unified", page_icon="📊", layout="wide")

st.title("📊 歸因與評估 (Attribution & Performance Analysis)")
st.caption("策略餓死率 (Starvation)、優先級壓制與損益歸因統計")

try:
    from ui.attribution_dashboard import (
        AttributionDashboard,
        _render_router_stats,
        _render_starvation_analysis,
        _render_priority_impact,
        _render_alerts,
    )
    
    attribution_dir = ROOT / "data" / "attribution"
    dashboard = AttributionDashboard(attribution_dir)
    router_df, signal_df, trade_df = dashboard.load_data()
    
    if router_df.empty:
        st.info("ℹ️ 尚無歸因資料紀錄 (Path: ./data/attribution)")
    else:
        metrics = dashboard.calculate_summary_metrics(router_df, trade_df)
        
        # 1. Top Metrics Cards
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("評估總 K 棒", f"{metrics.get('total_bars', 300):,}")
        col2.metric("總交易次數", f"{metrics.get('total_trades', len(trade_df)):,}")
        
        tot_pnl = metrics.get('total_pnl', 0)
        if tot_pnl == 0 and not trade_df.empty and 'pnl' in trade_df.columns:
            tot_pnl = trade_df['pnl'].sum()
        col3.metric("累積淨損益", f"${tot_pnl:+,.0f} TWD")
        col4.metric("策略評估數", f"{len(metrics.get('router_summary', router_df)):,}")
        
        st.divider()

        # 2. Tabs View
        tab1, tab2, tab3, tab4 = st.tabs([
            "📈 Router 策略評估", 
            "⚠️ 策略餓死分析 (Starvation)", 
            "🎯 優先級壓制 (Priority)",
            "🚨 風控與警報"
        ])
        
        with tab1:
            _render_router_stats(metrics)
        
        with tab2:
            _render_starvation_analysis(metrics)
        
        with tab3:
            _render_priority_impact(metrics)
        
        with tab4:
            _render_alerts(dashboard, metrics)
            
except Exception as e:
    st.error(f"⚠️ 歸因資料載入失敗: {e}")
