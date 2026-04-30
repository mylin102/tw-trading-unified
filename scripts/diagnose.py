#!/usr/bin/env python3
"""
System Health Check — one-shot diagnostic for trading system.

Usage:
    python3 scripts/diagnose.py

Output:
    Traffic-light status (✅/⚠️/❌) for each subsystem.
    Single actionable conclusion at the end.
"""
import json, os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKET_DATA = ROOT / "logs" / "market_data"
RAW_TICKS = ROOT / "logs" / "raw_ticks"
ROUTER_TRACE = ROOT / "logs" / "router_trace"

os.chdir(ROOT)


def heading(title):
    print(f"\n{'=' * 50}")
    print(f"  {title}")
    print(f"{'=' * 50}")


def ok(msg):
    print(f"  ✅ {msg}")


def warn(msg):
    print(f"  ⚠️  {msg}")


def fail(msg):
    print(f"  ❌ {msg}")


# ── 1. PM2 Status ──
heading("1. PM2 Process Status")
import subprocess
result = subprocess.run(["pm2", "status"], capture_output=True, text=True, timeout=10)
for line in result.stdout.split("\n"):
    if "trading-system" in line:
        parts = [p.strip() for p in line.split("│")]
        if len(parts) >= 9:
            pid = parts[5]
            uptime = parts[6]
            restarts = parts[7]
            status = parts[8]
            mem = parts[10]
            if status == "online":
                ok(f"trading-system  pid={pid}  uptime={uptime}  restarts={restarts}  mem={mem}")
            else:
                fail(f"trading-system  status={status}  restarts={restarts}")
    if "stock-monitor" in line:
        parts = [p.strip() for p in line.split("│")]
        if len(parts) >= 9:
            uptime = parts[6]
            restarts = parts[7]
            ok(f"stock-monitor    uptime={uptime}  restarts={restarts}")

# ── 2. Bar Freshness ──
heading("2. MXF Bar Freshness")
indicator_files = sorted(MARKET_DATA.glob("MXF_2026*_PAPER_indicators.csv"))
if not indicator_files:
    fail("No MXF indicator CSV found")
else:
    import pandas as pd
    latest_file = indicator_files[-1]
    df = pd.read_csv(latest_file, parse_dates=["timestamp"])
    # Filter to the latest trading day only
    latest_tday = df["trading_day"].iloc[-1]
    df_tday = df[df["trading_day"] == latest_tday]
    if df_tday.empty:
        fail(f"No bars for trading day {latest_tday}")
    else:
        latest_ts = df_tday["timestamp"].max()
        now = pd.Timestamp.now(tz="Asia/Taipei")
        latest_ts_pd = pd.Timestamp(latest_ts).tz_localize("Asia/Taipei") if pd.Timestamp(latest_ts).tz is None else pd.Timestamp(latest_ts)
        bar_age_sec = (now - latest_ts_pd).total_seconds()
        bars_today = len(df_tday)
        last_close = df_tday.iloc[-1]["close"]

        if bar_age_sec < 300:
            ok(f"tday={latest_tday}  bars={bars_today}  latest={latest_ts}  ({bar_age_sec:.0f}s ago)  close={last_close:.0f}")
        elif bar_age_sec < 600:
            warn(f"tday={latest_tday}  bars={bars_today}  latest={latest_ts}  ({bar_age_sec:.0f}s ago)  close={last_close:.0f}  — slightly stale")
        else:
            fail(f"tday={latest_tday}  bars={bars_today}  latest={latest_ts}  ({bar_age_sec:.0f}s ago)  close={last_close:.0f}  — STALE > 10min")

# ── 3. Raw Tick Freshness ──
heading("3. Raw Tick Feed")
tick_files = sorted(RAW_TICKS.glob("MXFE6_2026*_ticks.csv"))
if tick_files:
    latest_tick_file = tick_files[-1]
    df_ticks = pd.read_csv(latest_tick_file)
    if not df_ticks.empty:
        latest_epoch = df_ticks["ts_int"].iloc[-1]
        tick_age = time.time() - latest_epoch
        tick_count = len(df_ticks)
        if tick_age < 120:
            ok(f"MXFE6  {tick_count} ticks today  last={(time.time() - latest_epoch):.0f}s ago")
        else:
            warn(f"MXFE6  {tick_count} ticks today  last={(time.time() - latest_epoch):.0f}s ago  — ticks stale")
    else:
        fail("MXFE6 tick CSV is empty")
else:
    warn("No MXFE6 tick CSV for today")

# ── 4. Router Trace ──
heading("4. Router Trace (last 5 entries)")
trace_files = sorted(ROUTER_TRACE.glob("router_trace_*.jsonl"), reverse=True)
if trace_files:
    trace_path = trace_files[0]
    lines = trace_path.read_text().strip().split("\n")
    # Parse last 5 traces
    last_traces = []
    for line in lines[-5:]:
        try:
            t = json.loads(line)
            last_traces.append(t)
        except json.JSONDecodeError:
            continue
    if last_traces:
        for t in last_traces[-5:]:
            ts = t.get("ts", "?")[-19:-3]  # trim to HH:MM:SS
            regime = t.get("regime", "?")
            selected = t.get("selected")
            strategies = []
            for s in t.get("strategies", []):
                if s.get("triggered"):
                    strategies.append(f"{s['name']}=✅TRADE")
                else:
                    reason = s.get("skip_reason", "?")
                    strategies.append(f"{s['name']}=⛔{reason}")
            sel_str = f"selected={selected}" if selected else "selected=None"
            print(f"  [{ts}]  regime={regime}  {sel_str}")
            for st in strategies:
                print(f"          {st}")
    else:
        warn("Router trace file has content but no parseable entries")
else:
    warn("No router trace files found (monitor may not have run with Phase 5 code yet)")

# ── 5. Log Errors (last 24h) ──
heading("5. Recent Errors (last 50 lines)")
pm2_log = Path(os.path.expanduser("~/.pm2/logs/trading-system-out.log"))
if pm2_log.exists():
    # Read last 50 lines and look for obvious errors
    result = subprocess.run(
        ["tail", "-50", str(pm2_log)],
        capture_output=True, text=True, timeout=5
    )
    lines = result.stdout.split("\n")
    errors = [l for l in lines if any(kw in l.lower() for kw in ["error", "traceback", "exception", "reset by peer", "unreachable", "stale", "stagnant", "oom", "killed"])]
    if errors:
        for e in errors[-5:]:
            print(f"  {e.strip()[:120]}")
    else:
        ok("No notable errors in last 50 lines")
else:
    fail("PM2 out log not found")

# ── 6. Conclusion ──
heading("Conclusion")
bar_age_result = None
if indicator_files:
    df = pd.read_csv(indicator_files[-1], parse_dates=["timestamp"])
    df_tday = df[df["trading_day"] == df["trading_day"].iloc[-1]]
    if not df_tday.empty:
        latest_ts = df_tday["timestamp"].max()
        bar_age_sec = (pd.Timestamp.now(tz="Asia/Taipei") - pd.Timestamp(latest_ts).tz_localize("Asia/Taipei")).total_seconds()
        bar_age_result = bar_age_sec

tick_age_result = None
if tick_files:
    df_ticks = pd.read_csv(tick_files[-1])
    if not df_ticks.empty:
        tick_age_result = time.time() - df_ticks["ts_int"].iloc[-1]

if bar_age_result is None:
    print("  ❌  No bar data available. System may have just started.")
elif bar_age_result < 300:
    print(f"  ✅  System healthy. Bars {bar_age_result:.0f}s old, ticks flowing.")
elif bar_age_result < 600:
    print(f"  ⚠️  Bars slightly stale ({bar_age_result:.0f}s). Likely VPN reconnect — wait for next 5m boundary.")
elif bar_age_result > 600 and tick_age_result is not None and tick_age_result < 120:
    print(f"  ⚠️  Bars stale ({bar_age_result:.0f}s) but ticks flowing → bar builder may not be accumulating.")
    print(f"       Check: is _strategy_tick running? Is _tick_bars_deque receiving bars?")
elif bar_age_result > 600:
    print(f"  ❌  Data STAGNANT. Bars {bar_age_result:.0f}s old, ticks not flowing.")
    print(f"       Check: VPN, PM2 uptime, Shioaji contract subscription.")
