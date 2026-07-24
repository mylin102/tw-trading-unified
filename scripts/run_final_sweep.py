#!/usr/bin/env python3
"""Complete sweep: all ATR + Fixed configs → dated results file."""
import os, sys, json, logging, time, datetime
os.environ['MTS_BACKTEST'] = '1'
logging.disable(logging.CRITICAL)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd, glob

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
files = sorted(glob.glob(os.path.join(PROJECT, 'data/mxf_calendar_spread_*.csv')))[:-1] + \
        sorted(glob.glob(os.path.join(PROJECT, 'data/tmf_calendar_spread_*.csv')))
dfs = []
for f in files:
    df = pd.read_csv(f)
    if not df.empty:
        ts_col = next((c for c in ['ts', 'timestamp', 'datetime'] if c in df.columns), None)
        if ts_col:
            df[ts_col] = pd.to_datetime(df[ts_col])
            df = df.set_index(ts_col)
        dfs.append(df)

from scripts.backtest_spread_v2 import SpreadBacktester
for n in ['strategies.plugins.futures.active.tmf_spread']:
    l = logging.getLogger(n)
    l.setLevel(logging.CRITICAL); l.handlers.clear(); l.propagate = False

results = []
def run(label, cfg):
    t0 = time.time()
    t = SpreadBacktester("tmf_spread", config=cfg)
    for df in dfs: t.run_on_df(df)
    m = t.get_metrics()
    el = time.time() - t0
    if m and m.get("trade_count", 0) > 0:
        tg, tc = m.get("total_gross", 0), m.get("total_fees", 0) + m.get("total_taxes", 0)
        m["label"] = label; m["friction"] = round(tc/abs(tg),4) if tg else 0
        print(f"  {label:42s}  Tr={m['trade_count']:>3}  Net={m['total_net']:>+9,.0f}  "
              f"Win={m['win_rate']:.0%}  PF={m['profit_factor']:.2f}  ({el:.0f}s)")
        results.append(m)

# ── ATR grid ──
print("=== ATR ===")
for sm in [1.0, 1.5, 2.0, 2.5]:
    for tm in [0.1, 0.5, 1.0, 2.0]:
        run(f"ATR Stop {sm}x / Trail {tm}x",
            {"params": {"allow_night_session": True, "regime": "WEAK", "entry_z": 2.5,
             "min_atr": 10.0, "atr_multiplier_stop": sm, "atr_multiplier_trail": tm,
             "release_stop_points": 999, "trail_distance_points": 999}})

# ── Fixed grid ──
print("\n=== Fixed ===")
for stop in [10, 15, 20, 25, 28, 30, 32, 35, 40]:
    for trail in [10, 20, 25, 28, 30, 32, 35, 40, 50, 60, 80, 100]:
        run(f"Fixed Stop {stop}pt / Trail {trail}pt",
            {"params": {"allow_night_session": True, "regime": "WEAK", "entry_z": 2.5,
             "min_atr": 0.0, "atr_multiplier_stop": 0.0, "atr_multiplier_trail": 0.0,
             "release_stop_points": stop, "trail_distance_points": trail}})

# ── Sort & save ──
results.sort(key=lambda x: x.get("total_net", -999999), reverse=True)
today = datetime.date.today().strftime("%Y%m%d")
outpath = os.path.join(PROJECT, f"exports/tmf_sweep_{today}.json")
os.makedirs(os.path.dirname(outpath), exist_ok=True)
with open(outpath, "w") as f:
    json.dump({
        "date": today,
        "n_files": len(files),
        "n_dates": len(set(f.rsplit("_",1)[-1].replace(".csv","") for f in files)),
        "results": results,
    }, f, indent=2)

print(f"\n{'='*80}\nTOP 10\n{'='*80}")
for i, r in enumerate(results[:10]):
    print(f"#{i+1:>2}  {r['label']:38s}  Tr={r['trade_count']:>3}  Net={r['total_net']:>+9,.0f}  "
          f"Win={r['win_rate']:.0%}  PF={r['profit_factor']:.2f}  Fric={r.get('friction',0):.1%}")
print(f"\nSaved {len(results)} results to {outpath}")
