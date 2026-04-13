import streamlit as st
import pandas as pd
import sys
import os
import subprocess
from pathlib import Path
from datetime import datetime

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_manager import data_manager # noqa: E402
from core.i18n import get_text # noqa: E402

def main():
    st.title("🗄️ Historical Data Management")
    st.caption("Inspect and expand the high-performance Parquet database for long-term backtesting.")

    # 1. Database Inventory
    st.header("📊 Data Inventory")
    inventory = data_manager.get_inventory()
    
    if not inventory:
        st.warning("The Parquet database is currently empty. Start by expanding history below.")
    else:
        # Create display table
        rows = []
        for ticker, stats in inventory.items():
            rows.append({
                "Ticker": ticker,
                "Start Date": stats["start"].strftime("%Y-%m-%d"),
                "End Date": stats["end"].strftime("%Y-%m-%d"),
                "Total Bars": f"{stats['rows']:,}",
                "Size (MB)": stats["size_mb"],
                "Location": stats["path"]
            })
        st.table(pd.DataFrame(rows))

    st.divider()

    # 2. Expansion Interface
    st.header("🚀 Expand Database")
    st.markdown("Download missing historical data in monthly chunks to build a 3-year database.")
    
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        target_ticker = st.text_input("Ticker Symbol", value="TXFR1", help="Use 'TXFR1' for continuous futures.")
    with c2:
        years_back = st.number_input("Years Back", min_value=1, max_value=5, value=1)
    with c3:
        st.write("") # Spacer
        st.write("") # Spacer
        expand_btn = st.button("Start Expansion", type="primary", use_container_width=True)

    if expand_btn:
        with st.status(f"Expanding {target_ticker}...", expanded=True) as status:
            st.write(f"Initializing Shioaji session for {target_ticker}...")
            
            # Execute expand_history.py as a subprocess
            cmd = ["python3", "scripts/sync/expand_history.py", "--ticker", target_ticker, "--years", str(years_back)]
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            
            # Stream stdout to UI
            log_container = st.empty()
            full_log = ""
            for line in process.stdout:
                full_log += line
                log_container.code(full_log)
            
            process.wait()
            if process.returncode == 0:
                status.update(label="✅ Expansion Complete!", state="complete", expanded=False)
                st.success(f"Successfully expanded {target_ticker} database.")
                st.rerun()
            else:
                status.update(label="❌ Expansion Failed", state="error")
                st.error("Failed to expand database. Check console logs.")

if __name__ == "__main__":
    main()
