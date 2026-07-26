# 2026-07-26 Gemini CLI: Wave D1 Streamlit Multipage - 02 Positions & Orders
import sys
from pathlib import Path
import streamlit as st

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="Positions & Orders | Trading Unified", page_icon="📋", layout="wide")

st.title("📋 持倉與委託單 (Positions & Orders)")
st.caption("即時部位與歷史委託單明細視圖")

st.info("持倉與委託單載入中...")
