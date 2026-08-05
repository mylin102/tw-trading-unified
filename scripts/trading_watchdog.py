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
if BASE not in sys.path:
    sys.path.insert(0, BASE)
STATE_PATH = "/tmp/mts_position_state.json"
OUT_PATH = "/tmp/trading_watchdog_state.json"
FLAG_PATH = "/tmp/trading_watchdog_alert.flag"
LOG_PATH = os.path.join(BASE, "logs", "watchdog.log")

_ALERT_PRIORITY = {"OK": 0, "RESTART": 1, "FEED_SILENT": 2, "STUCK": 3,
                   "STORM": 4, "DOWN": 5, "POSITION_AT_RISK": 6}


def _escalate(current: str, new: str) -> str:
    """Return the higher-priority alert."""
    if _ALERT_PRIORITY.get(new, 0) > _ALERT_PRIORITY.get(current, 0):
        return new
    return current


STORM_THRESHOLD = 3      # restarts in window -> storm
STORM_WINDOW_S = 600     # 10 min
STALE_OK_S = 300         # state _updated tolerance while market open
RESTART_WINDOW_S = 600
FEED_STALE_S = 120       # quote channel silence threshold (near/far tick+bidask)


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


def _feed_snapshot():
    """Quote-channel health: age of last callback per near/far tick+bidask."""
    try:
        from core.market_data_health_registry import get_health
    except Exception:
        return {"error": "registry_unavailable"}
    out = {}
    now = time.time()
    for code, chan in (("TMFH6", "tick"), ("TMFH6", "bidask"),
                       ("TMFI6", "tick"), ("TMFI6", "bidask")):
        try:
            h = get_health(code, chan)
            if h is None or not h.last_callback_at:
                out[f"{code}_{chan}"] = {"age_s": None, "count": 0}
                continue
            age = now - datetime.fromisoformat(h.last_callback_at).timestamp()
            out[f"{code}_{chan}"] = {"age_s": round(age, 1),
                                     "count": h.callback_count}
        except Exception as e:
            out[f"{code}_{chan}"] = {"error": str(e)}
    return out


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
    fd = _feed_snapshot()
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
        alert = _escalate(alert, "STUCK")
        if alert == "STUCK":
            reasons.append(f"state_stale:{st['updated_age_s']:.0f}s")

    # quote-channel silence (process alive + heartbeat alive + feed silent).
    # Conservative: only alert when the registry HAS recorded callbacks that
    # went stale. An empty registry (TMF ticks route via tick_dispatcher,
    # not GCA) is UNKNOWN — not an alert (avoids false positives). The
    # monitor's own STALE_DATA/P4 machinery covers the TMF path.
    if pm.get("status") == "online" and not fd.get("error"):
        _known = {k: v for k, v in fd.items()
                  if v.get("age_s") is not None and v.get("count", 0) > 0}
        if _known:
            _silent = [k for k, v in _known.items()
                       if v["age_s"] > FEED_STALE_S]
            if _silent and st.get("has_position"):
                alert = "POSITION_AT_RISK"
                reasons.append(f"feed_silent:{','.join(_silent)}")
            elif _silent:
                alert = _escalate(alert, "FEED_SILENT")
                reasons.append(f"feed_silent:{','.join(_silent)}")

    now_iso = datetime.now().isoformat()
    out = {
        "ts": now_iso,
        "alert": alert,
        "reasons": reasons,
        "pm": pm,
        "state": st,
        "feed": fd,
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
