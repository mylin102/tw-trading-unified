# Test: restart-recovery exactly-once with DIFFERENT callback ts — still one outcome.
import os
from datetime import datetime, timedelta

from core.exit_shadow_f import FShadowCollector


def ago(sec):
    return (datetime.now().astimezone() - timedelta(seconds=sec)).isoformat(timespec="milliseconds")


def pos(**kw):
    p = dict(trade_id="T1", position_generation="GEN-1", entry_order_ids=["O1", "O2"],
             near_side="SHORT", far_side="LONG", near_entry=42830.0, far_entry=42967.0,
             release_threshold_pts=88.0, atr=97.0, mark_source="LAST_TRADE",
             point_value=10.0)
    p.update(kw)
    return p


def test_restart_recovery_exactly_once_different_ts(tmp_path):
    out = os.path.join(str(tmp_path), "shadow_f.jsonl")
    c1 = FShadowCollector(out)
    c1.on_quote("NEAR", 42910.0, 42920.0, receive_ts=ago(0))
    c1.on_quote("FAR", 42860.0, 42870.0, receive_ts=ago(0))
    r1 = c1.record_actual(pos(), -980.0, 5.0, exit_type="RELEASE", settlement_id="STL-A")
    assert r1 is not None
    c1.flush()
    # simulated restart: new instance, DIFFERENT callback ts/settlement_id
    c2 = FShadowCollector(out)
    c2._load_existing()
    r2 = c2.record_actual(pos(), -999.0, 9.0, exit_type="RELEASE", settlement_id="STL-B")
    assert r2 is None  # same stable identity (trade_id+gen+order_ids) — suppressed
    with open(out) as f:
        n_actual = sum(1 for l in f if '"ACTUAL_PATH"' in l)
    assert n_actual == 1  # exactly one outcome persisted
