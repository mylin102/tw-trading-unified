#!/usr/bin/env python3
"""Fast sweep: 4 workers, last 30 MXF spread files."""
import os, sys, glob, json
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.environ["MTS_BACKTEST"] = "1"

# Import backtester from project root path
import importlib.util
spec = importlib.util.spec_from_file_location(
    "backtest_spread_v2",
    os.path.join(PROJECT_ROOT, "scripts", "backtest_spread_v2.py"),
)
backtest_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backtest_mod)
SpreadBacktester = backtest_mod.SpreadBacktester
from concurrent.futures import ThreadPoolExecutor, as_completed

def run_one(args):
    strat_name, dfs, config, label, stop_val, trail_val, mode = args
    try:
        tester = SpreadBacktester(strat_name, config=config)
        for df in dfs:
            tester.run_on_df(df)
        m = tester.get_metrics()
        if not m or m.get("trade_count", 0) == 0:
            return {}
        tg = m.get("total_gross", 0)
        tc = m.get("total_fees", 0) + m.get("total_taxes", 0)
        m["label"] = label
        m["mode"] = mode
        m["friction"] = tc / abs(tg) if tg else 0
        if mode == "ATR":
            m["stop_mult"] = stop_val
            m["trail_mult"] = trail_val
        else:
            m["stop_pts"] = stop_val
            m["trail_pts"] = trail_val
        return m
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return {}

def main():
    files = sorted(glob.glob("data/mxf_calendar_spread_*.csv"))[-30:]
    print(f"Loading {len(files)} files...")

    dfs = []
    for f in files:
        df = pd.read_csv(f)
        if df.empty:
            continue
        ts_col = next((c for c in ["ts", "timestamp", "datetime"] if c in df.columns), None)
        if ts_col:
            df[ts_col] = pd.to_datetime(df[ts_col])
            df = df.set_index(ts_col)
        dfs.append(df)
    print(f"Loaded {len(dfs)} DataFrames")

    tasks = []
    # ATR sweep
    for sm in [1.0, 1.5, 2.0, 2.5]:
        for tm in [0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0]:
            cfg = {
                "params": {
                    "allow_night_session": True,
                    "regime": "WEAK",
                    "entry_z": 2.5,
                    "min_atr": 10.0,
                    "atr_multiplier_stop": sm,
                    "atr_multiplier_trail": tm,
                    "release_stop_points": 999,
                    "trail_distance_points": 999,
                }
            }
            tasks.append(("tmf_spread", dfs, cfg, f"ATR Stop {sm}x / Trail {tm}x", sm, tm, "ATR"))

    # Fixed sweep
    for fs in [10, 15, 20, 25, 30]:
        for ft in [10, 20, 30, 40, 50, 60, 80, 100]:
            cfg = {
                "params": {
                    "allow_night_session": True,
                    "regime": "WEAK",
                    "entry_z": 2.5,
                    "min_atr": 0.0,
                    "atr_multiplier_stop": 0.0,
                    "atr_multiplier_trail": 0.0,
                    "release_stop_points": fs,
                    "trail_distance_points": ft,
                }
            }
            tasks.append(("tmf_spread", dfs, cfg, f"Fixed Stop {fs}pt / Trail {ft}pt", fs, ft, "Fixed"))

    print(f"{len(tasks)} tasks, 4 workers — running...")
    results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(run_one, t): t for t in tasks}
        done = 0
        for fut in as_completed(futs):
            m = fut.result()
            done += 1
            if done % 10 == 0:
                print(f"  {done}/{len(tasks)}")
            if m:
                results.append(m)

    results.sort(key=lambda x: x.get("total_net", -999999), reverse=True)

    out = "exports/tmf_sweep_results.json"
    os.makedirs("exports", exist_ok=True)
    with open(out, "w") as f:
        json.dump({"results": results, "n_files": len(files), "n_tasks": len(tasks)}, f)

    print(f"\nDone. Top 10:\n")
    for i, r in enumerate(results[:10]):
        print(f"  #{i+1} {r['label']:40s} Net={r['total_net']:>8,.0f}  Trades={r['trade_count']:>3}  Win={r['win_rate']:.0%}  PF={r['profit_factor']:.2f}  Fric={r['friction']:.1%}")

if __name__ == "__main__":
    if sys.platform == "darwin":
        os.system(f"taskpolicy -b -p {os.getpid()}")
    main()
