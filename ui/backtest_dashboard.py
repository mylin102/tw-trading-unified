import streamlit as st
import sys
from pathlib import Path

# Ensure project root is in path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(
    page_title="Quant Lab | Backtest",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── Custom CSS for Institutional Look ──
st.markdown("""
    <style>
    /* Dark mode base aesthetic */
    .stApp {
        background-color: #0F172A;
        color: #F8FAFC;
    }
    /* Tabular numbers for all data */
    [data-testid="stMetricValue"], .stMarkdown code, .stTable, [data-testid="stDataFrame"] {
        font-variant-numeric: tabular-nums;
        font-family: 'JetBrains Mono', 'Fira Code', monospace !important;
    }
    /* Sidebar styling */
    section[data-testid="stSidebar"] {
        background-color: #1E293B;
        border-right: 1px solid #334155;
    }
    /* Sidebar Overall Text */
    section[data-testid="stSidebar"] {
        color: #F8FAFC !important;
    }
    
    /* Force high contrast for language buttons */
    div[data-testid="stSidebar"] div.stButton > button {
        background-color: #1E293B !important;
        border: 1px solid #3B82F6 !important;
        color: #FFFFFF !important;
    }
    div[data-testid="stSidebar"] div.stButton > button * {
        color: #FFFFFF !important;
        font-weight: 700 !important;
    }
    div[data-testid="stSidebar"] div.stButton > button:hover {
        background-color: #3B82F6 !important;
    }

    /* Metric UI Enhancement (Performance stats) */
    div[data-testid="stMetric"] {
        background-color: #1E293B !important;
        border: 1px solid #334155 !important;
        padding: 15px !important;
        border-radius: 10px !important;
    }
    div[data-testid="stMetricLabel"] {
        color: #94A3B8 !important; /* Muted label */
        font-size: 0.9rem !important;
    }
    div[data-testid="stMetricValue"] {
        color: #F8FAFC !important; /* Bright value */
        font-weight: 800 !important;
    }

    /* Input Fields Fix */
    .stTextInput input, .stNumberInput input, .stSelectbox div[data-baseweb="select"] {
        background-color: #0F172A !important;
        color: #F8FAFC !important;
        border: 1px solid #334155 !important;
    }
    /* Divider in sidebar */
    section[data-testid="stSidebar"] hr {
        border-top: 1px solid #334155;
        margin: 1rem 0;
    }
    /* Ensure input fields are readable: dark background + white text */
    .stTextInput input, .stNumberInput input, .stSelectbox div[data-baseweb="select"] {
        background-color: #334155 !important;
        color: #F8FAFC !important;
        border: 1px solid #475569 !important;
    }
    /* Metric Card styling */
    [data-testid="stMetric"] {
        background-color: #1E293B;
        padding: 15px;
        border-radius: 8px;
        border: 1px solid #334155;
    }
    </style>
    """, unsafe_allow_html=True)

from core.i18n import get_text # noqa: E402

# ── Language Selection ──
if "lang" not in st.session_state:
    st.session_state["lang"] = "zh"

with st.sidebar:
    # 一鍵切換按鈕
    toggle_label = f"🌐 {get_text('lang_toggle')}"
    if st.button(toggle_label, use_container_width=True):
        st.session_state["lang"] = "en" if st.session_state["lang"] == "zh" else "zh"
        st.rerun()
    st.divider()

# ── Navigation (2026 Modular Pattern) ──
pages = {
    get_text("nav_analysis"): [
        st.Page("backtest_pages/single_test.py", title=get_text("nav_single"), icon="📊"),
        st.Page("backtest_pages/sweep.py", title=get_text("nav_sweep"), icon="🔬"),
        st.Page("backtest_pages/stock_optimizer.py", title=get_text("nav_stock"), icon="🍎"),
        st.Page("backtest_pages/comparison.py", title=get_text("nav_leaderboard"), icon="🏆"),
    ],
    get_text("nav_system"): [
        st.Page("backtest_pages/history.py", title=get_text("nav_history"), icon="📈"),
    ]
}

pg = st.navigation(pages)

# ── Sidebar System Info ──
with st.sidebar:
    st.divider()
    st.caption("🚀 Quant Lab v2.0")
    st.caption(f"Python: {sys.version.split()[0]}")
    st.caption(f"Root: {ROOT.name}")

pg.run()

