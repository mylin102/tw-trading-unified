# External watchdog tests (2026-08-05 INCIDENT #3).
# Pure-logic tests of scripts/trading_watchdog decision table via
# subprocess-free import harness (monkeypatch _pm2_snapshot/_state_snapshot).
import sys
import os
import json
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

import trading_watchdog as wd


@pytest.fixture()
def run(monkeypatch, tmp_path):
    """Run main() with controlled pm/state snapshots; return output dict."""
    outputs = {}

    def _make(pm, st, baseline_pm=None):
        monkeypatch.setattr(wd, "_pm2_snapshot", lambda: pm)
        monkeypatch.setattr(wd, "_state_snapshot", lambda: st)
        monkeypatch.setattr(wd, "OUT_PATH", str(tmp_path / "state.json"))
        monkeypatch.setattr(wd, "FLAG_PATH", str(tmp_path / "alert.flag"))
        monkeypatch.setattr(wd, "LOG_PATH", str(tmp_path / "watchdog.log"))
        if baseline_pm is not None:
            # seed baseline (e.g. previous restarts count) then run target
            monkeypatch.setattr(wd, "_pm2_snapshot", lambda: baseline_pm)
            wd.main()
            monkeypatch.setattr(wd, "_pm2_snapshot", lambda: pm)
        wd.main()
        out = json.loads(open(str(tmp_path / "state.json")).read())
        outputs[out["alert"]] = out
        return out

    return _make


def test_healthy_no_alert(run):
    pm = {"status": "online", "restarts": 181, "uptime_s": 1000, "unstable": 0}
    st = {"has_position": False, "trade_id": None, "state": "HEARTBEAT",
          "updated_age_s": 1}
    out = run(pm, st)
    assert out["alert"] == "OK"


def test_position_open_no_alert_when_healthy(run):
    pm = {"status": "online", "restarts": 181, "uptime_s": 1000, "unstable": 0}
    st = {"has_position": True, "trade_id": "T1", "state": "HOLDING_SPREAD",
          "updated_age_s": 1}
    out = run(pm, st)
    assert out["alert"] == "OK"  # position alone is not an alert


def test_restart_storm_alert_with_position(run):
    base = {"status": "online", "restarts": 181, "uptime_s": 1000, "unstable": 0}
    pm = {"status": "online", "restarts": 184, "uptime_s": 100, "unstable": 3}
    st = {"has_position": True, "trade_id": "T1", "state": "HOLDING_SPREAD",
          "updated_age_s": 1}
    out = run(pm, st, baseline_pm=base)
    assert out["alert"] == "POSITION_AT_RISK"
    assert any("storm" in r for r in out["reasons"])


def test_process_down_alert(run):
    pm = {"status": "errored", "restarts": 181, "uptime_s": 0, "unstable": 0}
    st = {"has_position": False, "trade_id": None, "state": "HEARTBEAT",
          "updated_age_s": 1}
    out = run(pm, st)
    assert out["alert"] in ("DOWN",)


def test_process_down_with_position_critical(run):
    pm = {"status": "errored", "restarts": 181, "uptime_s": 0, "unstable": 0}
    st = {"has_position": True, "trade_id": "T1", "state": "HOLDING_SPREAD",
          "updated_age_s": 1}
    out = run(pm, st)
    assert out["alert"] == "POSITION_AT_RISK"


def test_stale_state_alert(run):
    pm = {"status": "online", "restarts": 181, "uptime_s": 1000, "unstable": 0}
    st = {"has_position": False, "trade_id": None, "state": "HEARTBEAT",
          "updated_age_s": 600}
    out = run(pm, st)
    assert out["alert"] == "STUCK"
