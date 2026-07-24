#!/usr/bin/env python3
"""
Cron job: update calendar spread CSV with fresh near/far kbar data.

Intended to run every 5 minutes via cron or PM2 cron_restart.
Uses atomic write (temp → rename) so SpreadLoader hot-reload sees a complete file.

Usage:
    python scripts/update_calendar_spread.py [--days 2]

Design principles:
    - Independent process: does NOT import from main trading loop
    - Atomic CSV write: writes to temp file, then renames
    - Silent exit on failure: no crash, no cascade
    - Logs to stdout for PM2 capture
"""

import os
import sys
import argparse
import tempfile
import shutil
import yaml
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.shioaji_session import get_api
from core.broker.shioaji_compat import kbars_to_dataframe
from core.date_utils import is_night_session


# ── Config ─────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
SPREAD_WINDOW = 20  # rolling window for spread_z

# ── Config Loading (GSD: Zero Hardcoding) ──
def load_yaml(path: str):
    if not os.path.exists(path): return {}
    with open(path, "r") as f:
        return yaml.safe_load(f)

_IS_NIGHT = is_night_session(datetime.now())
_CFG_NAME = "futures_night.yaml" if _IS_NIGHT else "futures.yaml"
_CFG = load_yaml(os.path.join(PROJECT_ROOT, "config", _CFG_NAME))

TICKER = _CFG.get("ticker")
if not TICKER:
    print(f"ERROR: 'ticker' missing in {_CFG_NAME}")
    sys.exit(1)


# ── Helpers ────────────────────────────────────────────────────────────────

def _dedup_contracts(contracts):
    """Shioaji returns duplicate contracts; dedup by code."""
    seen = set()
    result = []
    for c in contracts:
        if c.code not in seen:
            seen.add(c.code)
            result.append(c)
    return result


def get_near_far(api, category=None, days_to_switch=3):
    """Get near and far month contracts (unique, non-rolling).
    
    Skips contracts within ``days_to_switch`` of expiry, matching
    ``ContractResolver.get_near_far_contracts()`` logic.
    """
    if category is None:
        category = TICKER
    
    # 2026-07-23 Gemini CLI: Robust contract resolution with fallback scan
    contracts = []
    try:
        if hasattr(api.Contracts, "Futures") and category in api.Contracts.Futures:
            contracts = list(api.Contracts.Futures[category])
        else:
            # Force contract fetch retry (Shioaji 1.5.5 compat: no kwargs)
            try:
                api.fetch_contracts()
            except Exception:
                api.fetch_contracts()
            # Retry direct access after fetch
            if hasattr(api.Contracts, "Futures") and category in api.Contracts.Futures:
                contracts = list(api.Contracts.Futures[category])
    except Exception as err:
        print(f"WARN: Direct contract fetch failed for {category}: {err}")

    # Group scan fallback (runs regardless of whether direct access succeeded)
    if not contracts:
        print(f"WARN: Direct contract access failed for {category}, trying group scan...")
        try:
            for grp in api.Contracts.Futures:
                for c in grp:
                    code = getattr(c, "code", "") or ""
                    symbol = getattr(c, "symbol", "") or ""
                    if code.startswith(category) or symbol.startswith(category):
                        contracts.append(c)
        except Exception as err:
            print(f"WARN: Group scan failed: {err}")
        if contracts:
            print(f"Group scan found {len(contracts)} matching contracts for {category}")

    if not contracts:
        print(f"ERROR: Product {category} not found in Shioaji Contracts")
        return None, None
        
    regular = [c for c in contracts if not c.code.endswith(("R1", "R2", "R3"))]
    unique = _dedup_contracts(regular)
    sorted_c = sorted(unique, key=lambda c: c.delivery_date)

    # Filter out contracts within days_to_switch of expiry (may return empty kbars)
    today = datetime.now()
    available = []
    for c in sorted_c:
        try:
            ddate = datetime.strptime(str(c.delivery_date).replace("-", "/"), "%Y/%m/%d")
            if (ddate - today).days > days_to_switch:
                available.append(c)
        except (ValueError, AttributeError):
            continue

    if len(available) < 2:
        # Fallback: try without expiry filter — maybe the data API still works
        print(f"WARN: Only {len(available)} contracts survive expiry filter; falling back to raw sort")
        available = sorted_c

    if len(available) < 2:
        print(f"ERROR: Not enough {category} contracts: {len(available)}")
        return None, None
    return available[0], available[1]


def fetch_kbars(api, contract, days=2):
    """Fetch kbars and return DataFrame with ts column."""
    end = datetime.now()
    start = end - timedelta(days=days)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    kbars = api.kbars(contract=contract, start=start_str, end=end_str)
    df = kbars_to_dataframe(kbars)
    if df.empty:
        print(f"WARN: Empty kbars for {contract.code}")
        return None
    df = df.reset_index()
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def calculate_spread_metrics(df_near, df_far):
    """Merge near/far, compute spread_z and vwap_z. Same logic as fetch_calendar_spread_data.py."""
    near = df_near[["ts", "Close"]].copy()
    far = df_far[["ts", "Close"]].copy()
    near.rename(columns={"Close": "Close_near"}, inplace=True)
    far.rename(columns={"Close": "Close_far"}, inplace=True)

    merged = pd.merge(near, far, on="ts", how="inner")
    if merged.empty:
        print("ERROR: No overlapping timestamps between near and far")
        return None

    merged.sort_values("ts", inplace=True)
    merged.reset_index(drop=True, inplace=True)

    merged["spread"] = merged["Close_near"] - merged["Close_far"]

    window = SPREAD_WINDOW
    merged["spread_ma"] = merged["spread"].rolling(window=window, min_periods=window).mean()
    merged["spread_std"] = merged["spread"].rolling(window=window, min_periods=window).std()
    safe_std = merged["spread_std"].replace(0, pd.NA)
    merged["spread_z"] = (merged["spread"] - merged["spread_ma"]) / safe_std

    # 2026-07-09 Hermes Agent: Calculate Spread EMA 20 and EMA 60 for trend direction
    merged["spread_ema_20"] = merged["spread"].ewm(span=20, adjust=False).mean()
    merged["spread_ema_60"] = merged["spread"].ewm(span=60, adjust=False).mean()

    merged["vwap"] = merged["Close_near"].rolling(window=window, min_periods=window).mean()
    merged["vwap_std"] = merged["Close_near"].rolling(window=window, min_periods=window).std()
    safe_vwap_std = merged["vwap_std"].replace(0, pd.NA)
    merged["vwap_z"] = (merged["Close_near"] - merged["vwap"]) / safe_vwap_std

    return merged


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Update calendar spread CSV")
    parser.add_argument("--days", type=int, default=2, help="Days of history to fetch")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Override ticker from config (e.g. tmf)")
    args = parser.parse_args()

    # 1. Override ticker if explicitly provided (GSD: explicit > config default)
    if args.ticker:
        global TICKER
        TICKER = args.ticker.upper()
        print(f"[CalendarSpread] Ticker overridden to {TICKER} via --ticker")
    try:
        api = get_api()
    except Exception as e:
        print(f"ERROR: Login failed: {e}")
        return 1

    # 2. Resolve contracts
    near, far = get_near_far(api, TICKER)
    if not near or not far:
        return 1
    print(f"[{TICKER}] Near: {near.code} delivery={near.delivery_date}")
    print(f"[{TICKER}] Far:  {far.code} delivery={far.delivery_date}")

    # 3. Fetch data
    df_near = fetch_kbars(api, near, days=args.days)
    if df_near is None:
        print(f"ERROR: Failed to fetch {TICKER} near data")
        return 1
    df_far = fetch_kbars(api, far, days=args.days)
    if df_far is None:
        print(f"ERROR: Failed to fetch {TICKER} far data")
        return 1
    print(f"Near bars: {len(df_near)}, Far bars: {len(df_far)}")

    # 4. Compute spread
    df_spread = calculate_spread_metrics(df_near, df_far)
    if df_spread is None or df_spread.empty:
        print("ERROR: Spread calculation failed")
        return 1
    print(f"Spread bars: {len(df_spread)}, latest spread_z={df_spread['spread_z'].iloc[-1]:.2f}")

    # 5. Atomic write: temp → rename
    today_str = datetime.now().strftime("%Y%m%d")
    ticker_lower = TICKER.lower()
    final_path = os.path.join(DATA_DIR, f"{ticker_lower}_calendar_spread_{today_str}.csv")
    os.makedirs(DATA_DIR, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        suffix=".csv",
        prefix=f"{ticker_lower}_calendar_spread_{today_str}_",
        dir=DATA_DIR,
    )
    try:
        df_spread.to_csv(tmp_path, index=False)
        os.close(fd)
        shutil.move(tmp_path, final_path)
        print(f"OK: Written {final_path} ({len(df_spread)} rows)")
    except Exception as e:
        os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        print(f"ERROR: Write failed: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
