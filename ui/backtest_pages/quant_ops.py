import streamlit as st
import pandas as pd
from pathlib import Path
import sys

# Ensure project root is in path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.decision_logger import get_recent_decisions

def main():
    st.title("🛡️ Quant Lab Operations (v1.1)")
    st.caption("Monitoring health, decisions, and ML drift for the unified trading system.")

    tab1, tab2, tab3 = st.tabs(["📝 Decision Log", "🤖 ML Model Drift", "🎖️ Retirement Home"])

    with tab1:
        st.header("Recent Strategic Decisions")
        decisions = get_recent_decisions(limit=20)
        if not decisions.empty:
            # Color coding risk level
            def color_risk(val):
                color = 'red' if val == 'high' else ('orange' if val == 'medium' else 'gray')
                return f'color: {color}'
            
            st.dataframe(decisions.style.applymap(color_risk, subset=['risk_level']), use_container_width=True)
        else:
            st.info("No decisions logged yet.")

    with tab2:
        st.header("ML Model Performance History")
        history_path = ROOT / "data" / "optimization" / "model_history.csv"
        if history_path.exists():
            hist_df = pd.read_csv(history_path)
            st.line_chart(hist_df.set_index("timestamp")["accuracy"])
            st.subheader("Version Details")
            st.dataframe(hist_df.tail(10), use_container_width=True)
        else:
            st.info("No model history found. Run train_rf.py first.")

    with tab3:
        st.header("Retired Strategy Catalog")
        retired_readme = ROOT / "strategies" / "retired" / "README.md"
        if retired_readme.exists():
            with open(retired_readme, "r") as f:
                st.markdown(f.read())
        else:
            st.info("No strategies retired yet.")

if __name__ == "__main__":
    main()
