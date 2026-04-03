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
    /* Fix sidebar text contrast */
    section[data-testid="stSidebar"] p, 
    section[data-testid="stSidebar"] label,
    section[data-testid="stSidebar"] span,
    section[data-testid="stSidebar"] h1,
    section[data-testid="stSidebar"] h2,
    section[data-testid="stSidebar"] h3 {
        color: #F8FAFC !important;
        font-weight: 600;
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

# ── Navigation (2026 Modular Pattern) ──
pages = {
    "Analysis": [
        st.Page("backtest_pages/single_test.py", title="Single Test", icon="📊"),
        st.Page("backtest_pages/sweep.py", title="Parameter Sweep", icon="🔬"),
        st.Page("backtest_pages/comparison.py", title="Strategy Leaderboard", icon="🏆"),
    ],
    "System": [
        st.Page("backtest_pages/history.py", title="Performance History", icon="📈"),
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
