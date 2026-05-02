
import os
import json
import pandas as pd
from datetime import datetime
from pathlib import Path

def generate_v15_audit(target_date: str = None):
    """
    Generate v15_daily_audit report based on router_trace and trade logs.
    target_date format: YYYYMMDD (e.g. 20260430)
    """
    if target_date is None:
        target_date = datetime.now().strftime("%Y%m%d")
        
    trace_file = Path(f"logs/router_trace/router_trace_{target_date}.jsonl")
    attribution_file = Path(f"logs/trade_attribution.csv")
    
    report = {
        "date": target_date,
        "total_bars": 0,
        "ATR_GATE_PASS": 0,
        "ATR_GATE_FAIL": 0,
        "SESSION_BUFFER_SKIP": 0,
        "REGIME_BLOCKED": 0,
        "v15_winner": 0,
        "fallback_strategy": 0,
        "DECIMAL_DETECTED": 0,
        "STRATEGY_CRASH": 0,
        "trades": 0,
        "net_pnl": 0.0,
        "avg_hold_bars": 0.0
    }
    
    if not trace_file.exists():
        print(f"Trace file {trace_file} not found.")
        return report

    # 1. Parse Router Trace (Bar-by-Bar detail)
    with open(trace_file, "r") as f:
        for line in f:
            try:
                row = json.loads(line)
                report["total_bars"] += 1
                
                # Check for global tags in 'notes' or 'reasons' equivalent
                # In RouterTrace, the regime reasons often end up in the top-level or strategy notes
                
                # ═══ Check Strategies ═══
                strategies = row.get("strategies", [])
                for s in strategies:
                    name = s.get("name", "")
                    skip_reason = str(s.get("skip_reason", ""))
                    notes = str(s.get("notes", ""))
                    
                    if s.get("winner", False):
                        if "adaptive_orb" in name:
                            report["v15_winner"] += 1
                        else:
                            report["fallback_strategy"] += 1
                    
                    # Audit Specific Tags
                    if "ATR_GATE_FAIL" in notes or "ATR_GATE_FAIL" in skip_reason:
                        report["ATR_GATE_FAIL"] += 1
                    elif "ATR_GATE_PASS" in notes or "confirmed" in notes.lower():
                        report["ATR_GATE_PASS"] += 1
                        
                    if "SESSION_BUFFER_SKIP" in notes:
                        report["SESSION_BUFFER_SKIP"] += 1
                        
                    if "REGIME_NOT_TRADABLE" in skip_reason or "BLOCKED" in skip_reason:
                        report["REGIME_BLOCKED"] += 1
                        
            except Exception:
                continue

    # 2. Parse Attribution / Decisions for PnL and count
    if attribution_file.exists():
        try:
            # First peek to see if header exists
            with open(attribution_file, "r") as f:
                header = f.readline()
                has_header = "timestamp" in header.lower()
            
            df = pd.read_csv(attribution_file, header=0 if has_header else None,
                             names=["ts", "trade_id", "strategy", "regime", "entry_info", "exit_info", "notes"])
            
            # Use ISO8601 parsing for 'T' format
            df["ts_dt"] = pd.to_datetime(df["ts"], errors='coerce')
            df = df.dropna(subset=["ts_dt"])
            
            today_mask = df["ts_dt"].dt.strftime("%Y%m%d") == target_date
            today_trades = df[today_mask]
            
            for _, t in today_trades.iterrows():
                try:
                    exit_data = json.loads(t["exit_info"])
                    if "pnl" in exit_data:
                        report["trades"] += 1
                        report["net_pnl"] += float(exit_data["pnl"])
                except Exception:
                    continue
        except Exception as e:
            print(f"Error parsing attribution: {e}")

    # 3. Print Summary
    print(f"\n[v15_daily_audit] Report for {target_date}")
    print("-" * 40)
    for k, v in report.items():
        if isinstance(v, float):
            print(f"{k:20}: {v:.2f}")
        else:
            print(f"{k:20}: {v}")
    print("-" * 40)
    
    return report

if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else None
    generate_v15_audit(d)
