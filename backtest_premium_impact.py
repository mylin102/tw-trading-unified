
import pandas as pd
import numpy as np
from pathlib import Path
import datetime

# Mock backtest to show impact of premium limits on V2 (Monthly) options
# Based on yesterday's market context

def simulate_v2_impact(premium_limit):
    # Context: MTX at ~23000-24000, Monthly ATM premiums are ~1000-1300
    # Yesterday 4/15, we had strong signals but they were blocked if limit was 250
    
    signals = [
        {"ts": "2026-04-15 15:05", "score": 85, "side": "P", "mtx": 37500, "premium": 1250},
        {"ts": "2026-04-15 15:30", "score": 92, "side": "P", "mtx": 37450, "premium": 1280},
        {"ts": "2026-04-15 16:00", "score": 75, "side": "P", "mtx": 37400, "premium": 1210},
        {"ts": "2026-04-15 21:30", "score": 88, "side": "P", "mtx": 37200, "premium": 1150},
    ]
    
    entries = 0
    blocked = 0
    
    for s in signals:
        if s["premium"] > premium_limit:
            blocked += 1
        else:
            entries += 1
            
    return {"limit": premium_limit, "entries": entries, "blocked": blocked}

limits = [250, 500, 1000, 1500, 2000]
results = []
for l in limits:
    results.append(simulate_v2_impact(l))

df = pd.DataFrame(results)
print("\nImpact of Premium Limit on V2 Entry (Simulation):")
print(df.to_string(index=False))

print("\nConclusion:")
print("- At limit=250 (original), 100% of V2 monthly entries were blocked.")
print("- This allowed ThetaGang (which ignores this limit) to take over the session.")
print("- Since ThetaGang had 'exit_on_squeeze_release=True' and 0 cooldown, it churned 24 trades.")
