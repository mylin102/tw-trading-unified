import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/deployment/redeploy_bootstrap.py"


def _run(tmp_path, positions, flag=False, canonical=None):
    rt = tmp_path / "runtime"
    (rt / "logs").mkdir(parents=True)
    ctx = {"audit_reasons": ["SESSION_LOGOUT"],
           "effective_mode": "live_quarantined", "requested_mode": "live",
           "live_order_allowed": False}
    if canonical is not None:
        ctx["exit_only_capability"] = {"legs": canonical}
    (rt / "execution_context.json").write_text(json.dumps(ctx))
    snap = tmp_path / "snapshot.json"
    snap.write_text(json.dumps({
        "source": "live_broker", "mode": "live", "positions": positions,
        "open_orders": [], "captured_at": int(time.time() * 1000),
        "canonical_input_hash": "b" * 64, "session_id": "s",
        "account_identity_hash": "a" * 64}))
    profile = tmp_path / "futures_live.yaml"
    profile.write_text("live_trading: true\n")
    args = [sys.executable, "-B", str(SCRIPT), "--runtime-dir", str(rt),
            "--pid-file", str(tmp_path / "missing.pid"),
            "--position-state", str(snap), "--margin-evidence", str(snap),
            "--config-profile", str(profile), "--config-hash",
            hashlib.sha256(profile.read_bytes()).hexdigest()]
    if flag:
        args.append("--allow-existing-mts-position")
    return subprocess.run(args, capture_output=True, text=True)


def _position():
    return {"account": "futures", "code": "TXFH6", "quantity": 1,
            "direction": "Action.Buy", "avg_cost": 100}


def test_bootstrap_flag_accepts_stale_single_leg(tmp_path):
    r = _run(tmp_path, [_position()], flag=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DRY_RUN" in r.stdout


def test_bootstrap_without_flag_rejects_single_leg(tmp_path):
    r = _run(tmp_path, [_position()])
    assert r.returncode != 0
    assert "GUARD_POSITION_NOT_FLAT" in r.stdout + r.stderr


def test_bootstrap_rejects_one_leg_against_stale_two_leg_canonical(tmp_path):
    legs = [{"symbol": "TXFH6", "side": "buy", "remaining_qty": 1,
             "avg_cost": 100},
            {"symbol": "TXFM6", "side": "sell", "remaining_qty": 1,
             "avg_cost": 101}]
    r = _run(tmp_path, [_position()], flag=True, canonical=legs)
    assert r.returncode != 0
    assert "GUARD_POSITION_NOT_FLAT" in r.stdout + r.stderr
