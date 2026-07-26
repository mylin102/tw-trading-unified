# 2026-07-26 Gemini CLI: Wave D1 Streamlit Multipage - 05 Read-only Configuration
import sys
from pathlib import Path
import streamlit as st
import yaml

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="Configuration | Trading Unified", page_icon="⚙️", layout="wide")

st.title("⚙️ 唯讀系統設定 (System Configuration)")
st.caption("正式環境 YAML 參數檢視與驗證 (Read-only)")

config_dir = ROOT / "config"
yaml_files = list(config_dir.glob("*.yaml"))

if yaml_files:
    selected_file = st.selectbox("選擇設定檔", options=[f.name for f in yaml_files])
    file_path = config_dir / selected_file
    try:
        content = file_path.read_text(encoding="utf-8")
        st.code(content, language="yaml")
    except Exception as e:
        st.error(f"無法載入檔案: {e}")
else:
    st.info("無可用設定檔。")
