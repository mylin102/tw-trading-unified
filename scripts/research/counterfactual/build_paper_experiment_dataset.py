#!/usr/bin/env python3
"""Build paper experiment dataset from raw telemetry events."""
import json, csv, os, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

EXCLUSION_REASONS = [
    "QUOTE_STALE","MISSING_QUOTE","MISSING_FILL","DUPLICATE_FILL",
    "PARTIAL_FILL_INCOMPLETE","RESTART_INCOMPLETE","MANUAL_INTERVENTION",
    "EMERGENCY_FLATTEN","PNL_RECON_MISMATCH","TELEMETRY_GAP",
    "UNSUPPORTED_RELEASE_REASON","NON_PAPER_ACCOUNT"
]

def find_generations(base_dir="data/telemetry/combined-exit-paper"):
    p = Path(base_dir)
    if not p.exists(): return []
    return sorted([d for d in p.iterdir() if d.is_dir() and d.name.startswith("generation-")])

def load_events(generation_dir):
    events = []
    raw_dir = generation_dir / "raw"
    if not raw_dir.exists(): return events
    for f in sorted(raw_dir.glob("events-*.jsonl")):
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if line: events.append(json.loads(line))
    return events

def build_dataset(generation_dir=None):
    if generation_dir is None:
        gens = find_generations()
        if not gens:
            print("No generations found")
            return
        generation_dir = gens[-1]
        print("Using generation:", generation_dir.name)
    
    events = load_events(generation_dir)
    if not events:
        print("No events found in", generation_dir)
        return
    
    # Group by release_event_id
    groups = defaultdict(list)
    for ev in events:
        rid = ev.get("release_event_id") or ev.get("event_id","")
        groups[rid].append(ev)
    
    print("Loaded %d events, %d release groups" % (len(events), len(groups)))
    
    rows = []
    excluded = []
    
    for rid, evs in groups.items():
        decision = next((e for e in evs if e.get("event_type")=="RELEASE_DECISION_OBSERVED"), None)
        candidate = next((e for e in evs if e.get("event_type")=="COMBINED_EXIT_CANDIDATE_CREATED"), None)
        fills = [e for e in evs if e.get("event_type")=="ORDER_FILLED"]
        
        if not decision:
            excluded.append({"release_event_id":rid,"reason":"TELEMETRY_GAP","detail":"no decision event"})
            continue
        
        tid = decision.get("trade_id","")
        rel = decision.get("release_leg","")
        rel_reason = decision.get("release_reason","")
        
        # Check exclusion conditions
        excl_reason = None
        na_q = decision.get("near_quote_age_ms")
        fa_q = decision.get("far_quote_age_ms")
        if (na_q is not None and na_q > 5000) or (fa_q is not None and fa_q > 5000):
            excl_reason = "QUOTE_STALE"
        
        elig = decision.get("combined_candidate_eligible", False)
        
        row = {
            "trade_id": tid,
            "release_event_id": rid,
            "release_leg": rel,
            "release_reason": rel_reason,
            "session": decision.get("session",""),
            "near_unrealized_pnl_twd": decision.get("near_unrealized_pnl_twd"),
            "far_unrealized_pnl_twd": decision.get("far_unrealized_pnl_twd"),
            "spread_unrealized_pnl_twd": decision.get("spread_unrealized_pnl_twd"),
            "atr": decision.get("atr"),
            "stop_mult": decision.get("stop_mult"),
            "trail_mult": decision.get("trail_mult"),
            "spread_z": decision.get("spread_z"),
            "execution_enabled": decision.get("execution_enabled",False),
            "eligible": elig,
            "excluded": excl_reason is not None,
            "exclusion_reason": excl_reason or "",
            "num_fills": len(fills),
        }
        
        if candidate:
            for k in ["slippage_0_tick_net_pnl","slippage_1_tick_net_pnl",
                      "slippage_2_tick_net_pnl","slippage_3_tick_net_pnl",
                      "hypothetical_bid_close","hypothetical_ask_close"]:
                row[k] = candidate.get(k)
        
        if excl_reason:
            excluded.append(row)
        else:
            rows.append(row)
    
    # Write CSV
    out_dir = generation_dir / "dataset"
    out_dir.mkdir(exist_ok=True)
    
    if rows:
        cols = list(rows[0].keys())
        with open(out_dir / "release_events.csv","w",newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader(); w.writerows(rows)
        print("Wrote %d release events to dataset" % len(rows))
    
    if excluded:
        cols = list(excluded[0].keys())
        with open(out_dir / "excluded_events.csv","w",newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader(); w.writerows(excluded)
        print("Wrote %d excluded events" % len(excluded))
    
    # Coverage manifest
    manifest = {
        "generation": generation_dir.name,
        "total_events": len(events),
        "release_groups": len(groups),
        "release_events": len(rows),
        "excluded_events": len(excluded),
        "build_time": datetime.now().isoformat(),
    }
    with open(out_dir / "coverage_manifest.json","w") as f:
        json.dump(manifest, f, indent=2)
    print("Manifest:", json.dumps(manifest))
    return rows, excluded

if __name__ == "__main__":
    build_dataset()
