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

# ── Clean Light Mode CSS (Institutional Look) ──
st.markdown("""
    <style>
    /* Light mode base aesthetic */
    .stApp {
        background-color: #FFFFFF;
        color: #0F172A;
    }
    /* Main body text */
    .stApp p, .stApp span, .stApp label, .stApp h1, .stApp h2, .stApp h3 {
        color: #0F172A !important;
    }
    
    /* Sidebar styling */
    section[data-testid="stSidebar"] {
        background-color: #F8FAFC !important;
        border-right: 1px solid #E2E8F0;
    }
    section[data-testid="stSidebar"] * {
        color: #0F172A !important;
    }
    
    /* Language Toggle Button - High Visibility Blue */
    div[data-testid="stSidebar"] .stButton > button {
        background-color: #2563EB !important;
        color: #FFFFFF !important;
        border: none !important;
        font-weight: 700 !important;
        width: 100%;
    }
    div[data-testid="stSidebar"] .stButton > button * {
        color: #FFFFFF !important;
    }

    /* Metric UI Enhancement (Light Cards) */
    [data-testid="stMetric"] {
        background-color: #F1F5F9 !important;
        border: 1px solid #E2E8F0 !important;
        padding: 1rem !important;
        border-radius: 0.5rem !important;
    }
    [data-testid="stMetricLabel"] > div {
        color: #475569 !important;
        font-weight: 600 !important;
    }
    [data-testid="stMetricValue"] > div {
        color: #1E293B !important;
        font-weight: 800 !important;
    }

    /* Input Fields Fix: White background, dark text */
    .stTextInput input, .stNumberInput input, .stSelectbox div[data-baseweb="select"] {
        background-color: #FFFFFF !important;
        color: #0F172A !important;
        border: 1px solid #CBD5E1 !important;
    }
    
    /* Table / DataFrame text */
    [data-testid="stDataFrame"] * {
        color: #0F172A !important;
    }
    </style>
    """, unsafe_allow_html=True)

from core.i18n import get_text # noqa: E402

# ── Language Selection ──
if "lang" not in st.session_state:
    st.session_state["lang"] = "zh"

with st.sidebar:
    toggle_label = f"🌐 {get_text('lang_toggle')}"
    if st.button(toggle_label, use_container_width=True):
        st.session_state["lang"] = "en" if st.session_state["lang"] == "zh" else "zh"
        st.rerun()
    st.divider()

# ── Navigation (Asset-Centric Grouping) ──
pages = {
    get_text("nav_futures"): [
        st.Page("backtest_pages/single_test.py", title=get_text("nav_single"), icon="📈"),
        st.Page("backtest_pages/sweep.py", title=get_text("nav_sweep"), icon="🔬"),
        st.Page("backtest_pages/comparison.py", title=get_text("nav_leaderboard"), icon="🏆"),
    ],
    get_text("nav_stocks"): [
        st.Page("backtest_pages/stock_optimizer.py", title=get_text("nav_stock_lab"), icon="🍎"),
    ],
    get_text("nav_system"): [
        st.Page("backtest_pages/history.py", title=get_text("nav_history"), icon="📊"),
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
