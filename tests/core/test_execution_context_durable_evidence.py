"""RED: broker-truth evidence in the context is durable.

Live 2026-09-24: the operator recorded the broker-held spread as
ctx.canonical_holding (the only way pre_deploy and the in-process startup gate
accept a non-flat account).  Seconds after the app started, its own ctx publish
nulled the field, the startup gate refused GUARD_POSITION_NOT_FLAT again, and the
position had no managing path.  A writer that does not mention canonical_holding
must not erase it.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.execution_context_state import persist_execution_context  # noqa: E402

LEGS = {"canonical_legs": [
    {"code": "TMFJ6", "side": "SHORT", "qty": 1, "avg_cost": 48176.0},
    {"code": "TMFK6", "side": "LONG", "qty": 1, "avg_cost": 48320.0},
], "source": "broker_reconciled_record", "captured_at": 1790253493941,
    "canonical_input_hash": "c" * 64}


def _base(**over):
    ctx = {"requested_mode": "live", "effective_mode": "live_quarantined",
           "live_order_allowed": False, "audit_reasons": ["REDEPLOY_BOOTSTRAP"]}
    ctx.update(over)
    return ctx


def _read(rt):
    return json.loads((Path(rt) / "execution_context.json").read_text())


def test_a_writer_that_omits_the_holding_does_not_erase_it(tmp_path):
    rt = str(tmp_path)
    persist_execution_context(_base(canonical_holding=LEGS), runtime_dir=rt)
    persist_execution_context(_base(audit_reasons=["LIVE_READY"]), runtime_dir=rt)
    assert _read(rt)["canonical_holding"] == LEGS


def test_an_explicit_holding_in_the_payload_wins(tmp_path):
    rt = str(tmp_path)
    persist_execution_context(_base(canonical_holding=LEGS), runtime_dir=rt)
    other = {"canonical_legs": [{"code": "TMFJ6", "side": "SHORT", "qty": 1,
                                 "avg_cost": 0.0}]}
    persist_execution_context(_base(canonical_holding=other), runtime_dir=rt)
    assert _read(rt)["canonical_holding"] == other


def test_first_write_still_records_a_plain_none(tmp_path):
    rt = str(tmp_path)
    persist_execution_context(_base(), runtime_dir=rt)
    assert _read(rt)["canonical_holding"] is None
