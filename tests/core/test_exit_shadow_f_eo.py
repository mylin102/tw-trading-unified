# Exactly-once + metrics tests for FShadowCollector.
import json
import os
from datetime import datetime, timedelta

import pytest

from core.exit_shadow_f import FShadowCollector

NOW = datetime.now().astimezone().isoformat(timespec="milliseconds")


def ago(sec):
    return (datetime.now().astimezone() - timedelta(seconds=sec)).isoformat(timespec="milliseconds")


def mk(tmp_path):
    return FShadowCollector(os.path.join(str(tmp_path), "shadow_f.jsonl"),
                            bbo_path=os.path.join(str(tmp_path), "bbo.jsonl"))


def pos(**kw):
    p = dict(trade_id="T1", position_generation="GEN-1", entry_order_ids=["O1", "O2"],
             near_contract="TMFH6", far_contract="TMFI6",
             near_side="SHORT", far_side="LONG", near_entry=42830.0, far_entry=42967.0,
             release_threshold_pts=88.0, atr=97.0, mark_source="LAST_TRADE",
             production_release_leg="FAR", production_release_ts="2026-08-03T17:11:45",
             point_value=10.0)
    p.update(kw)
    return p


def _prime(tmp_path):
    c = mk(tmp_path)
    c.on_quote("NEAR", 42910.0, 42920.0, receive_ts=ago(0))
    c.on_quote("FAR", 42860.0, 42870.0, receive_ts=ago(0))
    return c


def test_record_actual_exactly_once(tmp_path):
    c = _prime(tmp_path)
    r1 = c.record_actual(pos(), -980.0, 5.0, exit_type="RELEASE", settlement_id="STL-1")
    r2 = c.record_actual(pos(), -999.0, 9.0, exit_type="RELEASE", settlement_id="STL-1")
    assert r1 is not None and r1["event"] == "ACTUAL_PATH"
    assert r2 is None  # duplicate suppressed


def test_record_actual_different_settlement_id_suppressed(tmp_path):
    # One outcome per trade identity — settlement_id is saved metadata,
    # NOT part of the dedupe key. Repeat with different settlement id
    # (e.g. restart recovery with new callback ts) is suppressed.
    c = _prime(tmp_path)
    r1 = c.record_actual(pos(), -980.0, 5.0, exit_type="RELEASE", settlement_id="STL-1")
    r2 = c.record_actual(pos(), -930.0, 2.0, exit_type="COMBINED_EXIT", settlement_id="STL-2")
    assert r1 is not None
    assert r2 is None  # same trade identity — suppressed regardless of id


def test_partial_fill_not_final(tmp_path):
    # Partial fills never reach record_actual in production (hook fires only
    # at qty=0 canonical settlement). If a partial were passed, it records
    # once; any later call for the same trade identity is suppressed — the
    # final settlement must carry the SAME identity (trade/gen/order ids).
    c = _prime(tmp_path)
    r = c.record_actual(pos(), -100.0, 1.0, exit_type="PARTIAL", settlement_id="STL-PARTIAL")
    assert r is not None
    rf = c.record_actual(pos(), -980.0, 5.0, exit_type="RELEASE", settlement_id="STL-FINAL")
    assert rf is None  # same trade identity — single outcome


def test_priority_event_immediate_flush(tmp_path):
    c = _prime(tmp_path)
    c.evaluate(pos())  # EXECUTABLE_CANDIDATE -> immediate flush
    assert os.path.exists(os.path.join(str(tmp_path), "shadow_f.jsonl"))
    with open(os.path.join(str(tmp_path), "shadow_f.jsonl")) as f:
        content = f.read()
    assert "EXECUTABLE_CANDIDATE" in content  # persisted without explicit flush


def test_buffer_stats(tmp_path):
    c = _prime(tmp_path)
    c.evaluate(pos())
    st = c.buffer_stats()
    assert "buffer_depth" in st and "last_flush_ts" in st
    assert st["flush_errors"] == 0
    assert st["dropped_events"] == 0
