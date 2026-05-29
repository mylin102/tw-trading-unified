#!/usr/bin/env python3
"""
weak_bear_trend 監控面板 - Dashboard 組件
顯示 auto_select 狀態、Regime、策略選擇、進場信號
"""
import streamlit as st
import pandas as pd
import yaml
from pathlib import Path
from datetime import datetime

# 配置文件路徑
CONFIG_PATH = Path(__file__).parent.parent / "config"
FUTURES_NIGHT_CFG = CONFIG_PATH / "futures_night.yaml"
WEAK_BEAR_CFG = CONFIG_PATH / "strategies" / "weak_bear_trend.yaml"


def load_config(path):
    """載入 YAML 配置文件."""
    if not path.exists():
        return {}
    with open(path, 'r') as f:
        return yaml.safe_load(f)


def parse_router_trace(log_path, lines=50):
    """解析 Router Trace 日誌."""
    if not log_path.exists():
        return []
    
    traces = []
    with open(log_path, 'r') as f:
        for line in f.readlines()[-lines:]:
            if '[RouterTrace]' in line:
                # 解析：ts= regime=SQUEEZE selected=None | adaptive_orb_v15=SKIP:...
                parts = line.split('|')
                if len(parts) >= 2:
                    header = parts[0].strip()
                    # 提取 regime 和 selected
                    regime = "?"
                    selected = "?"
                    if 'regime=' in header:
                        regime = header.split('regime=')[1].split()[0]
                    if 'selected=' in header:
                        selected = header.split('selected=')[1].split()[0]
                    
                    # 解析策略評估
                    strategies = []
                    for part in parts[1:]:
                        part = part.strip()
                        if '=' in part:
                            strat_name = part.split('=')[0].strip()
                            status = part.split('=')[1].split()[0].strip()
                            strategies.append({
                                'name': strat_name,
                                'status': status,
                                'detail': part
                            })
                    
                    traces.append({
                        'regime': regime,
                        'selected': selected,
                        'strategies': strategies,
                        'timestamp': datetime.now()
                    })
    
    return traces


def get_regime_strategy_mapping():
    """返回 Regime → 策略映射表."""
    return {
        'WEAK': {
            'SHORT': ['weak_bear_trend', 'counter_vwap', 'spring_upthrust'],
            'LONG': ['counter_vwap', 'spring_upthrust'],
            'NEUTRAL': ['range_mean_reversion_v1', 'kbar_feature']
        },
        'SQUEEZE': {
            'ANY': ['squeeze_fire_scout', 'range_mean_reversion_v1']
        },
        'TREND': {
            'LONG': ['adaptive_orb_v15', 'trend_continuation_v1'],
            'SHORT': ['adaptive_orb']
        },
        'CHOP': {
            'ANY': ['counter_vwap', 'calendar_condor_v2']
        }
    }


def render_weak_bear_monitor():
    """渲染 weak_bear_trend 監控面板."""
    st.title("🤖 auto_select 監控中心")
    st.caption("Regime 驅動的自動策略選擇系統")
    
    # 載入配置
    night_cfg = load_config(FUTURES_NIGHT_CFG)
    weak_bear_cfg = load_config(WEAK_BEAR_CFG)
    
    # ── 1. 配置狀態 ──
    st.subheader("⚙️ 配置狀態")
    
    auto_select = night_cfg.get('auto_select', False)
    active_strategy = night_cfg.get('active_strategy', '未設置')
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        if auto_select:
            st.success("✅ auto_select = true")
            st.caption("Regime 自動選擇策略")
        else:
            st.error("❌ auto_select = false")
            st.caption("手動選擇策略")
    
    with col2:
        if active_strategy is None or active_strategy == 'null':
            st.info("ℹ️ active_strategy = null")
            st.caption("不強制指定策略")
        else:
            st.warning(f"⚠️ active_strategy = {active_strategy}")
            st.caption("強制使用單一策略")
    
    with col3:
        live_trading = night_cfg.get('live_trading', False)
        if live_trading:
            st.error("🔴 LIVE TRADING")
        else:
            st.success("🟢 Paper Trading")
    
    # ── 2. 微台指設定 ──
    st.subheader("📊 微台指 (TMF) 設定")
    
    exec_cfg = night_cfg.get('execution', {})
    trade_cfg = night_cfg.get('trade_mgmt', {})
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("一點價值", f"{exec_cfg.get('point_value', 10)} 元")
    col2.metric("初始資金", f"{exec_cfg.get('initial_balance', 50000):,.0f} 元")
    col3.metric("每筆口數", trade_cfg.get('lots_per_trade', 2))
    col4.metric("最大持倉", trade_cfg.get('max_positions', 2))
    
    # ── 3. weak_bear_trend 參數 ──
    st.subheader("🎯 weak_bear_trend 參數")
    
    params = weak_bear_cfg.get('params', {})
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("##### 止損/止盈")
        st.metric("止損倍數", f"{params.get('stop_atr_mult', 1.0)} ATR")
        st.metric("止盈倍數", f"{params.get('take_profit_atr_mult', 2.0)} ATR")
        
        profit_loss_ratio = params.get('take_profit_atr_mult', 2.0) / params.get('stop_atr_mult', 1.0)
        st.metric("盈虧比", f"{profit_loss_ratio:.2f}:1")
        
        breakeven_winrate = 1 / (1 + profit_loss_ratio) * 100
        st.caption(f"平衡勝率：{breakeven_winrate:.1f}%")
    
    with col2:
        st.markdown("##### 進場門檻")
        st.metric("VWAP 距離", f"< {params.get('max_vwap_dist_atr', 0.5)} ATR")
        st.metric("動能門檻", f"< {params.get('min_mom_velo_bearish', -8.0)}")
        st.metric("ADX 上限", f"< {params.get('max_adx', 20.0)}")
        st.metric("時間止損", f"{params.get('time_stop_minutes', 15)} 分鐘")
    
    # ── 4. Regime → 策略映射 ──
    st.subheader("🧠 Regime → 策略映射")
    
    mapping = get_regime_strategy_mapping()
    
    for regime, biases in mapping.items():
        with st.expander(f"**{regime}** Regime"):
            for bias, strategies in biases.items():
                st.caption(f"Bias: {bias}")
                for i, strat in enumerate(strategies):
                    if i == 0:
                        st.write(f"🥇 **{strat}** (首選)")
                    elif i == 1:
                        st.write(f"🥈 {strat} (備選)")
                    else:
                        st.write(f"🥉 {strat}")
    
    # ── 5. 即時 Router Trace ──
    st.subheader("📡 即時 Router Trace")
    
    log_path = Path(__file__).parent.parent / "logs" / "pm2-trading-out-11.log"
    traces = parse_router_trace(log_path)
    
    if traces:
        latest = traces[-1]
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.metric("當前 Regime", latest['regime'])
            st.metric("選擇策略", latest['selected'])
        
        with col2:
            st.caption("候選策略評估:")
            for strat in latest['strategies']:
                if 'SKIP' in strat['status']:
                    st.error(f"❌ {strat['name']}: {strat['status']}")
                elif 'ALLOW' in strat['status']:
                    st.success(f"✅ {strat['name']}: {strat['status']}")
                else:
                    st.info(f"ℹ️ {strat['name']}: {strat['status']}")
        
        # 歷史 Trace
        st.divider()
        st.caption("最近 10 次 Router 決策:")
        
        trace_data = []
        for t in traces[-10:]:
            for strat in t['strategies']:
                trace_data.append({
                    '時間': t['timestamp'].strftime('%H:%M:%S'),
                    'Regime': t['regime'],
                    '策略': strat['name'],
                    '狀態': strat['status'],
                    '選擇': '✅' if t['selected'] == strat['name'] else ''
                })
        
        df_trace = pd.DataFrame(trace_data)
        st.dataframe(df_trace, hide_index=True, use_container_width=True)
    
    else:
        st.info("⏳ 等待 Router Trace 數據...")
        st.caption("請確保交易系統正在運行")
    
    # ── 6. 預期行為 ──
    st.subheader("🎯 預期行為")
    
    st.info("""
    **當前狀態**: SQUEEZE Regime
    
    **等待轉換**: SQUEEZE → WEAK + SHORT
    
    **觸發條件**:
    - ADX 上升至 15-20 區間
    - 價格震盪格局
    - Bias 維持 SHORT
    
    **自動啟動**: weak_bear_trend
    
    **進場信號**:
    - 弱勢反彈失敗後做空
    - 止損：1.0 ATR (50 點)
    - 止盈：2.0 ATR (100 點)
    - 盈虧比：2:1
    """)
    
    # ── 7. 監控清單 ──
    st.subheader("✅ 監控清單")
    
    st.markdown("""
    - [ ] auto_select = true ✅
    - [ ] active_strategy = null ✅
    - [ ] Paper Trading 模式 ✅
    - [ ] 等待 WEAK + Short Regime ⏳
    - [ ] weak_bear_trend 進場 ⏳
    - [ ] 盈虧比 2:1 驗證 ⏳
    """)


if __name__ == "__main__":
    render_weak_bear_monitor()
