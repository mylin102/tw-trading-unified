import sys
import os
import json
import pandas as pd
import plotly.graph_objects as go
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from ui.dashboard import load_futures_indicators, load_options_indicators

def inspect_json():
    f_df = load_futures_indicators(full_history=True)
    o_df = load_options_indicators(full_history=True)
    
    fig = go.Figure()
    
    if f_df is not None and not f_df.empty:
        f_close = f_df["close"] if "close" in f_df.columns else f_df["Close"]
        fig.add_trace(go.Scatter(
            x=f_df["timestamp"].to_numpy(),
            y=f_close.to_numpy(),
            name="Futures",
        ))
        
    if o_df is not None and not o_df.empty:
        m_col = "price_mtx" if "price_mtx" in o_df.columns else ("mtx_close" if "mtx_close" in o_df.columns else None)
        if m_col:
            fig.add_trace(go.Scatter(
                x=o_df["timestamp"].to_numpy(),
                y=o_df[m_col].to_numpy(),
                name="Options",
            ))
            
    fig.update_layout(height=400)
    
    d = fig.to_dict()
    
    # Check data types and values in traces
    for i, trace in enumerate(d.get("data", [])):
        print(f"\nTrace {i} ('{trace.get('name')}'):")
        x_val = trace.get("x", [])
        y_val = trace.get("y", [])
        print(f"  x length: {len(x_val)}, first 5: {x_val[:5]}")
        print(f"  y length: {len(y_val)}, first 5: {y_val[:5]}")
        
        # Check for non-finite values in y
        nan_y = [val for val in y_val if val is None or (isinstance(val, float) and (val != val))]
        print(f"  Non-finite y count: {len(nan_y)}")
        
        # Check for non-string/non-numeric values in x
        bad_x = [val for val in x_val if not isinstance(val, (str, int, float)) or val is None]
        print(f"  Invalid x type count: {len(bad_x)}")
        if bad_x:
            print("  First 5 invalid x:", bad_x[:5])

if __name__ == "__main__":
    inspect_json()
