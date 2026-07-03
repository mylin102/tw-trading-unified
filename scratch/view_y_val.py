import sys
import os
import pandas as pd
import plotly.graph_objects as go
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from ui.dashboard import load_futures_indicators, load_options_indicators

def inspect_types():
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
        
    d = fig.to_dict()
    trace = d["data"][0]
    y_val = trace["y"]
    x_val = trace["x"]
    print("y_val type:", type(y_val))
    print("x_val type:", type(x_val))
    print("y_val length:", len(y_val))
    print("x_val length:", len(x_val))
    print("y_val contents:", y_val)
    print("x_val contents:", x_val)

if __name__ == "__main__":
    inspect_types()
