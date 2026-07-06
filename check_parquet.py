import pandas as pd
from pathlib import Path

path = Path("data/historical/TXFR1_5m.parquet")
if path.exists():
    df = pd.read_parquet(path)
    print(f"TXFR1 Parquet: {len(df)} rows")
    print(f"Start: {df.index.min()}")
    print(f"End: {df.index.max()}")
else:
    print("TXFR1 Parquet not found")
