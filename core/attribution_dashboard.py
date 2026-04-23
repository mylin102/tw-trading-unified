#!/usr/bin/env python3
"""
Attribution Dashboard Module

Provides attribution metrics and starvation analysis for the trading dashboard.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import json
from datetime import datetime, timedelta
import streamlit as st


class AttributionDashboard:
    """Dashboard module for attribution analysis."""
    
    def __init__(self, attribution_dir: Path):
        self.attribution_dir = Path(attribution_dir)
        self.router_log_path = self.attribution_dir / "router_evaluation_log.csv"
        self.signal_log_path = self.attribution_dir / "strategy_signal_log.csv"
        self.trade_log_path = self.attribution_dir / "trade_attribution_log.csv"
        
    def load_data(self) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Load attribution data."""
        router_df = pd.DataFrame()
        signal_df = pd.DataFrame()
        trade_df = pd.DataFrame()
        
        if self.router_log_path.exists():
            router_df = pd.read_csv(self.router_log_path)
            if 'timestamp' in router_df.columns:
                router_df['timestamp'] = pd.to_datetime(router_df['timestamp'])
        
        if self.signal_log_path.exists():
            signal_df = pd.read_csv(self.signal_log_path)
            if 'timestamp' in signal_df.columns:
                signal_df['timestamp'] = pd.to_datetime(signal_df['timestamp'])
        
        if self.trade_log_path.exists():
            trade_df = pd.read_csv(self.trade_log_path)
            if 'entry_time' in trade_df.columns:
                trade_df['entry_time'] = pd.to_datetime(trade_df['entry_time'])
            if 'exit_time' in trade_df.columns:
                trade_df['exit_time'] = pd.to_datetime(trade_df['exit_time'])
        
        return router_df, signal_df, trade_df
    
    def calculate_summary_metrics(self, router_df: pd.DataFrame, trade_df: pd.DataFrame) -> Dict:
        """Calculate key attribution metrics."""
        if router_df.empty:
            return {}
        
        # Router summary
        router_summary = self._summarize_router(router_df)
        
        # Trade summary
        trade_summary = self._summarize_trades(trade_df)
        
        # Starvation analysis
        starvation = self._analyze_starvation(router_summary)
        
        # Priority impact
        priority_impact = self._calculate_priority_impact(router_summary)
        
        # Combine metrics
        metrics = {
            "router_summary": router_summary.to_dict('records') if not router_summary.empty else [],
            "trade_summary": trade_summary.to_dict('records') if not trade_summary.empty else [],
            "starvation_analysis": starvation,
            "priority_impact": priority_impact,
            "total_bars": len(router_df),
            "total_trades": len(trade_df),
            "total_pnl": trade_df['pnl'].sum() if not trade_df.empty and 'pnl' in trade_df.columns else 0,
            "last_update": datetime.now().isoformat()
        }
        
        return metrics
    
    def _summarize_router(self, router_df: pd.DataFrame) -> pd.DataFrame:
        """Summarize router evaluation data."""
        if router_df.empty:
            return pd.DataFrame()
        
        # Filter out router entries
        strategy_df = router_df[router_df['strategy_name'] != 'router'].copy()
        
        if strategy_df.empty:
            return pd.DataFrame()
        
        # Group by strategy
        summary = strategy_df.groupby('strategy_name').agg({
            'candidate': 'sum',
            'evaluated': 'sum',
            'winner': 'sum',
            'shadowed': 'sum',
            'regime_mismatch': 'sum',
            'no_signal': 'sum',
            'missing': 'sum'
        }).reset_index()
        
        # Calculate rates
        summary['candidate_rate'] = summary['candidate'] / summary['candidate'].sum()
        summary['evaluation_rate'] = summary['evaluated'] / summary['candidate']
        summary['shadow_rate'] = summary['shadowed'] / summary['candidate']
        summary['win_conversion'] = summary['winner'] / summary['evaluated'].replace(0, np.nan)
        summary['starvation_index'] = 1 - summary['evaluation_rate']
        
        # Fill NaN
        summary = summary.fillna(0)
        
        return summary
    
    def _summarize_trades(self, trade_df: pd.DataFrame) -> pd.DataFrame:
        """Summarize trade attribution data."""
        if trade_df.empty:
            return pd.DataFrame()
        
        # Group by strategy
        summary = trade_df.groupby('strategy_name').agg({
            'trade_id': 'count',
            'pnl': ['sum', 'mean', 'std'],
            'exit_reason': lambda x: x.value_counts().to_dict()
        }).reset_index()
        
        # Flatten columns
        summary.columns = ['strategy_name', 'trade_count', 'total_pnl', 'avg_pnl', 'std_pnl', 'exit_reasons']
        
        # Calculate win rate
        if 'pnl' in trade_df.columns:
            win_counts = trade_df.groupby('strategy_name')['pnl'].apply(lambda x: (x > 0).sum()).reset_index()
            win_counts.columns = ['strategy_name', 'win_count']
            summary = pd.merge(summary, win_counts, on='strategy_name', how='left')
            summary['win_rate'] = summary['win_count'] / summary['trade_count']
        else:
            summary['win_rate'] = 0.0
        
        return summary
    
    def _analyze_starvation(self, router_summary: pd.DataFrame) -> Dict:
        """Analyze starvation levels."""
        if router_summary.empty:
            return {}
        
        starvation_levels = []
        for _, row in router_summary.iterrows():
            idx = row['starvation_index']
            if idx > 0.7:
                level = "severe"
            elif idx > 0.4:
                level = "moderate"
            else:
                level = "acceptable"
            
            starvation_levels.append({
                "strategy_name": row['strategy_name'],
                "starvation_index": idx,
                "level": level,
                "eval_count": row['evaluated'],
                "shadowed_count": row['shadowed']
            })
        
        # Count by level
        severe_count = sum(1 for s in starvation_levels if s['level'] == 'severe')
        moderate_count = sum(1 for s in starvation_levels if s['level'] == 'moderate')
        acceptable_count = sum(1 for s in starvation_levels if s['level'] == 'acceptable')
        
        return {
            "by_strategy": starvation_levels,
            "counts": {
                "severe": severe_count,
                "moderate": moderate_count,
                "acceptable": acceptable_count
            },
            "total_strategies": len(starvation_levels)
        }
    
    def _calculate_priority_impact(self, router_summary: pd.DataFrame) -> Dict:
        """Calculate priority impact metrics."""
        if router_summary.empty:
            return {}
        
        priority_impact = []
        for _, row in router_summary.iterrows():
            if row['winner'] > 0:
                impact = row['shadowed'] / row['winner']
            else:
                impact = 0.0
            
            priority_impact.append({
                "strategy_name": row['strategy_name'],
                "priority_impact": impact,
                "shadowed_count": row['shadowed'],
                "winner_count": row['winner']
            })
        
        # Sort by impact
        priority_impact.sort(key=lambda x: x['priority_impact'], reverse=True)
        
        return {
            "by_strategy": priority_impact,
            "highest_impact": priority_impact[0] if priority_impact else None,
            "average_impact": np.mean([p['priority_impact'] for p in priority_impact]) if priority_impact else 0.0
        }
    
    def generate_alerts(self, metrics: Dict) -> List[Dict]:
        """Generate alerts based on attribution metrics."""
        alerts = []
        
        # Starvation alerts
        starvation = metrics.get('starvation_analysis', {})
        for strategy in starvation.get('by_strategy', []):
            if strategy['level'] == 'severe':
                alerts.append({
                    "type": "starvation",
                    "level": "critical",
                    "strategy": strategy['strategy_name'],
                    "message": f"策略 {strategy['strategy_name']} 嚴重飢餓 (index={strategy['starvation_index']:.2f})",
                    "details": f"評估率僅 {1-strategy['starvation_index']:.1%}，被 shadowed {strategy['shadowed_count']} 次"
                })
            elif strategy['level'] == 'moderate':
                alerts.append({
                    "type": "starvation",
                    "level": "warning",
                    "strategy": strategy['strategy_name'],
                    "message": f"策略 {strategy['strategy_name']} 中度飢餓 (index={strategy['starvation_index']:.2f})",
                    "details": f"評估率 {1-strategy['starvation_index']:.1%}，建議調整優先級"
                })
        
        # Priority impact alerts
        priority_impact = metrics.get('priority_impact', {})
        for strategy in priority_impact.get('by_strategy', []):
            if strategy['priority_impact'] > 2.0:
                alerts.append({
                    "type": "priority_impact",
                    "level": "warning",
                    "strategy": strategy['strategy_name'],
                    "message": f"策略 {strategy['strategy_name']} 高優先級壓制 (impact={strategy['priority_impact']:.1f})",
                    "details": f"每贏 1 次被壓制 {strategy['priority_impact']:.1f} 次"
                })
        
        # Low evaluation alerts
        router_summary = metrics.get('router_summary', [])
        for strategy in router_summary:
            if strategy['evaluated'] < 10 and strategy['candidate'] > 100:
                alerts.append({
                    "type": "low_evaluation",
                    "level": "info",
                    "strategy": strategy['strategy_name'],
                    "message": f"策略 {strategy['strategy_name']} 評估次數過低",
                    "details": f"候選 {strategy['candidate']} 次，僅評估 {strategy['evaluated']} 次"
                })
        
        return alerts


def render_attribution_dashboard(attribution_dir: Path):
    """Render attribution dashboard in Streamlit."""
    st.title("📊 Attribution & Starvation Analysis")
    
    # Initialize dashboard
    dashboard = AttributionDashboard(attribution_dir)
    
    # Load data
    with st.spinner("載入 attribution 數據..."):
        router_df, signal_df, trade_df = dashboard.load_data()
    
    if router_df.empty:
        st.warning("未找到 attribution 數據。請確保 router 已啟用 attribution 記錄。")
        st.info("啟用 attribution: 在 router 呼叫中傳遞 `AttributionRecorder` 實例")
        return
    
    # Calculate metrics
    metrics = dashboard.calculate_summary_metrics(router_df, trade_df)
    
    # Display summary
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("總 Bars", metrics.get('total_bars', 0))
    with col2:
        st.metric("總交易", metrics.get('total_trades', 0))
    with col3:
        st.metric("總 PnL", f"${metrics.get('total_pnl', 0):.2f}")
    with col4:
        st.metric("策略數量", len(metrics.get('router_summary', [])))
    
    # Tabs
    tab1, tab2, tab3, tab4 = st.tabs([
        "📈 Router 統計", 
        "⚠️ 飢餓分析", 
        "🎯 優先級影響",
        "🚨 警報"
    ])
    
    with tab1:
        _render_router_stats(metrics)
    
    with tab2:
        _render_starvation_analysis(metrics)
    
    with tab3:
        _render_priority_impact(metrics)
    
    with tab4:
        _render_alerts(dashboard, metrics)
    
    # Raw data
    with st.expander("📁 原始數據"):
        st.subheader("Router Evaluation Log")
        st.dataframe(router_df.head(100))
        
        if not trade_df.empty:
            st.subheader("Trade Attribution Log")
            st.dataframe(trade_df.head(100))


def _render_router_stats(metrics: Dict):
    """Render router statistics."""
    router_summary = metrics.get('router_summary', [])
    
    if not router_summary:
        st.info("無 router 統計數據")
        return
    
    # Convert to DataFrame
    df = pd.DataFrame(router_summary)
    
    # Display metrics
    st.subheader("策略曝光統計")
    st.dataframe(df[[
        'strategy_name', 'candidate', 'evaluated', 'winner', 
        'shadowed', 'evaluation_rate', 'starvation_index'
    ]].round(4))
    
    # Charts
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("候選次數分布")
        fig = go.Figure(data=[
            go.Bar(x=df['strategy_name'], y=df['candidate'], name='候選')
        ])
        fig.update_layout(height=300)
        st.plotly_chart(fig, use_container_width=True)
    
    with col2:
        st.subheader("評估率")
        fig = go.Figure(data=[
            go.Bar(x=df['strategy_name'], y=df['evaluation_rate'], name='評估率')
        ])
        fig.update_layout(height=300)
        st.plotly_chart(fig, use_container_width=True)


def _render_starvation_analysis(metrics: Dict):
    """Render starvation analysis."""
    starvation = metrics.get('starvation_analysis', {})
    
    if not starvation.get('by_strategy'):
        st.info("無飢餓分析數據")
        return
    
    # Display starvation levels
    st.subheader("飢餓等級分布")
    
    counts = starvation.get('counts', {})
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("嚴重", counts.get('severe', 0), delta=None, delta_color="off")
    with col2:
        st.metric("中度", counts.get('moderate', 0), delta=None, delta_color="off")
    with col3:
        st.metric("可接受", counts.get('acceptable', 0), delta=None, delta_color="off")
    
    # Strategy details
    st.subheader("策略飢餓詳情")
    df = pd.DataFrame(starvation['by_strategy'])
    st.dataframe(df[[
        'strategy_name', 'starvation_index', 'level', 
        'eval_count', 'shadowed_count'
    ]].round(4))
    
    # Starvation index chart
    st.subheader("飢餓指數圖表")
    fig = go.Figure()
    
    # Color by level
    colors = {
        'severe': 'red',
        'moderate': 'orange',
        'acceptable': 'green'
    }
    
    for level in ['severe', 'moderate', 'acceptable']:
        level_df = df[df['level'] == level]
        if not level_df.empty:
            fig.add_trace(go.Bar(
                x=level_df['strategy_name'],
                y=level_df['starvation_index'],
                name=level,
                marker_color=colors[level]
            ))
    
    fig.update_layout(
        title="Starvation Index by Strategy",
        yaxis_title="Starvation Index (1.0 = never evaluated)",
        height=400
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_priority_impact(metrics: Dict):
    """Render priority impact analysis."""
    priority_impact = metrics.get('priority_impact', {})
    
    if not priority_impact.get('by_strategy'):
        st.info("無優先級影響數據")
        return
    
    # Display highest impact
    highest = priority_impact.get('highest_impact')
    if highest:
        st.subheader("最高優先級影響")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("策略", highest['strategy_name'])
        with col2:
            st.metric("影響指數", f"{highest['priority_impact']:.2f}")
        with col3:
            st.metric("被壓制次數", highest['shadowed_count'])
    
    # All strategies
    st.subheader("優先級影響詳情")
    df = pd.DataFrame(priority_impact['by_strategy'])
    st.dataframe(df[[
        'strategy_name', 'priority_impact', 
        'shadowed_count', 'winner_count'
    ]].round(4))
    
    # Priority impact chart
    st.subheader("優先級影響圖表")
    fig = go.Figure(data=[
        go.Bar(x=df['strategy_name'], y=df['priority_impact'])
    ])
    fig.update_layout(
        title="Priority Impact by Strategy",
        yaxis_title="Priority Impact (shadowed/winner)",
        height=400
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_alerts(dashboard: AttributionDashboard, metrics: Dict):
    """Render alerts."""
    alerts = dashboard.generate_alerts(metrics)
    
    if not alerts:
        st.success("✅ 無警報 - 所有策略運作正常")
        return
    
    # Group by level
    critical_alerts = [a for a in alerts if a['level'] == 'critical']
    warning_alerts = [a for a in alerts if a['level'] == 'warning']
    info_alerts = [a for a in alerts if a['level'] == 'info']
    
    # Display critical alerts
    if critical_alerts:
        st.error("🚨 嚴重警報")
        for alert in critical_alerts:
            with st.expander(f"{alert['message']}", expanded=True):
                st.write(alert['details'])
                st.write(f"類型: {alert['type']}")
    
    # Display warning alerts
    if warning_alerts:
        st.warning("⚠️ 警告")
        for alert in warning_alerts:
            with st.expander(f"{alert['message']}"):
                st.write(alert['details'])
                st.write(f"類型: {alert['type']}")
    
    # Display info alerts
    if info_alerts:
        st.info("ℹ️ 資訊")
        for alert in info_alerts:
            with st.expander(f"{alert['message']}"):
                st.write(alert['details'])
                st.write(f"類型: {alert['type']}")


# For testing
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        attribution_dir = Path(sys.argv[1])
    else:
        attribution_dir = Path("./data/attribution")
    
    # Simple CLI output
    dashboard = AttributionDashboard(attribution_dir)
    router_df, signal_df, trade_df = dashboard.load_data()
    
    if router_df.empty:
        print("No attribution data found")
        sys.exit(1)
    
    metrics = dashboard.calculate_summary_metrics(router_df, trade_df)
    alerts = dashboard.generate_alerts(metrics)
    
    print(f"Total bars: {metrics.get('total_bars', 0)}")
    print(f"Total trades: {metrics.get('total_trades', 0)}")
    print(f"Total PnL: ${metrics.get('total_pnl', 0):.2f}")
    print(f"Strategies: {len(metrics.get('router_summary', []))}")
    
    if alerts:
        print(f"\nAlerts: {len(alerts)}")
        for alert in alerts:
            print(f"  [{alert['level'].upper()}] {alert['message']}")
    else:
        print("\nNo alerts")