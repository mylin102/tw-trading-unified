import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from pathlib import Path
import sys

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TRADES_DIR = ROOT / "exports" / "trades"

def load_all_history():
    """Load and aggregate all CSVs from exports/trades/"""
    files = list(TRADES_DIR.glob("*.csv"))
    if not files:
        return None
    
    all_df = []
    for f in files:
        try:
            df = pd.read_csv(f)
            # Ensure required columns exist
            if "PnL" in df.columns and "Timestamp" in df.columns:
                all_df.append(df)
        except Exception:
            continue
            
    if not all_df:
        return None
        
    combined = pd.concat(all_df, ignore_index=True)
    combined["Timestamp"] = pd.to_datetime(combined["Timestamp"], errors="coerce")
    combined = combined.dropna(subset=["Timestamp"]).sort_values("Timestamp")
    return combined

def main():
    st.title("📈 Performance History")
    st.caption("Aggregated analytics from all recorded trading sessions.")

    df = load_all_history()

    if df is None or df.empty:
        st.info("No trading history found in `exports/trades/`.")
        return

    # ── Summary Metrics ──
    # Assuming PnL column exists and 0 means no exit yet
    exits = df[df["PnL"] != 0].copy()
    
    st.header("Lifetime Statistics")
    c1, c2, c3, c4 = st.columns(4)
    
    total_pnl = exits["PnL"].sum()
    win_rate = (exits["PnL"] > 0).mean() * 100 if not exits.empty else 0
    pf = exits[exits["PnL"] > 0]["PnL"].sum() / abs(exits[exits["PnL"] < 0]["PnL"].sum()) if (exits["PnL"] < 0).any() else 1.0
    
    c1.metric("Total PnL", f"{total_pnl:+,.0f} TWD")
    c2.metric("Win Rate", f"{win_rate:.1f}%")
    c3.metric("Profit Factor", f"{pf:.2f}")
    c4.metric("Total Exits", len(exits))

    # ── Equity Curve ──
    st.divider()
    st.header("Cumulative Equity")
    exits["cum_pnl"] = exits["PnL"].cumsum()
    
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=exits["Timestamp"], 
        y=exits["cum_pnl"], 
        name="Total Equity",
        fill='tozeroy',
        line=dict(color="#3B82F6", width=2)
    ))
    fig.update_layout(template="plotly_dark", height=400, margin=dict(t=20, b=20, l=40, r=20))
    st.plotly_chart(fig, use_container_width=True)

    # ── Detailed Logs ──
    st.divider()
    st.header("Historical Trade Logs")
    st.dataframe(df.sort_values("Timestamp", ascending=False), use_container_width=True)

if __name__ == "__main__":
    main()
