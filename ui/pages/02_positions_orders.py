# 2026-07-31 Antigravity: Multipage Positions & Orders View
import sys
import os
import json
import time
import pandas as pd
from pathlib import Path
import streamlit as st

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="Positions & Orders | Trading Unified", page_icon="📋", layout="wide")

st.title("📋 持倉與委託單 (Positions & Orders)")
st.caption("即時部位與歷史委託單明細視圖")

# 1. Open Positions
st.subheader("📦 即時部位 (Active Positions)")
pos_file = Path("/tmp/mts_position_state.json")

if pos_file.exists():
    try:
        with open(pos_file) as f:
            ps = json.load(f)
            
        has_pos = ps.get("has_position", False)
        state = ps.get("state", "FLAT")
        reason = ps.get("reason", "—")
        
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("持倉狀態", "🟢 HELD" if has_pos else "⚪ FLAT", state)
        c2.metric("近月進場/現價", f"{ps.get('near_entry', 0):.0f} / {ps.get('near_last', 0):.0f}" if has_pos else "—")
        c3.metric("遠月進場/現價", f"{ps.get('far_entry', 0):.0f} / {ps.get('far_last', 0):.0f}" if has_pos else "—")
        c4.metric("未實現損益 (UPL)", f"{ps.get('total_upl', 0):+,.0f} TWD")
        
        if has_pos:
            st.info(f"💡 當前階段: `{state}` | 原因: `{reason}` | ATR: `{ps.get('atr', 0):.1f}` | 更新時間: `{str(ps.get('_updated', ''))[:19]}`")
        else:
            st.caption(f"目前無持倉 (FLAT) | 最後心跳: `{str(ps.get('heartbeat_at', ''))[:19]}`")
    except Exception as e:
        st.error(f"讀取持倉狀態失敗: {e}")
else:
    st.info("尚無持倉快照資料 (System FLAT)")

st.divider()

# 2. Orders & Fills
st.subheader("📤 歷史成交與委託明細 (Trades & Fills Ledger)")
fills_file = ROOT / "logs" / "mts_trade_fills.jsonl"

if fills_file.exists():
    try:
        fills_data = []
        with open(fills_file) as f:
            for line in f:
                if line.strip():
                    try:
                        fills_data.append(json.loads(line.strip()))
                    except Exception:
                        pass
                        
        if fills_data:
            df_fills = pd.DataFrame(fills_data)
            
            # Reorder columns
            cols = ["timestamp", "trade_id", "fill_type", "leg", "side", "qty", "price", "session"]
            available_cols = [c for c in cols if c in df_fills.columns]
            
            # Display latest 50 fills
            st.dataframe(df_fills[available_cols].tail(50).iloc[::-1], width='stretch', hide_index=True)
            
            st.caption(f"顯示最新 50 筆成交紀錄 (總計 {len(fills_data)} 筆紀錄)")
        else:
            st.info("成交紀錄日誌為空")
    except Exception as e:
        st.error(f"載入成交明細失敗: {e}")
else:
    st.info("成交紀錄檔尚未建立 (`logs/mts_trade_fills.jsonl`)")
