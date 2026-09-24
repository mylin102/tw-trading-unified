"""The operator act that records broker truth into the execution context.

Live 2026-09-24: the system opened a real TMF calendar spread (TMFJ6 Sell 1 /
TMFK6 Buy 1) but the local model reset to FLAT, so the startup gate refused with
GUARD_POSITION_NOT_FLAT and the position had no managing path. The context can
only accept such a position through `canonical_holding`, which nothing wrote.
These tests pin the record/refuse contract.
"""
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.deployment.record_reconciled_holding import main  # noqa: E402


def _snapshot(*, positions=None, open_orders=None, captured_at=None,
              source="live_broker", scope="futopt", capture="OK",
              errors=None, breadcrumb=True):
    positions = positions if positions is not None else [
        {"account": "futures", "code": "TMFJ6", "quantity": 1,
         "direction": "Action.Sell", "avg_cost": 48176.0},
        {"account": "futures", "code": "TMFK6", "quantity": 1,
         "direction": "Action.Buy", "avg_cost": 48320.0},
    ]
    data = {
        "source": source, "scope": scope, "captured_at": captured_at or int(time.time() * 1000),
        "positions": positions, "open_orders": open_orders or [],
        "fetch_status": {"capture": capture}, "errors": errors or {},
        "canonical_input_hash": None,
    }
    if breadcrumb:
        data["canonical_input_hash"] = hashlib.sha256(json.dumps(
            data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return data


def _runtime(tmp_path, ctx=None):
    runtime = tmp_path / "runtime"
    (runtime / "exports/trades/live/diagnostics").mkdir(parents=True)
    ctx = ctx if ctx is not None else {
        "requested_mode": "live", "effective_mode": "live_quarantined",
        "live_order_allowed": False,
        "audit_reasons": ["POST_STARTUP_GATE_FAILED", "GUARD_POSITION_NOT_FLAT"],
        "session_id": "s" * 32, "revision": 65,
    }
    (runtime / "execution_context.json").write_text(json.dumps(ctx))
    return runtime


def _write_snapshot(runtime, **kw):
    path = runtime / "snapshot.json"
    path.write_text(json.dumps(_snapshot(**kw)))
    return path


def _args(runtime, snapshot, apply=False):
    argv = ["--runtime-dir", str(runtime), "--snapshot", str(snapshot)]
    if apply:
        argv.append("--apply")
    return argv


def test_dry_run_plans_and_writes_nothing(tmp_path, capsys):
    runtime = _runtime(tmp_path)
    before = (runtime / "execution_context.json").read_bytes()
    assert main(_args(runtime, _write_snapshot(runtime))) == 0
    assert "phase=PLAN" in capsys.readouterr().out
    assert (runtime / "execution_context.json").read_bytes() == before


def test_apply_records_the_legs_archives_and_keeps_the_mode(tmp_path, capsys):
    runtime = _runtime(tmp_path)
    snapshot = _write_snapshot(runtime)
    assert main(_args(runtime, snapshot, apply=True)) == 0
    ctx = json.loads((runtime / "execution_context.json").read_text())
    legs = ctx["canonical_holding"]["canonical_legs"]
    assert [leg["code"] for leg in legs] == ["TMFJ6", "TMFK6"]
    sides = {leg["code"]: leg["side"] for leg in legs}
    assert sides == {"TMFJ6": "SHORT", "TMFK6": "LONG"}
    assert ctx["effective_mode"] == "live_quarantined"      # never LIVE_READY
    assert ctx["live_order_allowed"] is False
    assert ctx["audit_reasons"] == ["POST_STARTUP_GATE_FAILED", "GUARD_POSITION_NOT_FLAT"]
    assert ctx["canonical_holding"]["captured_at"]
    archives = list((runtime / "logs" / "context_history").glob("ctx_*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text())["revision"] == 65
    assert "phase=RECORDED" in capsys.readouterr().out


@pytest.mark.parametrize("kw,code", [
    ({"captured_at": int((time.time() - 900) * 1000)}, "CANONICAL_STALE"),
    ({"source": "paper"}, "CANONICAL_SOURCE_INVALID"),
    ({"scope": "stock"}, "CANONICAL_SOURCE_INVALID"),
    ({"capture": "FAILED"}, "CANONICAL_QUERY_FAILED"),
    ({"errors": {"positions": "boom"}}, "CANONICAL_QUERY_FAILED"),
    ({"breadcrumb": False}, "CANONICAL_HASH_MISMATCH"),
    ({"positions": [{"account": "futures", "code": "TMFJ6", "quantity": 1,
                     "direction": "Action.Sell", "avg_cost": 48176.0}]},
     "CANONICAL_NOT_A_SPREAD"),
    ({"open_orders": [{"code": "TMFJ6", "status": "Submitted"}]},
     "CANONICAL_PENDING_ORDERS"),
    ({"positions": [{"account": "futures", "code": "TMFJ6", "quantity": 0,
                     "direction": "Action.Sell", "avg_cost": 48176.0},
                    {"account": "futures", "code": "TMFK6", "quantity": 1,
                     "direction": "Action.Buy", "avg_cost": 48320.0}]},
     "CANONICAL_LEG_IDENTITY_INVALID"),
    ({"positions": [{"account": "futures", "code": "TMFJ6", "quantity": 1,
                     "direction": "Action.Sell", "avg_cost": 48176.0},
                    {"account": "futures", "code": "TMFK6", "quantity": 1,
                     "direction": "Action.Sell", "avg_cost": 48320.0}]},
     "CANONICAL_LEGS_NOT_OPPOSED"),
])
def test_refusals_write_nothing(tmp_path, capsys, kw, code):
    runtime = _runtime(tmp_path)
    before = (runtime / "execution_context.json").read_bytes()
    assert main(_args(runtime, _write_snapshot(runtime, **kw), apply=True)) == 1
    assert f"code={code}" in capsys.readouterr().out
    assert (runtime / "execution_context.json").read_bytes() == before
    assert not (runtime / "logs").exists()


def test_refuses_when_not_a_live_intent_or_already_recorded(tmp_path, capsys):
    runtime = _runtime(tmp_path, ctx={"requested_mode": "paper",
                                      "effective_mode": "paper"})
    assert main(_args(runtime, _write_snapshot(runtime), apply=True)) == 1
    assert "CTX_NOT_LIVE_INTENT" in capsys.readouterr().out

    runtime2 = _runtime(tmp_path / "second")
    assert main(_args(runtime2, _write_snapshot(runtime2), apply=True)) == 0
    assert main(_args(runtime2, _write_snapshot(runtime2), apply=True)) == 1
    assert "ALREADY_RECORDED" in capsys.readouterr().out
