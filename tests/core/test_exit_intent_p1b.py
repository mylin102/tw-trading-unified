"""P1-B RED tests B1-B31 — durable exit-intent protocol (codex-approved TDD).

FINAL contract (codex round-3 review): no existence checks, no source-text
search, no self-reported parity helpers. B1 asserts call-time durable
snapshots; B19/B23 exercise the REAL combined-exit dispatcher / producer
(_submit_mts_order_signal) with temp runtime artifacts (chdir + env
isolation). B21 races two independently constructed IntentLog instances.
Lock owner_check returns explicit {"alive": bool}.
"""
import json
import os
import threading
import time

import pytest

import core.exit_intent as ei  # noqa: E402,F401  (ImportError = RED)


# ── helpers ──────────────────────────────────────────────────────────────
class StubOrderMgr:
    """Records every submit/cancel/query call for side-effect assertions.

    Polymorphic submit: pure-core tests call submit(client_order_id, leg);
    the real dispatcher calls submit(order) with an order object."""
    from types import SimpleNamespace as _NS

    def __init__(self):
        self.submits = []
        self.cancels = []
        self.queries = []

    def create_order(self, **kw):
        from unittest.mock import MagicMock
        o = MagicMock()
        o.order_id = f"ORD-{len(self.submits) + 1}"
        o.client_order_id = kw.get("client_order_id")
        o.intent_id = kw.get("intent_id")
        o.leg = kw.get("leg")
        return o

    def submit(self, arg, leg=None, **kw):
        if hasattr(arg, "order_id"):  # dispatcher path: submit(order)
            self.submits.append({"client_order_id": getattr(arg, "client_order_id", None),
                                 "leg": leg or getattr(arg, "leg", None),
                                 "order_id": arg.order_id})
            return arg
        self.submits.append({"client_order_id": arg, "leg": leg})
        return {"order_id": f"ORD-{arg}"}

    def cancel(self, client_order_id):
        self.cancels.append(client_order_id)

    def query(self, client_order_id):
        self.queries.append(client_order_id)
        return {"status": "NOT_FOUND"}

    def has_pending_exit(self):
        return False


def make_log(tmp_path):
    return ei.IntentLog(str(tmp_path))


def make_intent(log, trade_id="t1", reason="COMBINED_EXIT"):
    return log.create(trade_id, reason)


def build_monitor(tmp_path, monkeypatch):
    """Real FuturesMonitor + released-position TMFSpread (existing-test pattern).

    Uses an ABSOLUTE repo config path so chdir(tmp_path) cannot break the
    config load (codex #6: B19/B23 must fail on integration gap, not config).
    TMFSpread subclass returns None for unset strategy internals so the real
    producer branch runs on minimal state (the exit decision itself is real).
    """
    from unittest.mock import MagicMock
    from datetime import datetime
    from strategies.futures.monitor import FuturesMonitor
    from strategies.plugins.futures.active.tmf_spread import (
        TMFSpread, PositionPhase, PositionLifecycle, ReleaseGroup,
        ReleaseGroupStatus, TrailGroup, TrailGroupStatus, Leg,
    )

    class _LenientTMFSpread(TMFSpread):
        def __getattr__(self, name):
            return None

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    config_path = os.path.join(repo_root, "config", "futures_night.yaml")
    api = MagicMock()
    api.Contracts.Futures.TMF = [MagicMock(code="TMFF6", delivery_date="2026-06-17")]
    monitor = FuturesMonitor(api, config_path, dry_run=True)
    monitor.ticker = "TMF"
    monitor._use_order_manager = True
    monitor.order_mgr = StubOrderMgr()
    monitor.contract = MagicMock(code="TMFF6")
    monitor.far_contract = MagicMock(code="TMFH6")
    strat = _LenientTMFSpread()
    strat._restore_position_state = MagicMock(return_value=False)
    strat._has_position = True
    strat._released_leg = "near"
    strat._near_side = "SHORT"
    strat._far_side = "LONG"
    strat._side = "LONG"
    strat._near_entry = 43800.0
    strat._far_entry = 44000.0
    strat._peak = 44100.0
    strat._ticker = "TMF"
    strat._trade_id = "t1"
    strat._lifecycle = "MANAGING"  # needed by _manage_position exit branch
    strat._single_leg_post_fill_ticks = 0  # warmup counter for single-leg path
    strat._pending_exit = False
    strat._near_status = "FILLED"
    strat._far_status = "FILLED"
    strat._trail_anchor_status = None
    strat._atr_mult_stop = 3.5  # trail params accessed by the exit branch
    strat._atr_mult_trail = 3.5
    strat._release_stop_fixed = None
    strat._trail_dist_fixed = None
    strat._near_max = 44100.0
    strat._near_min = 43800.0
    strat._far_max = 44020.0
    strat._far_min = 43365.0
    strat._release_price = 43434.0  # NEAR released at this price
    strat._mfe_pts = 0.0
    strat._mae_pts = 0.0
    strat._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.COMPLETED,
                                   filled_leg=Leg.FAR, canceled_leg=Leg.NEAR),
        trail_group=TrailGroup(status=TrailGroupStatus.ARMED),
    )
    return monitor, strat, api


# ── B1: call-time durable snapshot ordering; both ids persisted pre-submit
# The hook is invoked between the durable SUBMIT_ATTEMPTED write and the
# actual manager I/O: it asserts the durable snapshot, then the dispatch
# still performs the real manager submits (hook cannot be a pre-submit no-op).
def test_b1_durable_snapshot_ordering_before_submit(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    snapshots = {}
    seen_submits = 0

    def submit_hook(leg, client_order_id, intent_id):
        # durable snapshot AT CALL TIME (in-memory events are irrelevant)
        snap = log.get(intent_id)
        snapshots[leg] = (snap, log.raw_lines())
        # manager must NOT have been called for this leg yet (hook runs
        # before the actual submit I/O)
        assert seen_submits == len([s for s in mgr.submits if s["leg"] == leg])

    result = ei.dispatch_combined_exit(log, "t1", mgr, submit_hook=submit_hook)
    iid = result["intent_id"]
    near_intent, near_raw = snapshots["NEAR"]
    far_intent, far_raw = snapshots["FAR"]
    # before NEAR submit: BOTH ids persisted, NEAR=SUBMIT_ATTEMPTED, FAR untouched
    assert near_intent["legs"]["NEAR"]["client_order_id"] is not None
    assert near_intent["legs"]["FAR"]["client_order_id"] is not None
    assert near_intent["legs"]["NEAR"]["status"] == "SUBMIT_ATTEMPTED"
    assert near_intent["legs"]["FAR"]["status"] == "NOT_SUBMITTED"
    # intent record + NEAR attempt already durable in the JSONL
    joined_near = "".join(near_raw)
    assert '"trade_id": "t1"' in joined_near
    assert '"NEAR"' in joined_near and "SUBMIT_ATTEMPTED" in joined_near
    assert '"FAR"' in joined_near and "NOT_SUBMITTED" in joined_near
    # immediately before FAR submit: FAR=SUBMIT_ATTEMPTED durable
    assert far_intent["legs"]["FAR"]["status"] == "SUBMIT_ATTEMPTED"
    assert "SUBMIT_ATTEMPTED" in "".join(far_raw)
    assert len(far_raw) > len(near_raw)
    # the hook observed BEFORE the real submits; dispatch then performed them
    assert len(mgr.submits) == 2
    assert mgr.submits[0]["client_order_id"] == near_intent["legs"]["NEAR"]["client_order_id"]


# ── B2: intent write failure ⇒ fail-closed zero submits ──────────────────
def test_b2_intent_write_failure_fail_closed(tmp_path, monkeypatch):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    def boom(*a, **k):
        raise OSError("fsync failed")
    monkeypatch.setattr(ei.os, "fsync", boom)
    with pytest.raises(OSError):
        make_intent(log)
    assert mgr.submits == []


# ── B3: crash after intent, before any SUBMIT_ATTEMPTED ⇒ safe cancel ────
def test_b3_pending_no_attempt_safe_cancel(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    outcome = log.recover(iid, query_fn=lambda cid: {"status": "UNAVAILABLE"})
    assert outcome["legs"]["NEAR"]["status"] == "NOT_SUBMITTED"
    assert outcome["legs"]["FAR"]["status"] == "NOT_SUBMITTED"
    assert outcome["terminal"] == "CANCELED_SAFE"


# ── B4: crash after SUBMIT_ATTEMPTED before send ⇒ query, never resubmit ─
def test_b4_submit_attempted_recovery_queries_never_resubmits(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    cid = ei.client_order_id("t1", "NEAR")
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id=cid)
    log.recover(iid, query_fn=mgr.query, order_mgr=mgr)
    assert cid in mgr.queries
    assert mgr.submits == []


# ── B5: broker accepted but call did not return ⇒ UNKNOWN fail-closed ────
def test_b5_broker_accepted_no_return_unknown_fail_closed(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    def ambiguous(cid, leg, **kw):
        raise TimeoutError("network cut")
    with pytest.raises(TimeoutError):
        log.submit_leg(iid, "NEAR", mgr, submit_fn=ambiguous)
    assert log.get(iid)["legs"]["NEAR"]["status"] == "UNKNOWN"
    log.recover(iid, query_fn=lambda c: {"status": "UNAVAILABLE"})
    assert mgr.submits == []


# ── B6: recovery query unavailable ⇒ blocked, no infer ───────────────────
def test_b6_recovery_query_unavailable_blocks(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    cid = ei.client_order_id("t1", "NEAR")
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id=cid)
    outcome = log.recover(iid, query_fn=lambda c: {"status": "UNAVAILABLE"})
    assert outcome["legs"]["NEAR"]["status"] == "SUBMIT_ATTEMPTED"
    assert outcome["blocked"] is True


# ── B7: repeated restart ⇒ idempotent recovery (each restart queries
#      until resolution; never resubmits) ───────────────────────────────
def test_b7_repeated_restart_idempotent(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    cid = ei.client_order_id("t1", "FAR")
    log.transition(iid, "FAR", "SUBMIT_ATTEMPTED", client_order_id=cid)
    # three restarts: two while broker query is unavailable, then resolved
    states = iter([{"status": "UNAVAILABLE"}, {"status": "UNAVAILABLE"},
                   {"status": "NOT_FOUND"}])

    def qf(qid):
        mgr.queries.append(qid)
        return next(states)

    for _ in range(3):
        log.recover(iid, query_fn=qf, order_mgr=mgr)
    assert mgr.queries.count(cid) == 3  # every restart queried before resolution
    assert mgr.submits == []  # no duplicate actions across restarts
    assert log.get(iid)["legs"]["FAR"]["status"] == "NOT_FOUND_CONFIRMED"


# ── B8: crash after near attempted; far never attempted ⇒ reachable edge ─
def test_b8_near_attempted_far_not_attempted(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    cid = ei.client_order_id("t1", "NEAR")
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id=cid)
    outcome = log.recover(iid, query_fn=lambda c: {"status": "NOT_FOUND"})
    assert outcome["legs"]["NEAR"]["status"] == "NOT_FOUND_CONFIRMED"
    assert outcome["legs"]["FAR"]["status"] == "NOT_SUBMITTED"


# ── B9: crash after both SUBMITTED before state write ⇒ converge ─────────
def test_b9_both_submitted_before_state_write_converges(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    for leg in ("NEAR", "FAR"):
        log.transition(iid, leg, "SUBMIT_ATTEMPTED", client_order_id=f"cid-{leg}")
        log.transition(iid, leg, "SUBMITTED", broker_order_id=f"ORD-{leg}")
    outcome = log.recover(iid, query_fn=lambda c: {"status": "FILLED"})
    assert outcome["terminal"] == "COMPLETED"
    assert log.has_inflight_exit_intent("t1") is False


# ── B10: partial fill ⇒ repair child; child id persisted BEFORE repair I/O
#         (repair_complete creates NOT_SUBMITTED child; submit_leg performs
#         the durable SUBMIT_ATTEMPTED before the repair order) ───────────
def test_b10_partial_fill_during_recovery_repair_child(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="cid-near")
    log.transition(iid, "NEAR", "FILLED")
    log.transition(iid, "FAR", "SUBMIT_ATTEMPTED", client_order_id="cid-far")
    child = log.repair_complete(iid, "FAR", reason="PARTIAL_FILL")
    child_id = child["intent_id"]
    assert child["parent"] == iid
    cid = log.get(child_id)["legs"]["FAR"]["client_order_id"]
    assert log.get(child_id)["legs"]["FAR"]["status"] == "NOT_SUBMITTED"
    assert cid != "cid-far"  # NEW child id, persisted pre-I/O
    # canonical submit: durable SUBMIT_ATTEMPTED before the repair order
    mgr = StubOrderMgr()
    seen = {}

    def hook(leg, cid_, iid_):
        seen["status_at_hook"] = log.get(iid_)["legs"]["FAR"]["status"]
        seen["id_at_hook"] = cid_

    log.submit_leg(child_id, "FAR", mgr, submit_hook=hook)
    assert seen["status_at_hook"] == "SUBMIT_ATTEMPTED"  # durable BEFORE I/O
    assert seen["id_at_hook"] == cid


# ── B11: idempotency key duplicate ⇒ rejected, ONE fill ──────────────────
def test_b11_idempotency_key_dedup(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    log.submit_leg(iid, "NEAR", mgr)
    with pytest.raises(ei.DuplicateSubmitError):
        log.submit_leg(iid, "NEAR", mgr)
    assert len(mgr.submits) == 1


# ── B12: repair complete_exit uses the PERSISTED child id (submit_leg does
#         the durable attempt before I/O) ────────────────────────────────
def test_b12_near_ok_far_rejected_repair_complete(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c-near")
    log.transition(iid, "NEAR", "FILLED")
    log.transition(iid, "FAR", "SUBMIT_ATTEMPTED", client_order_id="c-far")
    log.transition(iid, "FAR", "REJECTED")
    child = log.repair_complete(iid, "FAR", reason="REJECTED")
    child_id = child["intent_id"]
    persisted_cid = log.get(child_id)["legs"]["FAR"]["client_order_id"]
    assert log.get(child_id)["legs"]["FAR"]["status"] == "NOT_SUBMITTED"
    log.submit_leg(child_id, "FAR", mgr)
    assert len(mgr.submits) == 1
    assert mgr.submits[0]["client_order_id"] == persisted_cid  # persisted id used
    assert persisted_cid != "cid-far"


# ── B13: far CANCELLED after near FILLED ⇒ repair child, no orphan ───────
def test_b13_far_cancelled_repair_no_orphan(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c-near")
    log.transition(iid, "NEAR", "FILLED")
    log.transition(iid, "FAR", "SUBMIT_ATTEMPTED", client_order_id="c-far")
    log.transition(iid, "FAR", "CANCELLED")
    child = log.repair_complete(iid, "FAR", reason="CANCELLED")
    log.submit_leg(child["intent_id"], "FAR", mgr)
    assert len(mgr.submits) == 1
    assert len(mgr.cancels) == 0


# ── B14: restart with memory cleared ⇒ intent converges without memory ───
def test_b14_memory_cleared_converges(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    cid = ei.client_order_id("t1", "NEAR")
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id=cid)
    log2 = ei.IntentLog(str(tmp_path))
    outcome = log2.recover(iid, query_fn=lambda c: {"status": "NOT_FOUND"})
    assert outcome["legs"]["NEAR"]["status"] == "NOT_FOUND_CONFIRMED"


# ── B15: pre-gate suppresses entry + second exit + session exit ──────────
def test_b15_pregate_suppresses_duplicates(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c")
    assert log.has_inflight_exit_intent("t1") is True
    assert log.entry_allowed("t1") is False
    assert log.exit_trigger_allowed("t1") is False
    assert log.session_transition_allowed("t1") is False
    assert log.recovery_path_allowed("t1") is True
    assert log.emergency_path_allowed("t1") is True


# ── B16: UNKNOWN blocks entry + ordinary exit; emergency still works ─────
def test_b16_unknown_blocks_but_emergency_works(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c")
    log.transition(iid, "NEAR", "UNKNOWN")
    assert log.entry_allowed("t1") is False
    assert log.exit_trigger_allowed("t1") is False
    assert log.emergency_path_allowed("t1") is True


# ── B17: state-write failure after submits ⇒ intent authority ────────────
def test_b17_state_write_failure_intent_authority(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    for leg in ("NEAR", "FAR"):
        log.transition(iid, leg, "SUBMIT_ATTEMPTED", client_order_id=f"c-{leg}")
        log.transition(iid, leg, "SUBMITTED", broker_order_id=f"O-{leg}")
    assert log.get(iid)["legs"]["NEAR"]["status"] == "SUBMITTED"
    assert log.has_inflight_exit_intent("t1") is True


# ── B18: compaction only after terminal durable; retention enforced ──────
def test_b18_compaction_gated_by_durable_terminal(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    with pytest.raises(ei.IntentNotTerminalError):
        log.archive(iid)
    for leg in ("NEAR", "FAR"):
        log.transition(iid, leg, "SUBMIT_ATTEMPTED", client_order_id=f"c-{leg}")
        log.transition(iid, leg, "FILLED")
    log.mark_terminal(iid, "COMPLETED")
    log.archive(iid)
    assert iid in log.archive_index()
    assert log.get(iid)["retention_expires_at"] is not None


# ── B19: REAL combined-exit dispatcher writes REAL artifacts ─────────────
def test_b19_real_combined_exit_artifacts(tmp_path, monkeypatch):
    from datetime import datetime
    from core.signal import Signal
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MTS_EVENT_LOG_PATH", str(tmp_path / "mts_spread_events.jsonl"))
    monkeypatch.setenv("MTS_FILL_LOG_PATH", str(tmp_path / "mts_trade_fills.jsonl"))
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monitor, strat, api = build_monitor(tmp_path, monkeypatch)
    bar = {"near_close": 44100.0, "far_close": 44020.0, "atr": 10.0,
           "timestamp": datetime.now(), "code": "TMFF6"}
    from unittest.mock import patch
    from pathlib import Path
    state_path = Path(tmp_path) / "state.json"
    with patch("strategies.futures.monitor._mts_position_state_path",
               return_value=state_path), \
         patch("strategies.futures.monitor.is_taifex_futures_market_open",
               return_value=True), \
         patch.object(Path, "exists", return_value=False):
        sig = Signal("COMBINED_EXIT", "TMF_COMBINED_EXIT")
        monitor._submit_mts_order_signal(sig, strat, bar, datetime.now())
    # DISCRIMINATING ASSERTION FIRST (Phase-2 integration gap): the
    # dispatcher must create ONE durable intent at the production location
    intent_log = ei.IntentLog(str(tmp_path / "logs"))
    actives = [iid for iid in intent_log.list_active()
               if intent_log.get(iid)["trade_id"] == "t1"]
    assert len(actives) == 1  # RED until Phase 2 wires the dispatcher
    intent = intent_log.get(actives[0])
    for leg in ("NEAR", "FAR"):
        assert intent["legs"][leg]["client_order_id"] is not None
        assert intent["legs"][leg]["status"] in ("SUBMIT_ATTEMPTED", "SUBMITTED", "FILLED")
    assert intent_log.has_inflight_exit_intent("t1") is True
    # REAL artifacts (not synthetic): fills ledger rows
    fills = [json.loads(l) for l in
             (tmp_path / "mts_trade_fills.jsonl").read_text().splitlines() if l.strip()]
    assert len(fills) >= 2
    assert all(f.get("trade_id") == "t1" for f in fills)
    # events ledger: two ORDER_SUBMITTED rows
    events = [json.loads(l) for l in
              (tmp_path / "mts_spread_events.jsonl").read_text().splitlines() if l.strip()]
    assert sum(1 for e in events if e.get("event") == "ORDER_SUBMITTED") >= 2
    # orders JSON: two orders persisted
    orders_files = list((tmp_path / "exports" / "trades").glob("TMF_*_orders.json"))
    assert orders_files
    orders = json.loads(orders_files[0].read_text())
    order_list = orders if isinstance(orders, list) else orders.get("orders", [])
    assert len(order_list) >= 2
    # state file: COMBINED_EXIT action written
    assert (tmp_path / "state.json").exists()
    assert "COMBINED_EXIT" in (tmp_path / "state.json").read_text()
    assert len(monitor.order_mgr.submits) == 2


# ── B20: capacity fail-closed — never silently prunes ACTIVE intents ─────
def test_b20_capacity_fail_closed(tmp_path):
    log = make_log(tmp_path)
    for i in range(ei.MAX_ACTIVE_INTENTS):
        make_intent(log, trade_id=f"t{i}")
    with pytest.raises(ei.IntentCapacityError):
        make_intent(log, trade_id="overflow")
    assert len(log.list_active()) == ei.MAX_ACTIVE_INTENTS


# ── B21: two INDEPENDENT IntentLog instances, public recover WITHOUT
#         external locks ⇒ exactly ONE durable action (serialization is
#         INTERNAL to recovery, not caller discipline) ───────────────────
def test_b21_two_instances_concurrent_single_action(tmp_path):
    log_a = ei.IntentLog(str(tmp_path))
    log_b = ei.IntentLog(str(tmp_path))  # second process instance, same runtime dir
    iid = log_a.create("t1", "COMBINED_EXIT")
    cid = ei.client_order_id("t1", "NEAR")
    log_a.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id=cid)
    barrier = threading.Barrier(3)
    outcomes = []

    def slow_query(c):
        time.sleep(0.15)  # hold the lock long enough for the loser to collide
        return {"status": "NOT_FOUND"}

    def actor(name, log_inst):
        barrier.wait()
        # NO external lock — recovery must serialize internally
        try:
            r = log_inst.recover(iid, query_fn=slow_query)
            outcomes.append((name, r["legs"]["NEAR"]["status"]))
        except ei.LockBusyError:
            outcomes.append((name, "LOCK_BUSY"))

    t1 = threading.Thread(target=actor, args=("a", log_a))
    t2 = threading.Thread(target=actor, args=("b", log_b))
    t1.start(); t2.start()
    barrier.wait()
    t1.join(); t2.join()
    transitions = [o for o in outcomes if o[1] == "NOT_FOUND_CONFIRMED"]
    others = [o for o in outcomes if o[1] != "NOT_FOUND_CONFIRMED"]
    assert len(transitions) == 1  # exactly ONE actor performed the transition
    assert all(o[1] in ("LOCK_BUSY", "NOT_FOUND_CONFIRMED", "SUBMIT_ATTEMPTED") for o in others)


# ── B22: foreign owner → owner-verified reclaim → second process acquires ─
def test_b22_restart_overlap_owner_verified(tmp_path):
    log = make_log(tmp_path)
    log._force_lock_owner({"pid": 99999, "start_token": "tokA", "host": "h",
                           "acquired_at": time.time()})
    # owner_check: OLD process dead (alive=False) ⇒ reclaim + acquire
    acquired = log.try_acquire({"pid": os.getpid(), "start_token": "tokB", "host": "h"},
                               owner_check_fn=lambda pid, token: {"alive": False})
    assert acquired is True
    assert log.lock_owner()["pid"] == os.getpid()
    # healthy foreign owner (alive=True) ⇒ acquisition refused
    log._force_lock_owner({"pid": 77777, "start_token": "alive", "host": "h"})
    with pytest.raises(ei.LockBusyError):
        log.try_acquire({"pid": os.getpid(), "start_token": "tokC", "host": "h"},
                        owner_check_fn=lambda pid, token: {"alive": True})


# ── B23: REAL tmf_spread remaining-leg producer — same qualifying state
#         evaluated twice ⇒ ONE durable intent + ONE dispatch/submit ─────
def test_b23_real_producer_double_eval_one_submit(tmp_path, monkeypatch):
    from datetime import datetime
    from pathlib import Path
    from unittest.mock import patch
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monitor, strat, api = build_monitor(tmp_path, monkeypatch)
    # REAL strategy init via a minimal real StrategyContext — initializes the
    # risk engines + params through the actual init() path (not stubs)
    from core.strategy_context import StrategyContext, MarketData, PositionView
    ctx = StrategyContext(
        market=MarketData(last_bar={}, ticker="TMF"),
        position=PositionView(),
        config={"ticker": "TMF",
                "params": {"atr_multiplier_stop": 3.5, "atr_multiplier_trail": 3.5,
                           "trail_distance_points": 35.0, "min_atr": 5.0,
                           "entry_z": -2.0, "confirm_ticks": 1, "confirm_ms": 0.0}},
    )
    strat.init(ctx)
    now = datetime.now()
    bar = {"near_close": 44100.0, "far_close": 44020.0, "atr": 10.0,
           "timestamp": now, "code": "TMFF6"}
    with patch("strategies.futures.monitor._mts_position_state_path",
               return_value=Path(tmp_path) / "state.json"), \
         patch("strategies.futures.monitor.is_taifex_futures_market_open", return_value=True), \
         patch.object(Path, "exists", return_value=False):
        # producer tick 1: the REAL remaining-leg trail branch emits a signal
        sig1 = strat._manage_position(44100.0, 44020.0, -0.5, now, bar)
        # producer must have created ONE durable intent for t1 BEFORE emitting
        intent_log = ei.IntentLog(str(tmp_path / "logs"))
        actives = [iid for iid in intent_log.list_active()
                   if intent_log.get(iid)["trade_id"] == "t1"]
        assert len(actives) == 1
        # producer tick 2: same qualifying state — NO second emission
        sig2 = strat._manage_position(44100.0, 44020.0, -0.5, now, bar)
        assert sig2 is None  # suppressed by the durable intent
        # the single emitted signal dispatches exactly once
        if sig1 is not None:
            monitor._submit_mts_order_signal(sig1, strat, bar, now)
    assert len(monitor.order_mgr.submits) == 1  # P1-2 regression: ONE order


# ── B24: crash after repair-child SUBMIT_ATTEMPTED before send ───────────
def test_b24_repair_child_crash_before_send(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    child = log.repair_complete(iid, "FAR", reason="REJECTED")
    cid = log.get(child["intent_id"])["legs"]["FAR"]["client_order_id"]
    # crash state: child durably SUBMIT_ATTEMPTED, send never happened
    log.transition(child["intent_id"], "FAR", "SUBMIT_ATTEMPTED",
                   client_order_id=cid)
    log2 = ei.IntentLog(str(tmp_path))
    outcome = log2.recover(child["intent_id"], query_fn=lambda c: {"status": "NOT_FOUND"})
    assert outcome["legs"]["FAR"]["status"] == "NOT_FOUND_CONFIRMED"
    assert mgr.submits == []


# ── B25: broker accepted repair but call did not return ⇒ child UNKNOWN ──
def test_b25_repair_broker_accepted_no_return(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    child = log.repair_complete(iid, "FAR", reason="REJECTED")
    def ambiguous(cid, leg, **kw):
        raise TimeoutError("network cut")
    with pytest.raises(TimeoutError):
        log.submit_leg(child["intent_id"], "FAR", mgr, submit_fn=ambiguous)
    assert log.get(child["intent_id"])["legs"]["FAR"]["status"] == "UNKNOWN"
    log.recover(child["intent_id"], query_fn=lambda c: {"status": "UNAVAILABLE"})
    assert mgr.submits == []


# ── B26: emergency supersedes active intent; reconciliation sees both ────
def test_b26_emergency_supersedes_intent(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c")
    emg = log.emergency_supersede(iid, mgr)
    assert emg["supersedes"] == iid
    assert log.get(iid)["terminal"] == "SUPERSEDED_BY_EMERGENCY"
    rec = log.reconciliation_view(iid)
    assert rec["emergency"] is not None and rec["intent"] is not None
    with pytest.raises(ei.SupersededIntentError):
        log.repair_complete(iid, "FAR", reason="LATE")


# ── B27: NOT_FOUND confirmed vs unavailable; no silent timeout-resubmit ──
def test_b27_not_found_vs_unavailable(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c")
    r1 = log.recover(iid, query_fn=lambda c: {"status": "NOT_FOUND"})
    assert r1["legs"]["NEAR"]["status"] == "NOT_FOUND_CONFIRMED"
    iid2 = make_intent(log, trade_id="t2")
    log.transition(iid2, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c2")
    r2 = log.recover(iid2, query_fn=lambda c: {"status": "UNAVAILABLE"})
    assert r2["legs"]["NEAR"]["status"] == "SUBMIT_ATTEMPTED"
    assert r2["blocked"] is True


# ── B28: intent_version CAS stale rejection ──────────────────────────────
def test_b28_cas_stale_transition_rejected(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    v1 = log.get(iid)["version"]
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c", expect_version=v1)
    with pytest.raises(ei.StaleVersionError):
        log.transition(iid, "FAR", "SUBMIT_ATTEMPTED", expect_version=v1)


# ── B29: healthy owner with slow I/O is NOT stolen (age only alerts) ─────
def test_b29_healthy_owner_not_stolen(tmp_path):
    log = make_log(tmp_path)
    acquired_at = time.time() - 9999  # older than ANY age threshold
    log._force_lock_owner({"pid": os.getpid(), "start_token": "mine", "host": "h",
                           "acquired_at": acquired_at})
    with pytest.raises(ei.LockBusyError):  # refusal despite age
        log.try_acquire({"pid": 8888, "start_token": "other", "host": "h"},
                        owner_check_fn=lambda pid, token: {"alive": True},  # owner alive
                        age_alert_threshold_s=1)
    assert log.lock_owner()["pid"] == os.getpid()  # not stolen


# ── B30: PID reuse — live PID with start-token MISMATCH reclaimable;
#         live PID with MATCHING token refuses (owner_check returns the
#         CURRENT start token) ────────────────────────────────────────────
def test_b30_pid_reuse_start_token_mismatch_reclaimable(tmp_path):
    log = make_log(tmp_path)
    log._force_lock_owner({"pid": 4242, "start_token": "old-boot-token", "host": "h"})
    # PID 4242 is ALIVE (alive=true) but its current token differs from the
    # recorded one ⇒ the recorded owner is an old incarnation ⇒ reclaimable
    acquired = log.try_acquire({"pid": 9999, "start_token": "new-token", "host": "h"},
                               owner_check_fn=lambda pid, token: {"alive": True,
                                                                  "start_token": "new-token"})
    assert acquired is True
    assert log.lock_owner()["start_token"] == "new-token"
    # live PID with MATCHING token ⇒ owner is genuinely alive ⇒ refuse
    log._force_lock_owner({"pid": 5555, "start_token": "same", "host": "h"})
    with pytest.raises(ei.LockBusyError):
        log.try_acquire({"pid": 9998, "start_token": "x", "host": "h"},
                        owner_check_fn=lambda pid, token: {"alive": True,
                                                           "start_token": "same"})


# ── B31: emergency supersede write failure ⇒ fail-closed no orders ───────
def test_b31_emergency_supersede_write_failure_fail_closed(tmp_path, monkeypatch):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    def boom(*a, **k):
        raise OSError("supersede fsync failed")
    monkeypatch.setattr(ei.os, "fsync", boom)
    with pytest.raises(OSError):
        log.emergency_supersede(iid, mgr)
    assert mgr.submits == [] and mgr.cancels == []


# ── B32: submit_leg from SUBMIT_ATTEMPTED is REJECTED (no re-send without
#         an authoritative query — codex #1) ─────────────────────────────
def test_b32_submit_leg_rejects_already_attempted(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="cid")
    with pytest.raises(ei.DuplicateSubmitError):
        log.submit_leg(iid, "NEAR", mgr)  # attempted ⇒ recovery must query
    assert mgr.submits == []
    # SUBMITTED/UNKNOWN also rejected
    log.transition(iid, "NEAR", "UNKNOWN")
    with pytest.raises(ei.DuplicateSubmitError):
        log.submit_leg(iid, "NEAR", mgr)
    assert mgr.submits == []


# ── B33: illegal state transitions are rejected (never FILLED→attempted) ─
def test_b33_illegal_transition_rejected(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c")
    log.transition(iid, "NEAR", "FILLED")
    with pytest.raises(ei.IllegalTransitionError):
        log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED")  # FILLED→attempted
    with pytest.raises(ei.IllegalTransitionError):
        log.transition(iid, "NEAR", "SUBMITTED")  # FILLED→SUBMITTED
    assert log.get(iid)["legs"]["NEAR"]["status"] == "FILLED"  # unchanged


# ── B34: public mutation race — concurrent mark_terminal/transitions
#         serialize (internal lock), version monotonic, no lost update ────
def test_b34_public_mutation_race_serialized(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    barrier = threading.Barrier(3)
    errors = []

    def worker():
        barrier.wait()
        try:
            for _ in range(5):
                log.mark_terminal(iid, "T")  # legal from any state
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    barrier.wait()
    for t in ts:
        t.join()
    assert errors == []
    # 2 workers × 5 mutations + the initial create row = version 11
    assert log.get(iid)["version"] == 1 + 2 * 5  # no lost updates


# ── B35: child repair race — concurrent repair_complete yields EXACTLY ONE
#         active child (per-(parent,leg) idempotency; the loser is rejected) ─
def test_b35_child_repair_race_distinct_ids(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    barrier = threading.Barrier(3)
    children = []
    errors = []

    def worker():
        barrier.wait()
        try:
            child = log.repair_complete(iid, "FAR", reason="RACE")
            children.append(child["intent_id"])
        except ei.DuplicateRepairError:
            errors.append("dup")

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    barrier.wait()
    for t in ts:
        t.join()
    assert len(children) == 1  # exactly ONE active repair child
    assert len(errors) == 1     # the second call was rejected (no double order)


# ── B36: id collision resistance — many creates yield unique ids ────────
def test_b36_intent_id_collision_resistance(tmp_path):
    log = make_log(tmp_path)
    ids = set()
    for i in range(60):
        try:
            ids.add(log.create(f"t{i % 3}", "COMBINED_EXIT"))
        except ei.IntentCapacityError:
            break
    assert len(ids) <= ei.MAX_ACTIVE_INTENTS  # capacity fail-closed
    assert len(ids) == len(set(ids))  # all distinct (uuid, no collision)


# ── B37: attempted exit with NO fill (both legs rejected) ⇒ FAILED_NO_FILL,
#         never silently COMPLETED (codex #5) ────────────────────────────
def test_b37_failed_dual_leg_not_completed(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    for leg in ("NEAR", "FAR"):
        log.transition(iid, leg, "SUBMIT_ATTEMPTED", client_order_id=f"c-{leg}")
    outcome = log.recover(iid, query_fn=lambda c: {"status": "REJECTED"})
    assert outcome["terminal"] == "FAILED_NO_FILL"
    assert log.has_inflight_exit_intent("t1") is True  # position still open


# ── B38: one leg FILLED + other REJECTED ⇒ PARTIAL (repair needed) ───────
def test_b38_partial_fill_terminal(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    for leg in ("NEAR", "FAR"):
        log.transition(iid, leg, "SUBMIT_ATTEMPTED", client_order_id=f"c-{leg}")
    states = {"NEAR": "FILLED", "FAR": "REJECTED"}
    outcome = log.recover(iid, query_fn=lambda c: {"status": states[c.split("-")[1]]})
    assert outcome["terminal"] == "PARTIAL"
    assert log.has_inflight_exit_intent("t1") is True  # repair still pending


# ── B39: LIVE foreign process with the SAME OS start token is NOT
#         reclaimed (healthy owner; owner-verified against the OS) ────────
def test_b39_live_foreign_same_token_not_reclaimed(tmp_path):
    import subprocess as sp
    import sys
    log = make_log(tmp_path)
    lock_path = str(tmp_path / "intent.lock")
    code = (
        "import json,os,sys,time,subprocess\n"
        "pid=os.getpid()\n"
        "r=subprocess.run(['ps','-o','lstart=','-p',str(pid)],"
        "capture_output=True,text=True)\n"
        "tok=f'{pid}:{r.stdout.strip()}'\n"
        "open(sys.argv[1],'w').write(json.dumps({'pid':pid,'start_token':tok}))\n"
        "time.sleep(10)\n"
    )
    child = sp.Popen([sys.executable, "-c", code, lock_path])
    try:
        for _ in range(100):
            if os.path.exists(lock_path) and os.path.getsize(lock_path) > 10:
                break
            time.sleep(0.05)
        with pytest.raises(ei.LockBusyError):
            with log._file_lock():  # healthy foreign owner: NOT stolen
                pass
    finally:
        child.kill()
        child.wait()
    # after the foreign process is VERIFIED dead → owner-verified reclaim works
    # (the dead owner's lock file is reclaimed, not waited out)
    with log._file_lock():
        assert log.lock_owner()["pid"] == os.getpid()


# ── B40: LIVE pid with a STALE recorded token (PID reuse) ⇒ reclaimed ────
def test_b40_foreign_mismatch_reclaimed(tmp_path):
    log = make_log(tmp_path)
    # stale token recorded for a live pid (the OS token no longer matches)
    log._force_lock_owner({"pid": os.getpid(),
                           "start_token": f"{os.getpid()}:stale-token"})
    with log._file_lock():
        assert log.lock_owner()["pid"] == os.getpid()


# ── B41: UNVERIFIABLE owner query ⇒ fail-closed LOCK_BUSY, never reclaim ─
def test_b41_unknown_owner_check_fail_closed(tmp_path, monkeypatch):
    log = make_log(tmp_path)
    log._force_lock_owner({"pid": 12345, "start_token": "x"})
    monkeypatch.setattr(ei, "_os_start_token",
                        lambda pid: ("unknown", None))
    with pytest.raises(ei.LockBusyError):
        with log._file_lock():
            pass


# ── B42: crash-tail malformed record ⇒ CorruptLogError fail-closed (never
#         silently swallowed) ────────────────────────────────────────────
def test_b42_crash_tail_fail_closed(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    # simulate a crash mid-append: truncated JSON line at the tail
    with open(log.log_path, "a", encoding="utf-8") as fh:
        fh.write('{"intent_id": "CE-t1-trunc')
    with pytest.raises(ei.CorruptLogError):
        log.get(iid)
    with pytest.raises(ei.CorruptLogError):
        log.recover(iid, query_fn=lambda c: {"status": "NOT_FOUND"})
    with pytest.raises(ei.CorruptLogError):
        log.list_active()
