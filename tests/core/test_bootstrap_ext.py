import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest


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
    profile.write_text("live_trading: true\nconfig_profile: futures_live\n")
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


# 2026-10-01 codex: real CLI, explicit broker-only reconciliation, never LIVE.
def _current_single_leg(tmp_path, *, apply=False, changes=None, active=False):
    rt = tmp_path/'runtime'
    (rt/'logs').mkdir(parents=True)
    ctx = {'requested_mode':'live','effective_mode':'live_quarantined',
           'live_order_allowed':False,'audit_reasons':['SESSION_LOGOUT','BROKER_NOT_FLAT'],
           'canonical_holding':{'canonical_legs':[
               {'code':'TMFJ6','side':'SHORT','qty':1,'avg_cost':48176.},
               {'code':'TMFK6','side':'LONG','qty':1,'avg_cost':48320.}]},
           'gateway_intents':{'receipt':{'state':'FILLED','broker_order_id':'e88c0e3c'}}}
    ctx_path=rt/'execution_context.json'
    ctx_path.write_text(json.dumps(ctx))
    before=ctx_path.read_bytes()
    row={'account':'futures','code':'TMFK6','direction':'Action.Buy',
         'quantity':1,'avg_cost':48428.}
    snap={'source':'live_broker','mode':'live','scope':'futopt',
          'account_identity_hash':'a'*64,'session_id':'b'*32,
          'captured_at':int(time.time()*1000),'fetch_status':{'capture':'OK'},
          'positions':[row],'open_orders':[],'available_margin':1000000.,
          'canonical_input_hash':None}
    snap.update(changes or {})
    snap['canonical_input_hash']=hashlib.sha256(json.dumps(
        snap,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
    path=tmp_path/'snapshot.json'
    path.write_text(json.dumps(snap))
    profile=tmp_path/'futures_live.yaml'
    profile.write_text('live_trading: true\nconfig_profile: futures_live\n')
    pid=tmp_path/'owner.pid'
    if active:
        import os
        pid.write_text(str(os.getpid()))
    args=[sys.executable,'-B',str(SCRIPT),'--runtime-dir',str(rt),'--pid-file',str(pid),
          '--position-state',str(path),'--margin-evidence',str(path),
          '--config-profile',str(profile),'--config-hash',hashlib.sha256(profile.read_bytes()).hexdigest(),
          '--allow-existing-mts-position','--reconcile-current-single-leg','TMFK6','buy','1','48428']
    if apply:args.append('--apply')
    result=subprocess.run(args,capture_output=True,text=True)
    return result,ctx_path,before


def test_explicit_current_single_leg_cli_dryrun_keeps_ctx(tmp_path):
    result,path,before=_current_single_leg(tmp_path)
    assert result.returncode==0,result.stdout+result.stderr
    assert path.read_bytes()==before


def test_explicit_current_single_leg_cli_apply_reconciles_without_live(tmp_path):
    result,path,_=_current_single_leg(tmp_path,apply=True)
    assert result.returncode==0,result.stdout+result.stderr
    ctx=json.loads(path.read_text())
    assert ctx['canonical_holding']['canonical_legs']==[
        {'code':'TMFK6','side':'LONG','qty':1,'avg_cost':48428.}]
    assert ctx['effective_mode']=='live_quarantined' and ctx['live_order_allowed'] is False
    assert ctx['session_id'] is None
    assert ctx['gateway_intents']['receipt']['broker_order_id']=='e88c0e3c'
    assert list((path.parent/'logs/context_history').glob('*.json'))


@pytest.mark.parametrize('changes',[
    {'source':'paper'}, {'captured_at':1}, {'captured_at':int(time.time()*1000)+3600000},
    {'fetch_status':{'capture':'FAILED'}}, {'open_orders':[{'account':'futures','code':'TMFK6'}]},
    {'positions':[]}, {'positions':[dict(_position(),code='OTHER')]},
    {'available_margin':0}, {'scope':'stock'}, {'account_identity_hash':None},
])
def test_explicit_current_single_leg_cli_bad_proof_no_write(tmp_path,changes):
    result,path,before=_current_single_leg(tmp_path,apply=True,changes=changes)
    assert result.returncode!=0
    assert path.read_bytes()==before


def test_explicit_current_single_leg_active_owner_still_refused(tmp_path):
    result,path,before=_current_single_leg(tmp_path,apply=True,active=True)
    assert result.returncode!=0 and 'ACTIVE' in result.stderr
    assert path.read_bytes()==before
