#!/usr/bin/env python3
"""
Squeeze Futures 即時交易儀表板
使用 Streamlit 建立即時更新的監控介面

執行：
  streamlit run src/squeeze_futures/ui/dashboard.py
"""

import streamlit as st
import pandas as pd
import numpy as np
import yaml
from pathlib import Path

# 嘗試載入 Shioaji
try:
    import shioaji as sj
    HAS_SHIOAJI = True
except:
    HAS_SHIOAJI = False
from datetime import datetime, timedelta
from pathlib import Path
import time
import re
import os
import yaml

# 頁面配置
st.set_page_config(
    page_title="Squeeze Futures 交易監控",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 樣式
st.markdown("""
<style>
    .metric-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 20px;
        border-radius: 10px;
        color: white;
        margin-bottom: 20px;
    }
    .profit { color: #00cc00; font-weight: bold; }
    .loss { color: #ff0000; font-weight: bold; }
    .neutral { color: #666666; }
</style>
""", unsafe_allow_html=True)

# 路徑設定
LOG_FILE = Path("logs/automation.log")
MARKET_DIR = Path("logs/market_data")
BACKTEST_DIR = Path("data/backtest")


# 快取函數
@st.cache_data(ttl=1)
def load_market_data(date: str = None):
    """載入市場數據"""
    if date is None:
        # 自動尋找最新的數據檔案
        if not MARKET_DIR.exists():
            return None
        
        all_files = list(MARKET_DIR.glob("TMF_*.csv"))
        if not all_files:
            return None
        
        latest_file = max(all_files, key=os.path.getmtime)
        date = latest_file.stem.replace("TMF_", "")
    
    # 嘗試載入指定日期或最新數據
    pattern = f"TMF_{date}*.csv"
    files = list(MARKET_DIR.glob(pattern))
    
    if not files:
        # 如果找不到，嘗試載入最新的
        all_files = list(MARKET_DIR.glob("TMF_*.csv"))
        if all_files:
            files = [max(all_files, key=os.path.getmtime)]
    
    if not files:
        return None
    
    try:
        latest_file = max(files, key=os.path.getmtime)
        df = pd.read_csv(latest_file, index_col=0, parse_dates=True)
        df = df.sort_index()
        df = df.drop_duplicates(keep='last')
        return df
    except Exception as e:
        st.error(f"載入數據失敗：{e}")
        return None


@st.cache_data(ttl=1)
def parse_trade_log(date: str = None):
    """解析交易日誌"""
    # 始終使用今日日期
    date = datetime.now().strftime("%Y-%m-%d")
    
    trades = []
    if LOG_FILE.exists():
        with open(LOG_FILE, 'r') as f:
            for line in f:
                if True:  # Date check disabled for testing
                    try:
                        time_part = line.split(']')[0].replace('[', '')
                        
                        if 'PARTIAL_EXIT' in line:
                            trade_type = 'PARTIAL'
                        elif 'EXIT' in line:
                            trade_type = 'EXIT'
                        elif 'BUY' in line:
                            trade_type = 'BUY'
                        elif 'SELL' in line:
                            trade_type = 'SELL'
                        else:
                            continue
                        
                        price_match = re.search(r'at ([\d.]+)', line)
                        pnl_match = re.search(r'PnL: ([\d,-]+)', line)
                        
                        price = float(price_match.group(1)) if price_match else 0
                        pnl = float(pnl_match.group(1).replace(',', '')) if pnl_match else 0
                        
                        trades.append({
                            'timestamp': time_part,
                            'type': trade_type,
                            'price': price,
                            'pnl': pnl,
                        })
                    except:
                        pass
    
    return pd.DataFrame(trades) if trades else pd.DataFrame()


def calculate_metrics(trades_df: pd.DataFrame):
    """計算績效指標 - LIVE TRADING 只顯示實際權益"""
    # 統一使用 display_initial 變數
    display_initial = 40000  # 預設值
    
    # 讀取配置
    try:
        config_file = Path("config/trade_config.yaml")
        if config_file.exists():
            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
                display_initial = config.get('execution', {}).get('initial_balance', 40000)
    except:
        pass
    
    if trades_df is None or trades_df.empty:
        return {
            'total_pnl': 0,
            'total_trades': 0,
            'win_rate': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'profit_factor': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'initial_balance': display_initial,
            'current_equity': display_initial,
        }
    
    exits = trades_df[trades_df['type'].isin(['EXIT', 'PARTIAL'])]
    
    if exits.empty:
        return {
            'total_pnl': 0,
            'total_trades': 0,
            'win_rate': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'profit_factor': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'initial_balance': display_initial,
            'current_equity': display_initial,
        }
    
    total_pnl = exits['pnl'].sum()
    winning = exits[exits['pnl'] > 0]
    losing = exits[exits['pnl'] < 0]
    
    win_rate = len(winning) / len(exits) * 100 if len(exits) > 0 else 0
    avg_win = winning['pnl'].mean() if len(winning) > 0 else 0
    avg_loss = abs(losing['pnl'].mean()) if len(losing) > 0 else 0
    
    gross_profit = winning['pnl'].sum() if len(winning) > 0 else 0
    gross_loss = abs(losing['pnl'].sum()) if len(losing) > 0 else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
    
    return {
        'total_pnl': total_pnl,
        'total_trades': len(exits),
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': profit_factor,
        'winning_trades': len(winning),
        'losing_trades': len(losing),
        'initial_balance': display_initial,
        'current_equity': display_initial + total_pnl,
    }


# 讀取交易模式 (在側邊欄之前定義)
is_live = False
try:
    config_file = Path("config/trade_config.yaml")
    if config_file.exists():
        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            is_live = config.get('live_trading', False)
except:
    pass

# 側邊欄
with st.sidebar:
    st.header("⚙️ 設定")
    
    # 日期選擇
    selected_date = st.date_input(
        "交易日期",
        value=datetime.now(),
        format="YYYY-MM-DD"
    )
    
    # 自動刷新 (預設關閉)
    auto_refresh = st.checkbox("自動更新", value=False)
    if auto_refresh:
        refresh_interval = st.slider("更新間隔 (秒)", 10, 60, 30)
    
    # 手動刷新
    if st.button("🔄 立即刷新"):
        st.cache_data.clear()
        st.rerun()
    
    # 自動刷新按鈕
    st.write("**自動刷新：**")
    if st.checkbox("啟用自動刷新 (10 秒)", value=False):
        import time
        time.sleep(0.1)
        st.rerun()
    
    st.divider()
    
    # 顯示當前策略參數
    st.header("📋 策略參數")
    
    config_file = Path("config/trade_config.yaml")
    if config_file.exists():
        try:
            import yaml
            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            st.subheader("進場條件")
            st.write(f"**Entry Score**: ≥ {config['strategy']['entry_score']}")
            st.write(f"**Regime Filter**: {config['strategy']['regime_filter']}")
            st.write(f"**使用 Squeeze**: {config['strategy']['use_squeeze']}")
            st.write(f"**使用 Pullback**: {config['strategy']['use_pullback']}")
            
            st.subheader("停損/停利")
            st.write(f"**停損點數**: {config['risk_mgmt']['stop_loss_pts']} pts")
            st.write(f"**停利點數**: {config['strategy']['partial_exit']['tp1_pts']} pts")
            st.write(f"**VWAP 離場**: {config['risk_mgmt']['exit_on_vwap']}")
            
            st.subheader("部位管理")
            st.write(f"**每筆口數**: {config['trade_mgmt']['lots_per_trade']} 口")
            st.write(f"**最大部位**: {config['trade_mgmt']['max_positions']} 口")
            
            st.subheader("移動停損")
            if config['risk_mgmt'].get('trailing_stop_enabled', False):
                st.write(f"**觸發點數**: {config['risk_mgmt']['trailing_stop_trigger_pts']} pts")
                st.write(f"**距離**: {config['risk_mgmt']['trailing_stop_distance_pts']} pts")
            else:
                st.write("❌ 未啟用")
            
        except Exception as e:
            st.error(f"載入配置失敗：{e}")
    else:
        st.warning("找不到配置文件")
    
    st.divider()
    
    # 重置功能 (僅 PAPER TRADING 顯示)
    if not is_live:
        st.subheader("🔄 系統重置 (PAPER 模式)")
        
        # 期初資金輸入
        new_initial = st.number_input(
            "期初資金 (TWD)",
            min_value=10000,
            max_value=10000000,
            value=40000,
            step=10000,
            help="LIVE TRADING 模式不顯示此功能"
        )
        
        # 重置按鈕
        if st.button("🗑️ 清空記錄"):
            from datetime import datetime
            today = datetime.now().strftime("%Y%m%d")
            
            # 清空檔案
            files = [
                f"logs/market_data/TMF_{today}_indicators.csv",
                f"exports/trades/TMF_{today}_trades.json",
                f"exports/trades/TMF_{today}_trades.csv",
            ]
            for f in files:
                try:
                    Path(f).unlink(missing_ok=True)
                except:
                    pass
            
            # 清空日誌
            try:
                with open("logs/automation.log", 'r') as f:
                    lines = f.readlines()
                with open("logs/automation.log", 'w') as f:
                    for line in lines:
                        if today not in line:
                            f.write(line)
            except:
                pass
            
            st.success("✅ 已清空！請按 F5 刷新頁面")
            st.cache_data.clear()
        
        st.info("ℹ️ LIVE TRADING 模式不顯示重置功能")
        st.divider()
    else:
        st.divider()
        st.success("✅ LIVE TRADING 模式 - 使用實際帳戶權益")
    
    # 快速連結
    st.subheader("🔗 快速連結")
    st.markdown("""
    - [📊 市場數據](#market-data)
    - [📈 持倉狀態](#position)
    - [💰 績效統計](#performance)
    - [📝 交易記錄](#trades)
    """)


# 主標題
st.title("📊 Squeeze Futures 即時交易儀表板")
live_mode = False
try:
    config = yaml.safe_load(Path("config/trade_config.yaml").open())
    live_mode = config.get('live_trading', False)
except:
    pass
if live_mode:
    st.success("✅ 實際交易模式")
else:
    st.warning("⚠️ 模擬交易模式")
st.markdown(f"**交易日期**: {selected_date.strftime('%Y-%m-%d')} | **更新時間**: {datetime.now().strftime('%H:%M:%S')}")

st.divider()

# 載入數據
date_str = selected_date.strftime("%Y%m%d")
market_df = load_market_data(date_str)
trades_df = parse_trade_log(date_str)
metrics = calculate_metrics(trades_df)

# 第一列：關鍵指標
col1, col2, col3, col4 = st.columns(4)

with col1:
    pnl_color = "profit" if metrics['total_pnl'] > 0 else "loss" if metrics['total_pnl'] < 0 else "neutral"
    st.metric(
        label="💰 總 PnL (TWD)",
        value=f"{metrics['total_pnl']:+,.0f}",
        delta=None,
    )

with col2:
    st.metric(
        label="📊 交易次數",
        value=metrics['total_trades'],
        delta=None,
    )

with col3:
    win_delta = f"{metrics['win_rate']:.1f}%" if metrics['total_trades'] > 0 else "0%"
    st.metric(
        label="🎯 勝率",
        value=win_delta,
        delta=None,
    )

with col4:
    pf_delta = f"{metrics['profit_factor']:.2f}" if metrics['profit_factor'] > 0 else "0.00"
    st.metric(
        label="📈 盈虧比",
        value=pf_delta,
        delta=None,
    )

st.divider()

# 第二列：市場數據和持倉
col1, col2 = st.columns(2)

with col1:
    st.subheader("📊 市場數據")
    
    if market_df is not None and not market_df.empty:
        latest = market_df.iloc[-1]
        
        # 關鍵數據
        c1, c2, c3 = st.columns(3)
        c1.metric("價格", f"{latest.get('close', 0):.0f}")
        c2.metric("VWAP", f"{latest.get('vwap', latest.get('close', 0)):.0f}")
        c3.metric("Score", str(latest.get("score", "N/A")))
        
        # Squeeze 狀態
        sqz_on = latest.get('sqz_on', False)
        mom_state = latest.get('mom_state', 0)
        
        if sqz_on:
            st.info("⏸️ Squeeze: **ON** (壓縮中)")
        else:
            st.success("▶️ Squeeze: **OFF** (釋放中)")
        
        st.write(f"**動能狀態**: {mom_state}")
        
        # 價格走勢圖
        if len(market_df) > 1:
            fig = {
                "data": [
                    {"x": market_df.index.tolist(), "y": market_df['close'].tolist(), "type": "scatter", "name": "Close"},
                    {"x": market_df.index.tolist(), "y": market_df.get('vwap', market_df['close']).tolist(), "type": "scatter", "name": "VWAP", "line": {"dash": "dash"}},
                ],
                "layout": {"title": "價格走勢", "xaxis": {"title": "時間"}, "yaxis": {"title": "價格"}}
            }
            st.plotly_chart(fig, use_container_width=True)
        
        # Score 走勢
        if 'score' in market_df.columns:
            fig = {
                "data": [
                    {"x": market_df.index.tolist(), "y": market_df['score'].tolist(), "type": "scatter", "name": "Score"},
                ],
                "layout": {
                    "title": "MTF Score",
                    "xaxis": {"title": "時間"},
                    "yaxis": {"title": "Score"},
                    "shapes": [
                        {"type": "line", "x0": market_df.index[0], "y0": 50, "x1": market_df.index[-1], "y1": 50, "line": {"color": "green", "dash": "dash"}},
                        {"type": "line", "x0": market_df.index[0], "y0": -50, "x1": market_df.index[-1], "y1": -50, "line": {"color": "red", "dash": "dash"}},
                    ]
                }
            }
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("⚠️ 暫無市場數據")

with col2:
    st.subheader("📝 持倉狀態")
    
    if not trades_df.empty:
        # 查找最新進場和平倉
        buys = trades_df[trades_df['type'] == 'BUY']
        exits = trades_df[trades_df['type'].isin(['EXIT', 'PARTIAL'])]
        
        if not buys.empty and not exits.empty:
            last_buy_time = buys.iloc[-1]['timestamp']
            last_exit_time = exits.iloc[-1]['timestamp']
            
            if last_exit_time > last_buy_time:
                st.success("✅ 目前無持倉")
            else:
                st.warning("🟡 持有部位中...")
                st.write(f"**進場時間**: {last_buy_time}")
                st.write(f"**進場價格**: {buys.iloc[-1]['price']:.0f}")
        elif not buys.empty:
            st.warning("🟡 持有部位中...")
            st.write(f"**進場時間**: {buys.iloc[-1]['timestamp']}")
            st.write(f"**進場價格**: {buys.iloc[-1]['price']:.0f}")
        else:
            st.success("✅ 目前無持倉")
    else:
        st.success("✅ 目前無持倉")
    
    st.divider()
    
    # 績效統計
    st.subheader("💰 績效統計")
    
    # 顯示期初資金和實際權益 (LIVE TRADING)
    col1, col2 = st.columns(2)
    with col1:
        st.metric(
            label="期初資金",
            value=f"{metrics.get('initial_balance', 0):,.0f} TWD",
            delta=None
        )
    with col2:
        current = metrics.get('current_equity', metrics.get('initial_balance', 0))
        pnl = metrics.get('total_pnl', 0)
        st.metric(
            label="實際帳戶權益",
            value=f"{current:,.0f} TWD",
            delta=f"{pnl:+,.0f} TWD" if pnl != 0 else None
        )
    
    st.divider()
    
    if metrics['total_trades'] > 0:
        col3, col4 = st.columns(2)
        with col3:
            st.write(f"**獲利次數**: {metrics.get('winning_trades', 0)}")
            st.write(f"**平均獲利**: {metrics.get('avg_win', 0):+,.0f} TWD")
        with col4:
            st.write(f"**虧損次數**: {metrics.get('losing_trades', 0)}")
            st.write(f"**平均虧損**: {metrics.get('avg_loss', 0):+,.0f} TWD")
    else:
        st.info("今日尚無交易")

st.divider()

# 第三列：交易記錄
st.subheader("📝 交易記錄")

if not trades_df.empty:
    # 格式化顯示
    display_df = trades_df.copy()
    display_df['pnl_display'] = display_df['pnl'].apply(lambda x: f"{x:+,.0f}")
    display_df['type_display'] = display_df['type'].apply(
        lambda x: "🟢 BUY" if x == 'BUY' else "🔴 SELL" if x == 'SELL' else "⚪ EXIT" if x == 'EXIT' else "⚪ PARTIAL"
    )
    
    st.dataframe(
        display_df[['timestamp', 'type_display', 'price', 'pnl_display']],
        use_container_width=True,
        hide_index=True,
        column_config={
            "timestamp": "時間",
            "type_display": "類型",
            "price": "價格",
            "pnl_display": "PnL",
        }
    )
else:
    st.info("今日尚無交易記錄")

# 底部說明
st.divider()
st.markdown("""
**說明**:
- 數據更新：點擊側邊欄 🔄 立即刷新 按鈕
- 自動更新：預設關閉 (避免連接錯誤)
- 數據來源：`logs/market_data/` 和 `logs/automation.log`
""")

# 不自動刷新，避免 connection error
# if auto_refresh:
#     time.sleep(refresh_interval)
#     st.rerun()
