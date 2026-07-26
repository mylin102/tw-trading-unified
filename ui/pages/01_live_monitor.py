# 2026-07-26 Gemini CLI: Wave D1 Streamlit Multipage - 01 Live Monitor
import sys
from pathlib import Path
import streamlit as st

# Ensure project root is in sys.path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="Live Monitor | Trading Unified", page_icon="🟢", layout="wide")

st.title("🟢 即時行情與監控 (Live Monitor)")
st.caption("即時期貨/選擇權行情與指標監控主頁面")

st.info("系統監控運作中。請使用主入口 ui/dashboard.py 或點擊選單開啟子視圖。")
