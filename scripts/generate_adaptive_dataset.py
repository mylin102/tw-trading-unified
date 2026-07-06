#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Adaptive Trade Dataset Pipeline — Unified dataset builder from multiple log fragments.
Author: Gemini CLI
Date: 2026-07-01
"""

import sys
import os
import json
import csv
import glob
import re
import datetime
from pathlib import Path
import pandas as pd

# macOS Silicon optimization: Force process to E-Cores if run directly
if __name__ == '__main__':
    if sys.platform == "darwin":
        os.system(f"taskpolicy -b -p {os.getpid()}")

def parse_mts_trades():
    """
    Parse MTS calendar spread trades from mts_trade_fills.jsonl and mts_spread_events.jsonl.
    """
    fills_file = Path("logs/mts_trade_fills.jsonl")
    events_file = Path("logs/mts_spread_events.jsonl")
    
    if not fills_file.exists():
        return []
        
    trades = {}
    
    # 1. Parse all fills from mts_trade_fills.jsonl
    with open(fills_file, "r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                fill = json.loads(line)
                trade_id = fill.get("trade_id")
                if not trade_id:
                    continue
                if trade_id not in trades:
                    trades[trade_id] = {
                        "timestamp": fill.get("timestamp"),
                        "trade_id": trade_id,
                        "trading_day": fill.get("trading_day") or fill.get("timestamp")[:10] if fill.get("timestamp") else None,
                        "session": fill.get("session", "UNKNOWN"),
                        "instrument_family": "futures",
                        "strategy_name": "tmf_spread",
                        "signal_side": fill.get("side"),  # Entry side
                        "score": 0.0,
                        "momentum": 0.0,
                        "vwap_distance_pts": 0.0,
                        "atr": 0.0,
                        "regime": "UNKNOWN",
                        "execution_status": "entered",
                        "rejection_reason": "",
                        "realized_pnl": 0.0,
                        "pnl_pts": 0.0,
                        "friction_cost": 0.0,
                        "supporting_context": {
                            "legs": {},
                            "events": []
                        }
                    }
                
                trade = trades[trade_id]
                fill_type = fill.get("fill_type")
                leg_name = fill.get("leg")
                
                # Capture leg info
                trade["supporting_context"]["legs"][leg_name] = {
                    "symbol": fill.get("contract"),
                    "side": fill.get("side"),
                    "qty": fill.get("qty"),
                    "price": fill.get("price"),
                    "fill_type": fill_type,
                    "leg_mfe": fill.get("leg_mfe"),
                    "leg_mae": fill.get("leg_mae"),
                    "post_release_mfe": fill.get("post_release_mfe"),
                    "post_release_mae": fill.get("post_release_mae"),
                    "realized_pnl": fill.get("realized_pnl")
                }
                
                if fill_type == "ENTRY":
                    # Capture entry details
                    trade["timestamp"] = fill.get("timestamp")
                    if fill.get("spread_z") is not None:
                        trade["supporting_context"]["spread_z_entry"] = fill.get("spread_z")
                        
                # Sum PNL and approximate friction
                pnl = fill.get("realized_pnl")
                if pnl is not None:
                    trade["realized_pnl"] += float(pnl)
                    # Approximate friction: 20 TWD broker fee per side + tax
                    # In real mode, it's better to deduct from realized_pnl. We log it.
                    trade["friction_cost"] += 20.0
                    
            except Exception as e:
                print(f"Error parsing fills line: {e}", file=sys.stderr)
                
    # 2. Enrich using events from mts_spread_events.jsonl
    if events_file.exists():
        with open(events_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    trade_id = event.get("trade_id")
                    if not trade_id or trade_id not in trades:
                        continue
                        
                    trade = trades[trade_id]
                    event_type = event.get("event")
                    
                    # Capture Z-score, ATR, regime from event context
                    if event_type == "ENTRY":
                        if event.get("spread_z") is not None:
                            trade["supporting_context"]["spread_z_entry"] = event.get("spread_z")
                            
                    elif event_type in ("RELEASE_FAR_SUBMITTED", "RELEASE_NEAR_SUBMITTED", "EXIT_LOG", "EXIT_REMAINING"):
                        if event.get("atr") is not None:
                            trade["atr"] = float(event.get("atr"))
                        if event.get("session") is not None:
                            trade["session"] = event.get("session")
                        if event.get("risk_mode") is not None:
                            trade["supporting_context"]["risk_mode"] = event.get("risk_mode")
                            
                except Exception as e:
                    print(f"Error parsing events line: {e}", file=sys.stderr)
                    
    # Calculate total points PnL from legs
    for trade in trades.values():
        legs = trade["supporting_context"]["legs"]
        # Standard calendar spread gross points: NEAR leg PnL + FAR leg PnL
        near_leg = legs.get("NEAR", {})
        far_leg = legs.get("FAR", {})
        if near_leg and far_leg:
            # We calculate points based on side
            # (Exit Price - Entry Price) for Long, (Entry Price - Exit Price) for Short
            def get_leg_pts(leg):
                # An entry and an exit/release are required to compute leg pts
                # In standard fills JSON, we might have multiple fills per leg (e.g. entry, release/exit)
                # But they are indexed under same leg name "NEAR" or "FAR" in the simple trades parser above.
                # Let's check how they are stored.
                pass
            
            # Since fills.jsonl has explicit realized_pnl in TWD, and point value is 10 TWD/point:
            trade["pnl_pts"] = round(trade["realized_pnl"] / 10.0, 1)
            
    return list(trades.values())

def parse_standard_trades():
    """
    Parse standard directional futures trades from logs/trade_attribution.csv.
    """
    attribution_file = Path("logs/trade_attribution.csv")
    if not attribution_file.exists():
        return []
        
    trades = []
    with open(attribution_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                features = json.loads(row.get("features", "{}"))
                outcome = json.loads(row.get("outcome", "{}"))
                
                trade = {
                    "timestamp": row.get("timestamp"),
                    "trading_day": row.get("timestamp")[:10] if row.get("timestamp") else None,
                    "session": "UNKNOWN",  # Can be derived or default
                    "instrument_family": "futures",
                    "strategy_name": row.get("strategy"),
                    "signal_side": features.get("signal", "UNKNOWN"),
                    "score": float(features.get("score", 0.0)),
                    "momentum": float(features.get("momentum", 0.0)),
                    "vwap_distance_pts": float(features.get("vwap_distance_pts", 0.0)),
                    "atr": float(features.get("atr", 0.0)),
                    "regime": row.get("regime", "UNKNOWN"),
                    "execution_status": "entered",
                    "rejection_reason": "",
                    "realized_pnl": float(outcome.get("pnl", 0.0)),
                    "pnl_pts": float(outcome.get("pnl_pts", 0.0)),
                    "friction_cost": float(outcome.get("friction_cost", 0.0)),
                    "supporting_context": {
                        "raw_features": features,
                        "raw_outcome": outcome,
                        "exit_reason": outcome.get("exit_reason", "")
                    }
                }
                
                # Check for exit status
                if outcome.get("exit_reason") == "PARTIAL_EXIT":
                    trade["execution_status"] = "partial_exit"
                else:
                    trade["execution_status"] = "exit"
                    
                trades.append(trade)
            except Exception as e:
                print(f"Error parsing trade attribution row: {e}", file=sys.stderr)
                
    return trades

def parse_blocked_signals():
    """
    Parse blocked / no-entry signals from logs/market_data/*_signals_audit.csv and associate indicators.
    """
    audit_pattern = "logs/market_data/*_signals_audit.csv"
    audit_files = glob.glob(audit_pattern)
    
    blocked_rows = []
    
    for file_path in audit_files:
        path = Path(file_path)
        # Extract ticker from filename, e.g. TMF_20260630_signals_audit.csv
        match = re.match(r"^([A-Z0-9]+)_(\d{8})_signals_audit.csv$", path.name)
        if not match:
            continue
            
        ticker, date_str = match.groups()
        
        # Load corresponding indicators to join indicators context
        indicators_file = path.parent / f"{ticker}_{date_str}_PAPER_indicators.csv"
        if not indicators_file.exists():
            indicators_file = path.parent / f"{ticker}_{date_str}_indicators.csv"
            
        indicators_df = None
        if indicators_file.exists():
            try:
                indicators_df = pd.read_csv(indicators_file)
                # Parse timestamp for exact matching
                indicators_df["parsed_time"] = pd.to_datetime(indicators_df["timestamp"])
            except Exception as e:
                print(f"Error loading indicators {indicators_file}: {e}", file=sys.stderr)
                
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts_str = row.get("timestamp")
                    if not ts_str:
                        continue
                        
                    # Parse timestamp
                    try:
                        ts = pd.to_datetime(ts_str)
                    except:
                        continue
                        
                    # Skip standard hourly audits unless they contain a real signal trigger
                    signal = row.get("signal", "")
                    reason = row.get("reason", "")
                    rejection = row.get("rejection", "")
                    
                    if signal == "HOURLY_AUDIT" and reason == "NO_VALID_SIGNALS":
                        # This is a standard idle diagnostic, skip to keep learning clean
                        continue
                        
                    blocked_row = {
                        "timestamp": ts_str,
                        "trading_day": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
                        "session": "UNKNOWN",
                        "instrument_family": "futures" if ticker in ("TMF", "MXF", "TXF") else "options" if ticker.startswith("TXO") else "stock",
                        "strategy_name": signal,
                        "signal_side": "UNKNOWN",
                        "score": 0.0,
                        "momentum": 0.0,
                        "vwap_distance_pts": 0.0,
                        "atr": 0.0,
                        "regime": "UNKNOWN",
                        "execution_status": "blocked" if rejection else "no_entry",
                        "rejection_reason": rejection or reason,
                        "realized_pnl": 0.0,
                        "pnl_pts": 0.0,
                        "friction_cost": 0.0,
                        "supporting_context": {
                            "raw_audit_row": row
                        }
                    }
                    
                    # Try to merge indicator features at this time
                    if indicators_df is not None:
                        # Find closest match within 1 minute
                        # Calculate time delta
                        time_diff = (indicators_df["parsed_time"] - ts).abs()
                        min_diff_idx = time_diff.idxmin()
                        if pd.notna(min_diff_idx) and time_diff.loc[min_diff_idx] <= pd.Timedelta(minutes=5):
                            match_row = indicators_df.loc[min_diff_idx]
                            blocked_row["score"] = float(match_row.get("score", 0.0))
                            blocked_row["momentum"] = float(match_row.get("momentum", 0.0))
                            blocked_row["vwap_distance_pts"] = float(match_row.get("price_vs_vwap", 0.0))
                            blocked_row["atr"] = float(match_row.get("atr", 0.0))
                            blocked_row["regime"] = str(match_row.get("regime", "UNKNOWN"))
                            blocked_row["session"] = "night" if match_row.get("session") == 2 else "day"
                            
                    blocked_rows.append(blocked_row)
                except Exception as e:
                    print(f"Error parsing signals audit row: {e}", file=sys.stderr)
                    
    return blocked_rows

def parse_options_trades():
    """
    Parse options trades from options_trade_ledger.csv if any exist.
    """
    ledger_file = Path("strategies/options/logs/paper_trading/options_trade_ledger.csv")
    if not ledger_file.exists():
        return []
        
    trades = []
    with open(ledger_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                # Options log has columns: trade_id,Timestamp,Mode,Action,Side,Price,Quantity,PnL,Balance,Note
                ts_str = row.get("Timestamp")
                if not ts_str:
                    continue
                    
                trade = {
                    "timestamp": ts_str,
                    "trading_day": ts_str[:10] if ts_str else None,
                    "session": "day" if 8 <= datetime.datetime.fromisoformat(ts_str).hour < 15 else "night",
                    "instrument_family": "options",
                    "strategy_name": row.get("Mode", "options_squeeze"),
                    "signal_side": row.get("Side", "UNKNOWN"),
                    "score": 0.0,
                    "momentum": 0.0,
                    "vwap_distance_pts": 0.0,
                    "atr": 0.0,
                    "regime": "UNKNOWN",
                    "execution_status": "exit" if row.get("Action") in ("EXIT", "CLOSE") else "entered",
                    "rejection_reason": "",
                    "realized_pnl": float(row.get("PnL", 0.0)) if row.get("PnL") else 0.0,
                    "pnl_pts": 0.0,
                    "friction_cost": 20.0,  # default option fee
                    "supporting_context": {
                        "raw_ledger_row": row
                    }
                }
                trades.append(trade)
            except Exception as e:
                print(f"Error parsing options trade ledger row: {e}", file=sys.stderr)
                
    return trades

def build_dataset(date_filter=None):
    """
    Merge all trade and blocked signal sources, sort them, and write to exports/adaptive_dataset.csv.
    """
    all_rows = []
    
    # 1. Parse and add MTS trades
    all_rows.extend(parse_mts_trades())
    
    # 2. Parse and add standard directional trades
    all_rows.extend(parse_standard_trades())
    
    # 3. Parse and add blocked decisions
    all_rows.extend(parse_blocked_signals())
    
    # 4. Parse and add options trades
    all_rows.extend(parse_options_trades())
    
    if not all_rows:
        print("No trade data or blocked signals found to generate dataset.", file=sys.stderr)
        return False
        
    # Apply date filter if specified
    if date_filter:
        all_rows = [r for r in all_rows if r.get("trading_day") == date_filter]
        
    # Sort by timestamp
    all_rows.sort(key=lambda x: x.get("timestamp", ""))
    
    # Convert list of dicts to DataFrame
    df = pd.DataFrame(all_rows)
    
    # Create output directory
    output_dir = Path("exports")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Write canonical dataset
    output_file = output_dir / "adaptive_dataset.csv"
    df.to_csv(output_file, index=False)
    print(f"✅ Canonical dataset successfully materialized: {output_file} ({len(df)} rows)")
    
    # Write day-specific dataset
    if date_filter:
        day_dir = output_dir / "adaptive_datasets"
        day_dir.mkdir(parents=True, exist_ok=True)
        day_file = day_dir / f"adaptive_dataset_{date_filter.replace('-', '')}.csv"
        df.to_csv(day_file, index=False)
        print(f"✅ Session dataset saved: {day_file}")
        
    return True

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="Compile and generate adaptive trade dataset.")
    parser.add_argument("--date", type=str, help="Specific trading day filter (YYYY-MM-DD)")
    args = parser.parse_args()
    
    success = build_dataset(date_filter=args.date)
    sys.exit(0 if success else 1)
