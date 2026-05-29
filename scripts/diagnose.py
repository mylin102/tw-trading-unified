#!/usr/bin/env python3
"""
System Health Check — one-shot diagnostic for trading system.

Usage:
    python3 scripts/diagnose.py

Output:
    Traffic-light status (✅/⚠️/❌) for each subsystem.
    Single actionable conclusion at the end.
"""
import json
import os
import sys
import time
import subprocess
import traceback
import yaml
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MARKET_DATA = ROOT / "logs" / "market_data"
RAW_TICKS = ROOT / "logs" / "raw_ticks"
ROUTER_TRACE = ROOT / "logs" / "router_trace"

os.chdir(ROOT)

# ── Config Loading (GSD: Zero Hardcoding) ──
def load_yaml(path: Path):
    if not path.exists(): return {}
    with open(path, "r") as f:
        return yaml.safe_load(f)

from core.date_utils import is_night_session
_IS_NIGHT = is_night_session(datetime.now())
_CFG_NAME = "futures_night.yaml" if _IS_NIGHT else "futures.yaml"
_CFG = load_yaml(ROOT / "config" / _CFG_NAME)

TICKER = _CFG.get("ticker")
if not TICKER:
    print(f"❌ Error: 'ticker' missing in {_CFG_NAME}")
    sys.exit(1)

# Map ticker to hot-month tick symbol (e.g., TMF -> TMFE6, MXF -> MXFE6)
# GSD: In Shioaji, hot month is usually E6 (June), G6 (July) etc.
# We'll detect it from the filesystem or fallback to E6 for now.
_TICK_SYMBOL = f"{TICKER}E6" 

# ── Thresholds (tunable) ──
BAR_FRESH_OK_SEC = 600
BAR_FRESH_WARN_SEC = 900
TICK_FRESH_OK_SEC = 120


def heading(title: str):
    print(f"\n{'=' * 56}")
    print(f"  {title}")
    print(f"{'=' * 56}")


def ok(msg: str):
    print(f"  ✅ {msg}")


def warn(msg: str):
    print(f"  ⚠️  {msg}")


def fail(msg: str):
    print(f"  ❌ {msg}")


def safe_read_csv(path: Path, **kwargs):
    """Read CSV with error handling. Returns (df, error_str)."""
    try:
        if not path.exists():
            return None, f"File not found: {path.name}"
        import pandas as pd
        df = pd.read_csv(path, **kwargs)
        return df, None
    except Exception as e:
        return None, f"Read failed: {e}"


# ═══════════════════════════════════════════
#  Checks
# ═══════════════════════════════════════════

def check_pm2():
    heading("1. PM2 Process Status")
    try:
        # Use jlist for robust JSON parsing
        result = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            fail(f"pm2 jlist returned code {result.returncode}")
            return
        
        processes = json.loads(result.stdout)
        target_names = ["trading-system", "dashboard", "stock-monitor"]
        
        for proc in processes:
            name = proc.get("name")
            if name in target_names:
                status = proc.get("pm2_env", {}).get("status")
                restarts = proc.get("pm2_env", {}).get("restart_time", 0)
                pid = proc.get("pid", "N/A")
                
                # Format memory
                mem_bytes = proc.get("monit", {}).get("memory", 0)
                mem_str = f"{mem_bytes / 1024 / 1024:.1f}MB"
                
                # Format uptime
                pm_uptime = proc.get("pm2_env", {}).get("pm_uptime", 0)
                if pm_uptime > 0:
                    uptime_sec = (time.time() * 1000 - pm_uptime) / 1000
                    if uptime_sec > 3600:
                        uptime_str = f"{uptime_sec / 3600:.1f}h"
                    elif uptime_sec > 60:
                        uptime_str = f"{uptime_sec / 60:.1f}m"
                    else:
                        uptime_str = f"{uptime_sec:.0f}s"
                else:
                    uptime_str = "0s"

                msg = f"{name:<15}  pid={pid:<6}  uptime={uptime_str:<5}  restarts={restarts:<3}  mem={mem_str}"
                if status == "online":
                    ok(msg)
                else:
                    fail(f"{name:<15}  status={status:<8}  restarts={restarts}")
                    
    except subprocess.TimeoutExpired:
        fail("pm2 jlist timed out")
    except json.JSONDecodeError:
        fail("Failed to parse pm2 jlist output")
    except FileNotFoundError:
        fail("pm2 not installed or not in PATH")
    except Exception as e:
        fail(f"PM2 check error: {e}")


def check_bar_freshness():
    """Returns bar_age_sec or None on failure."""
    heading(f"2. {TICKER} Bar Freshness")
    try:
        files = sorted(MARKET_DATA.glob(f"{TICKER}_2026*_PAPER_indicators.csv"))
        if not files:
            fail(f"No {TICKER} indicator CSV found")
            return None

        df, err = safe_read_csv(files[-1], parse_dates=["timestamp"])
        if err:
            fail(err)
            return None

        import pandas as pd
        # Use timestamp-based trading day, not row-based — last CSV row may be old
        latest_ts = df["timestamp"].max()
        tday = df[df["timestamp"] == latest_ts]["trading_day"].iloc[0]
        df_tday = df[df["trading_day"] == tday]
        if df_tday.empty:
            fail(f"No bars for trading day {tday}")
            return None

        latest_ts = df_tday["timestamp"].max()
        now = pd.Timestamp.now(tz="Asia/Taipei")
        ts_pd = pd.Timestamp(latest_ts)
        if ts_pd.tz is None:
            ts_pd = ts_pd.tz_localize("Asia/Taipei")
        age = (now - ts_pd).total_seconds()
        nbars = len(df_tday)
        close = df_tday.iloc[-1]["close"]

        msg = f"tday={tday}  bars={nbars}  latest={latest_ts}  ({age:.0f}s ago)  close={close:.0f}"
        if age < BAR_FRESH_OK_SEC:
            ok(msg)
        elif age < BAR_FRESH_WARN_SEC:
            warn(f"{msg}  — slightly stale")
        else:
            fail(f"{msg}  — STALE > {BAR_FRESH_WARN_SEC // 60}min")
        return age
    except Exception as e:
        fail(f"Bar freshness check error: {e}")
        return None


def check_raw_ticks():
    """Returns tick_age_sec or None."""
    heading("3. Raw Tick Feed")
    try:
        # Detect the actual symbol used in the filesystem for today
        # Pattern: {TICKER}??_YYYYMMDD_ticks.csv
        today_str = time.strftime("%Y%m%d")
        potential_files = list(RAW_TICKS.glob(f"{TICKER}*_ticks.csv"))
        if not potential_files:
            warn(f"No {TICKER}* tick CSV found")
            return None
        
        # Sort by modification time to get the most recent one
        potential_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        target_file = potential_files[0]
        actual_symbol = target_file.name.split('_')[0]

        df, err = safe_read_csv(target_file)
        if err:
            fail(err)
            return None
        if df.empty:
            fail(f"{actual_symbol} tick CSV is empty")
            return None

        n = len(df)
        last_epoch = df["ts_int"].iloc[-1]
        age = time.time() - last_epoch

        if age < TICK_FRESH_OK_SEC:
            ok(f"{actual_symbol}  {n} ticks today  last={age:.0f}s ago")
        else:
            warn(f"{actual_symbol}  {n} ticks today  last={age:.0f}s ago  — stale")
        return age
    except Exception as e:
        fail(f"Tick check error: {e}")
        return None


def check_router_trace():
    heading("4. Router Trace (last 5 entries)")
    try:
        files = sorted(ROUTER_TRACE.glob("router_trace_*.jsonl"), reverse=True)
        if not files:
            warn("No router trace files found")
            return

        lines = files[0].read_text().strip().split("\n")
        traces = []
        for line in lines[-5:]:
            try:
                traces.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        if not traces:
            warn("Router trace file has no parseable entries")
            return

        for t in traces[-5:]:
            ts = str(t.get("ts", "?"))
            if len(ts) >= 19:
                ts = ts[-19:-3]  # trim to HH:MM:SS
            regime = t.get("regime", "?")
            selected = t.get("selected")
            sel_str = f"selected={selected}" if selected else "selected=None"
            print(f"  [{ts}]  regime={regime}  {sel_str}")
            for s in t.get("strategies", []):
                if s.get("triggered"):
                    print(f"          {s['name']}=✅TRADE")
                else:
                    reason = s.get("skip_reason", "?")
                    print(f"          {s['name']}=⛔{reason}")
    except Exception as e:
        fail(f"Router trace error: {e}")


def check_recent_errors():
    heading("5. Recent Errors (last 50 lines)")
    try:
        log = Path(os.path.expanduser("~/.pm2/logs/trading-system-out.log"))
        if not log.exists():
            fail("PM2 out log not found")
            return

        result = subprocess.run(["tail", "-50", str(log)], capture_output=True, text=True, timeout=5)
        keywords = ["error", "traceback", "exception", "reset by peer", "unreachable",
                     "stale", "stagnant", "oom", "killed"]
        errors = [l for l in result.stdout.splitlines()
                  if any(kw in l.lower() for kw in keywords)]
        if errors:
            for e in errors[-5:]:
                print(f"  {e.strip()[:120]}")
        else:
            ok("No notable errors in last 50 lines")
    except subprocess.TimeoutExpired:
        fail("tail timed out")
    except Exception as e:
        fail(f"Error check error: {e}")


# ═══════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════

def main():
    heading("Trading System Health Check")
    print(f"  Run at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    bar_age = None
    tick_age = None

    try:
        check_pm2()
        bar_age = check_bar_freshness()
        tick_age = check_raw_ticks()
        check_router_trace()
        check_recent_errors()
    except KeyboardInterrupt:
        print("\n  ⚠️  Interrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n  ❌ Unexpected error: {e}")
        traceback.print_exc()
        sys.exit(1)

    # ── Conclusion ──
    heading("Conclusion")
    if bar_age is None:
        print("  ❌  No bar data. System may have just started or data pipeline is down.")
    elif bar_age < BAR_FRESH_OK_SEC:
        print(f"  ✅  System HEALTHY. Bars fresh ({bar_age:.0f}s), ticks flowing.")
    elif bar_age < BAR_FRESH_WARN_SEC:
        print(f"  ⚠️  Bars slightly stale ({bar_age:.0f}s). Likely VPN reconnect — wait for next 5m boundary.")
    elif tick_age is not None and tick_age < TICK_FRESH_OK_SEC:
        print(f"  ⚠️  Bars stale ({bar_age:.0f}s) but ticks flowing → bar builder not accumulating.")
        print("       Check: _strategy_tick running? _tick_bars_deque receiving bars?")
    else:
        print(f"  ❌  DATA STAGNANT. Bars {bar_age:.0f}s old, ticks not flowing.")
        print("       Check: VPN → Shioaji subscription → PM2 processes.")


if __name__ == "__main__":
    main()
