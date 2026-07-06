import sys
import os
import pandas as pd
import plotly.graph_objects as go
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from ui.dashboard import load_futures_indicators, load_options_indicators

def test_chart():
    print("Loading data...")
    f_df = load_futures_indicators(full_history=True)
    o_df = load_options_indicators(full_history=True)
    
    fig = go.Figure()
    _TICKER = "TMF"
    
    if f_df is not None and not f_df.empty:
        f_close = f_df["close"] if "close" in f_df.columns else f_df["Close"]
        print("Adding futures trace...")
        fig.add_trace(go.Scatter(
            x=f_df["timestamp"].to_numpy(),
            y=f_close.to_numpy(),
            name=f"{_TICKER} (期貨)",
            line=dict(color="#1f77b4", width=2)
        ))
        
    if o_df is not None and not o_df.empty:
        m_col = "price_mtx" if "price_mtx" in o_df.columns else ("mtx_close" if "mtx_close" in o_df.columns else None)
        if m_col:
            print("Adding options trace...")
            fig.add_trace(go.Scatter(
                x=o_df["timestamp"].to_numpy(),
                y=o_df[m_col].to_numpy(),
                name="MTX (選擇權標的)",
                line=dict(color="#ff7f0e", width=1.5, dash="dot")
            ))
            
    fig.update_layout(
        height=400, 
        margin=dict(t=10, b=10, l=40, r=20), 
        legend=dict(orientation="h", y=1.05, x=0.5, xanchor="center"),
        hovermode="x unified"
    )
    fig.update_yaxes(title_text="指數點位", tickformat=",.0f", gridcolor="rgba(128,128,128,0.1)")
    fig.update_xaxes(gridcolor="rgba(128,128,128,0.1)")
    
    print("Converting figure to JSON...")
    try:
        json_str = fig.to_json()
        print("Figure successfully converted to JSON! Length:", len(json_str))
    except Exception as e:
        print("Failed to convert figure to JSON:", e)

if __name__ == "__main__":
    test_chart()
