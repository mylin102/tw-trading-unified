"""Trading system external watchdog (2026-08-05, INCIDENT #3 design).

Passive supervision of the trading process + broker session state.
READ-ONLY: never touches broker, never stops/restarts the process when a
position is open. Writes alert flag + JSON state for dashboard/alerting.

Usage: cron every 2 min or launchd:
  .venv/bin/python3 scripts/trading_watchdog.py >> logs/watchdog.log 2>&1
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = "/tmp/mts_position_state.json"
OUT_PATH = "/tmp/trading_watchdog_state.json"
FLAG_PATH = "/tmp/trading_watchdog_alert.flag"
LOG_PATH = os.path.join(BASE, "logs", "watchdog.log")

STORM_THRESHOLD = 3      # restarts in window -> storm
STORM_WINDOW_S = 600     # 10 min
STALE_OK_S = 300         # state _updated tolerance while market open
RESTART_WINDOW_S = 600


def _pm2_snapshot():
    try:
        out = subprocess.run(["pm2", "jlist"], capture_output=True, text=True,
                             timeout=15).stdout
        procs = json.loads(out)
        for p in procs:
            if p.get("name") == "trading-system":
                env = p["pm2_env"]
                return {
                    "status": env.get("status"),
                    "restarts": env.get("restart_time", 0),
                    "uptime_s": max(0, (time.time() * 1000 - env.get("pm_uptime", 0)) / 1000),
                    "unstable": env.get("unstable_restarts", 0),
                }
    except Exception as e:
        return {"error": str(e)}
    return {"status": "NOT_FOUND"}


def _state_snapshot():
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
        return {
            "has_position": bool(d.get("has_position")),
            "trade_id": d.get("trade_id"),
            "state": d.get("state"),
            "updated": d.get("_updated"),
            "updated_age_s": (time.time() - datetime.fromisoformat(d["_updated"]).timestamp())
                             if d.get("_updated") else None,
        }
    except Exception as e:
        return {"error": str(e)}


def _load_prev():
    try:
        with open(OUT_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    pm = _pm2_snapshot()
    st = _state_snapshot()
    prev = _load_prev()

    alert = "OK"
    reasons = []
    prev_restarts = int(prev.get("pm", {}).get("restarts", 0) or 0)
    first_run = prev_restarts == 0
    restarts_delta = int(pm.get("restarts", 0)) - prev_restarts if not first_run else 0

    # restart storm (delta within window) — skip on first run (baseline)
    if not first_run and restarts_delta >= STORM_THRESHOLD:
        alert = "STORM"
        reasons.append(f"restart_storm: +{restarts_delta} in window")
    if pm.get("status") in ("stopped", "errored", "NOT_FOUND"):
        alert = "DOWN"
        reasons.append(f"process:{pm.get('status')}")
    if st.get("has_position"):
        if alert != "OK":
            alert = "POSITION_AT_RISK"
            reasons.append("position_open")
    if st.get("updated_age_s") is not None and st["updated_age_s"] > STALE_OK_S \
            and pm.get("status") == "online":
        alert = max(alert, "STUCK") if alert in ("OK", "RESTART") else alert
        if alert == "STUCK":
            reasons.append(f"state_stale:{st['updated_age_s']:.0f}s")

    now_iso = datetime.now().isoformat()
    out = {
        "ts": now_iso,
        "alert": alert,
        "reasons": reasons,
        "pm": pm,
        "state": st,
        "restarts_delta": restarts_delta,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=1, default=str)

    # flag file (dashboard banner reads this)
    if alert != "OK":
        with open(FLAG_PATH, "w") as f:
            f.write(json.dumps(out, default=str))
        with open(LOG_PATH, "a") as f:
            f.write(f"{now_iso} ALERT={alert} {reasons}\n")
    else:
        if os.path.exists(FLAG_PATH):
            os.unlink(FLAG_PATH)

    # dedup: only log transitions
    if prev.get("alert") != alert:
        with open(LOG_PATH, "a") as f:
            f.write(f"{now_iso} {alert}: {reasons} | pm={pm.get('status')} "
                    f"restarts={pm.get('restarts')} has_pos={st.get('has_position')}\n")
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
