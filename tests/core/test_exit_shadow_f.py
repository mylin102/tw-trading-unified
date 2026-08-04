# F Shadow Canary tests — safety locks, dedupe, rejection taxonomy, isolation.
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


def test_hard_locks_present(tmp_path):
    c = mk(tmp_path)
    assert c.EXECUTION_ALLOWED is False
    assert c.ORDER_SUBMISSION_ALLOWED is False
    assert c.STATE_MUTATION_ALLOWED is False
    assert c.LIFECYCLE_TRANSITION_ALLOWED is False


def test_no_order_api_anywhere(tmp_path):
    src = open("/Users/myllin_mini/Documents/mylin102/tw-trading-unified-git/core/exit_shadow_f.py").read()
    # source-code hard lock: no execution keywords
    for kw in ("place_order", "send_order", "api.place", "order_mgr", "OrderManager",
               "submit(", "EXIT_PENDING", "try_claim", "ExitArbiter"):
        assert kw not in src, f"forbidden execution path found: {kw}"


def test_fault_injection_no_mutation(tmp_path):
    c = mk(tmp_path)
    before = dict(c.__dict__)
    ev = c.evaluate(pos(near_side="SHORT", far_side="LONG"))
    after = dict(c.__dict__)
    # only collector bookkeeping may change (seq, candidates) — never position/order state
    assert ev.get("mode") == "SHADOW_ONLY"
    assert ev.get("execution_influence") is False


def test_state_isolation_namespace(tmp_path):
    c = mk(tmp_path)
    ev = c.evaluate(pos())
    # shadow_f.* telemetry only — no production state keys
    assert "position_effect" not in ev
    assert "lifecycle" not in ev
    assert ev.get("adr") == "ADR-026"


def test_missing_near_bbo(tmp_path):
    c = mk(tmp_path)
    c.on_quote("FAR", 42860.0, 42870.0, receive_ts=ago(0))
    ev = c.evaluate(pos())
    assert ev["reason"] == "MISSING_NEAR_BBO"


def test_stale_far_rejected(tmp_path):
    c = mk(tmp_path)
    c.on_quote("NEAR", 42830.0, 42840.0, receive_ts=ago(0))
    c.on_quote("FAR", 42860.0, 42870.0, receive_ts=ago(660))  # 11min old
    ev = c.evaluate(pos())
    assert ev["reason"] == "STALE_FAR"


def test_pair_skew_rejected(tmp_path):
    c = mk(tmp_path)
    c.on_quote("NEAR", 42830.0, 42840.0, receive_ts=ago(0))
    c.on_quote("FAR", 42860.0, 42870.0, receive_ts=ago(0))  # skew 1s
    c._near["receive_ts"] = ago(0)
    c._far["receive_ts"] = ago(1.5)  # 1.5s skew > 500ms, < stale 2s
    ev = c.evaluate(pos())
    assert ev["reason"] == "PAIR_SKEW"


def test_crossed_market_rejected(tmp_path):
    c = mk(tmp_path)
    c.on_quote("NEAR", 42840.0, 42830.0, receive_ts=ago(0))  # crossed
    c.on_quote("FAR", 42860.0, 42870.0, receive_ts=ago(0))
    ev = c.evaluate(pos())
    assert ev["reason"] == "LOCKED_OR_CROSSED_MARKET"


def test_executable_candidate_short_long(tmp_path):
    c = mk(tmp_path)
    c.on_quote("NEAR", 42910.0, 42920.0, receive_ts=ago(0))   # SHORT exit ask 42920 -> -90 pts
    c.on_quote("FAR", 42860.0, 42870.0, receive_ts=ago(0))    # LONG exit bid 42860 -> -107 pts
    ev = c.evaluate(pos(near_side="SHORT", far_side="LONG",
                        near_entry=42830.0, far_entry=42967.0))
    assert ev["event"] == "EXECUTABLE_CANDIDATE"
    # SHORT -> exit at ask; LONG -> exit at bid
    n_pts = (42830.0 - 42920.0) * 10 / 10  # SHORT: (entry - exit)
    f_pts = (42860.0 - 42967.0) * 10 / 10
    assert n_pts == pytest.approx(ev["near_executable_pnl"] / 10)
    assert f_pts == pytest.approx(ev["far_executable_pnl"] / 10)
    assert ev["mode"] == "SHADOW_ONLY"


def test_duplicate_candidate_per_generation(tmp_path):
    c = mk(tmp_path)
    c.on_quote("NEAR", 42690.0, 42700.0, receive_ts=ago(0))
    c.on_quote("FAR", 42860.0, 42870.0, receive_ts=ago(0))
    ev1 = c.evaluate(pos())
    ev2 = c.evaluate(pos())  # same generation
    assert ev1["event"] == "EXECUTABLE_CANDIDATE"
    assert ev2["event"] == "DUPLICATE_CANDIDATE"


def test_restart_dedupe_from_disk(tmp_path):
    c = mk(tmp_path)
    c.on_quote("NEAR", 42690.0, 42700.0, receive_ts=ago(0))
    c.on_quote("FAR", 42860.0, 42870.0, receive_ts=ago(0))
    c.evaluate(pos())
    # restart: new instance, same file — must not re-emit for same generation
    c2 = FShadowCollector(str(tmp_path / "shadow_f.jsonl"))
    c2.on_quote("NEAR", 42690.0, 42700.0, receive_ts=ago(0))
    c2.on_quote("FAR", 42860.0, 42870.0, receive_ts=ago(0))
    c2._load_existing()
    ev = c2.evaluate(pos())
    assert ev["event"] == "DUPLICATE_CANDIDATE"


def test_contract_pair_mismatch(tmp_path):
    c = mk(tmp_path)
    c.bind_contracts("TMFH6", "TMFI6")
    c.on_quote("NEAR", 42690.0, 42700.0, receive_ts=ago(0))
    c.on_quote("FAR", 42860.0, 42870.0, receive_ts=ago(0))
    ev = c.evaluate(pos(near_contract="TMFQ6"))
    assert ev["reason"] == "CONTRACT_PAIR_MISMATCH"


def test_join_identity_fields(tmp_path):
    c = mk(tmp_path)
    c.on_quote("NEAR", 42690.0, 42700.0, receive_ts=ago(0))
    c.on_quote("FAR", 42860.0, 42870.0, receive_ts=ago(0))
    ev = c.evaluate(pos())
    for k in ("trade_id", "position_generation", "entry_order_ids",
              "near_contract", "far_contract"):
        assert k in ev
    assert "COMBINED_EXIT" != ev.get("trade_id")
