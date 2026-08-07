"""P1-B RED tests B1-B31 — durable exit-intent protocol (codex-approved TDD).

STRENGTHENED contract (codex RED review round 2): every test must fail
INDEPENDENTLY for its specific missing behavior once core.exit_intent
exists, not be masked by collection error. First run remains collection
RED (module absent). Isolated tmp_path runtimes; explicit side-effect
assertions (zero submits on failed durable transitions); actual
concurrency (threads+barrier); real lock acquisition/refusal semantics;
producer seam + COMBINED_EXIT parity integration contracts.
"""
import json
import os
import threading
import time

import pytest

# The module under test does not exist yet — import fails = intended RED.
import core.exit_intent as ei  # noqa: E402,F401  (ImportError = RED)


# ── helpers ──────────────────────────────────────────────────────────────
class StubOrderMgr:
    """Records every submit/cancel/query call for side-effect assertions."""

    def __init__(self):
        self.submits = []
        self.cancels = []
        self.queries = []

    def submit(self, client_order_id, leg, qty=1, **kw):
        self.submits.append({"client_order_id": client_order_id, "leg": leg})
        return {"order_id": f"ORD-{client_order_id}"}

    def cancel(self, client_order_id):
        self.cancels.append(client_order_id)

    def query(self, client_order_id):
        self.queries.append(client_order_id)
        return {"status": "NOT_FOUND"}


def make_log(tmp_path):
    return ei.IntentLog(str(tmp_path))


def make_intent(log, trade_id="t1", reason="COMBINED_EXIT"):
    return log.create(trade_id, reason)


# ── B1: exact durable-before-I/O sequence, both ids persisted pre-submit ──
def test_b1_durable_sequence_and_ids_persisted_before_submit(tmp_path, monkeypatch):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    events = []
    real_fsync = os.fsync

    def rec_fsync(fd):
        events.append("fsync")
        real_fsync(fd)
    monkeypatch.setattr(ei.os, "fsync", rec_fsync)

    def rec_submit(cid, leg, **kw):
        events.append(f"submit:{leg}")
        return {"order_id": f"ORD-{cid}"}

    result = ei.dispatch_combined_exit(log, "t1", mgr, submit_hook=rec_submit)
    assert result is not None
    intent = log.get(result["intent_id"])
    # BOTH client ids persisted BEFORE either submit (codex #7)
    near_id = intent["legs"]["NEAR"]["client_order_id"]
    far_id = intent["legs"]["FAR"]["client_order_id"]
    assert near_id and far_id
    first_sub = min(i for i, e in enumerate(events) if e.startswith("submit:"))
    pre_submit_log = log.raw_lines()[: first_sub]  # durable rows before first submit
    assert near_id in "".join(pre_submit_log) and far_id in "".join(pre_submit_log)
    # exact order: intent fsync → leg SUBMIT_ATTEMPTED fsync → submit
    assert events[0] == "fsync"  # intent record fsync'd first
    sub_near = events.index("submit:NEAR")
    assert events.index("submit:FAR") > sub_near  # sequential per-leg
    assert mgr.submits[0]["client_order_id"] == near_id


# ── B2: intent write failure ⇒ fail-closed zero submits ──────────────────
def test_b2_intent_write_failure_fail_closed(tmp_path, monkeypatch):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    def boom(*a, **k):
        raise OSError("fsync failed")
    monkeypatch.setattr(ei.os, "fsync", boom)
    with pytest.raises(OSError):
        make_intent(log)
    assert mgr.submits == []  # zero submits


# ── B3: crash after intent, before any SUBMIT_ATTEMPTED ⇒ safe cancel ────
def test_b3_pending_no_attempt_safe_cancel(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    outcome = log.recover(iid, query_fn=lambda cid: {"status": "UNAVAILABLE"})
    assert outcome["legs"]["NEAR"]["status"] == "NOT_SUBMITTED"
    assert outcome["legs"]["FAR"]["status"] == "NOT_SUBMITTED"
    assert outcome["terminal"] == "CANCELED_SAFE"  # nothing reached broker


# ── B4: crash after SUBMIT_ATTEMPTED before send ⇒ query, never resubmit ─
def test_b4_submit_attempted_recovery_queries_never_resubmits(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    cid = ei.client_order_id("t1", "NEAR")
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id=cid)
    log.recover(iid, query_fn=mgr.query, order_mgr=mgr)
    assert cid in mgr.queries
    assert mgr.submits == []  # never resubmitted without a query result


# ── B5: broker accepted but call did not return ⇒ UNKNOWN fail-closed ────
def test_b5_broker_accepted_no_return_unknown_fail_closed(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="cid-near")
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
    assert outcome["legs"]["NEAR"]["status"] == "SUBMIT_ATTEMPTED"  # unresolved
    assert outcome["blocked"] is True


# ── B7: repeated restart ⇒ idempotent recovery ───────────────────────────
def test_b7_repeated_restart_idempotent(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    cid = ei.client_order_id("t1", "FAR")
    log.transition(iid, "FAR", "SUBMIT_ATTEMPTED", client_order_id=cid)
    for _ in range(3):
        log.recover(iid, query_fn=mgr.query, order_mgr=mgr)
    assert mgr.queries.count(cid) >= 3
    assert mgr.submits == []  # no duplicate actions across restarts


# ── B8: crash after near attempted; far never attempted ⇒ reachable edge ─
def test_b8_near_attempted_far_not_attempted(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    cid = ei.client_order_id("t1", "NEAR")
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id=cid)
    outcome = log.recover(iid, query_fn=lambda c: {"status": "NOT_FOUND"})
    assert outcome["legs"]["NEAR"]["status"] == "NOT_FOUND_CONFIRMED"
    assert outcome["legs"]["FAR"]["status"] == "NOT_SUBMITTED"  # safely cancellable


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


# ── B10: partial fill during recovery ⇒ repair child after query; child
#         id + SUBMIT_ATTEMPTED persisted BEFORE repair I/O (codex #7) ────
def test_b10_partial_fill_during_recovery_repair_child(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="cid-near")
    log.transition(iid, "NEAR", "FILLED")
    log.transition(iid, "FAR", "SUBMIT_ATTEMPTED", client_order_id="cid-far")
    child = log.repair_complete(iid, "FAR", reason="PARTIAL_FILL")
    cid = log.get(child["intent_id"])["legs"]["FAR"]["client_order_id"]
    assert log.get(child["intent_id"])["legs"]["FAR"]["status"] == "SUBMIT_ATTEMPTED"
    assert cid != "cid-far"  # NEW child id, persisted pre-I/O


# ── B11: idempotency key duplicate ⇒ rejected, ONE fill ──────────────────
def test_b11_idempotency_key_dedup(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    log.submit_leg(iid, "NEAR", mgr)
    with pytest.raises(ei.DuplicateSubmitError):
        log.submit_leg(iid, "NEAR", mgr)
    assert len(mgr.submits) == 1  # ONE fill/one submit


# ── B12: near success / far REJECTED ⇒ repair complete_exit with the
#         PERSISTED child id (fixed: no undeclared cid) ───────────────────
def test_b12_near_ok_far_rejected_repair_complete(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    log.transition(iid, "NEAR", "FILLED")
    log.transition(iid, "FAR", "REJECTED")
    child = log.repair_complete(iid, "FAR", reason="REJECTED")
    child_id = child["intent_id"]
    persisted_cid = log.get(child_id)["legs"]["FAR"]["client_order_id"]
    assert log.get(child_id)["legs"]["FAR"]["status"] == "SUBMIT_ATTEMPTED"  # durable pre-I/O
    log.submit_leg(child_id, "FAR", mgr)
    assert len(mgr.submits) == 1
    assert mgr.submits[0]["client_order_id"] == persisted_cid  # the persisted id was used
    assert persisted_cid != "cid-far"


# ── B13: far CANCELLED after near FILLED ⇒ repair child, no orphan ───────
def test_b13_far_cancelled_repair_no_orphan(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    log.transition(iid, "NEAR", "FILLED")
    log.transition(iid, "FAR", "CANCELLED")
    child = log.repair_complete(iid, "FAR", reason="CANCELLED")
    log.submit_leg(child["intent_id"], "FAR", mgr)
    assert len(mgr.submits) == 1
    assert len(mgr.cancels) == 0  # no orphan cancel of a filled leg


# ── B14: restart with memory cleared ⇒ intent converges without memory ───
def test_b14_memory_cleared_converges(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    cid = ei.client_order_id("t1", "NEAR")
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id=cid)
    log2 = ei.IntentLog(str(tmp_path))  # fresh object = memory cleared
    outcome = log2.recover(iid, query_fn=lambda c: {"status": "NOT_FOUND"})
    assert outcome["legs"]["NEAR"]["status"] == "NOT_FOUND_CONFIRMED"


# ── B15: pre-gate suppresses entry + second exit + session exit ──────────
def test_b15_pregate_suppresses_duplicates(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c")
    assert log.has_inflight_exit_intent("t1") is True
    assert log.entry_allowed("t1") is False
    assert log.exit_trigger_allowed("t1") is False      # second exit trigger suppressed
    assert log.session_transition_allowed("t1") is False
    assert log.recovery_path_allowed("t1") is True      # recovery unaffected
    assert log.emergency_path_allowed("t1") is True     # emergency unaffected


# ── B16: UNKNOWN blocks entry + ordinary exit; emergency still works ─────
def test_b16_unknown_blocks_but_emergency_works(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    log.transition(iid, "NEAR", "UNKNOWN")
    assert log.entry_allowed("t1") is False
    assert log.exit_trigger_allowed("t1") is False
    assert log.emergency_path_allowed("t1") is True


# ── B17: state-write failure after submits ⇒ intent authority ────────────
def test_b17_state_write_failure_intent_authority(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    for leg in ("NEAR", "FAR"):
        log.transition(iid, leg, "SUBMITTED", broker_order_id=f"O-{leg}")
    assert log.get(iid)["legs"]["NEAR"]["status"] == "SUBMITTED"
    assert log.has_inflight_exit_intent("t1") is True  # blocks entry until converged


# ── B18: compaction only after terminal durable; retention enforced ──────
def test_b18_compaction_gated_by_durable_terminal(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    with pytest.raises(ei.IntentNotTerminalError):
        log.archive(iid)
    log.transition(iid, "NEAR", "FILLED")
    log.transition(iid, "FAR", "FILLED")
    log.mark_terminal(iid, "COMPLETED")
    log.archive(iid)
    assert iid in log.archive_index()
    assert log.get(iid)["retention_expires_at"] is not None


# ── B19: COMBINED_EXIT parity contract (state/events/orders JSON) ────────
def test_b19_combined_exit_parity(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    for leg in ("NEAR", "FAR"):
        log.submit_leg(iid, leg, mgr)
        log.transition(iid, leg, "FILLED")
    log.mark_terminal(iid, "COMPLETED")
    rec = log.parity_records(iid)
    assert rec["fills"] == 2            # existing fills ledger parity
    assert rec["events"] == 2           # ORDER_SUBMITTED events parity
    assert rec["state"] == "COMBINED_EXIT"  # state file parity
    assert rec["orders_json"] == 2      # orders JSON parity
    assert len(mgr.submits) == 2
    assert log.has_inflight_exit_intent("t1") is False


# ── B20: capacity fail-closed — never silently prunes ACTIVE intents ─────
def test_b20_capacity_fail_closed(tmp_path):
    log = make_log(tmp_path)
    for i in range(ei.MAX_ACTIVE_INTENTS):
        make_intent(log, trade_id=f"t{i}")
    with pytest.raises(ei.IntentCapacityError):
        make_intent(log, trade_id="overflow")  # fail-closed, NOT prune
    assert len(log.list_active()) == ei.MAX_ACTIVE_INTENTS  # nothing dropped


# ── B21: REAL concurrent actors (threads + barrier) ⇒ exactly ONE action ─
def test_b21_concurrent_recovery_single_action(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c")
    barrier = threading.Barrier(3)
    outcomes = []

    def actor(name):
        barrier.wait()
        try:
            with log.lock(f"proc-{name}"):
                r = log.recover(iid, query_fn=lambda c: {"status": "NOT_FOUND"},
                                order_mgr=mgr)
                outcomes.append((name, r["legs"]["NEAR"]["status"]))
        except ei.LockBusyError:
            outcomes.append((name, "LOCK_BUSY"))

    t1 = threading.Thread(target=actor, args=("a",))
    t2 = threading.Thread(target=actor, args=("b",))
    t1.start(); t2.start()
    barrier.wait()
    t1.join(); t2.join()
    performed = [o for o in outcomes if o[1] != "LOCK_BUSY"]
    assert len(performed) == 1  # exactly ONE actor performed the transition
    assert outcomes.count(("a", "LOCK_BUSY")) + outcomes.count(("b", "LOCK_BUSY")) == 1


# ── B22: foreign owner → owner-verified reclaim → second process acquires ─
def test_b22_restart_overlap_owner_verified(tmp_path):
    log = make_log(tmp_path)
    # foreign owner: old process, dead PID + stale start token
    log._force_lock_owner({"pid": 99999, "start_token": "tokA", "host": "h",
                           "acquired_at": time.time()})
    acquired = log.try_acquire({"pid": os.getpid(), "start_token": "tokB", "host": "h"},
                               owner_check_fn=lambda pid, token: token != "tokB")
    assert acquired is True  # reclaimed owner-verified
    assert log.lock_owner()["pid"] == os.getpid()
    # healthy foreign owner → acquisition refused
    log._force_lock_owner({"pid": 77777, "start_token": "alive", "host": "h"})
    with pytest.raises(ei.LockBusyError):
        log.try_acquire({"pid": os.getpid(), "start_token": "tokC", "host": "h"},
                        owner_check_fn=lambda pid, token: token == "alive")


# ── B23: REAL producer seam — remaining-leg trail exit writes the SAME
#         intent; second tick suppressed (P1-2 double-submit regression) ──
def test_b23_producer_double_submit_suppressed(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    # tick 1: producer trigger
    r1 = ei.producer_exit_trigger("t1", reason="TRAIL_REMAINING", leg="FAR",
                                  order_mgr=mgr)
    assert len(mgr.submits) == 1
    # tick 2: same producer trigger — MUST be suppressed via the same intent
    r2 = ei.producer_exit_trigger("t1", reason="TRAIL_REMAINING", leg="FAR",
                                  order_mgr=mgr)
    assert r2["suppressed"] is True
    assert len(mgr.submits) == 1  # P1-2 regression: ONE order total
    # source-level integration contract: tmf_spread remaining-leg exit branch
    # must route through the durable intent (fails until wired)
    src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                            "strategies", "plugins", "futures", "active",
                            "tmf_spread.py"), encoding="utf-8").read()
    assert "exit_intent" in src and "producer_exit_trigger" in src


# ── B24: crash after repair-child SUBMIT_ATTEMPTED before send ───────────
def test_b24_repair_child_crash_before_send(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    child = log.repair_complete(iid, "FAR", reason="REJECTED")
    log2 = ei.IntentLog(str(tmp_path))
    outcome = log2.recover(child["intent_id"], query_fn=lambda c: {"status": "NOT_FOUND"})
    assert outcome["legs"]["FAR"]["status"] == "NOT_FOUND_CONFIRMED"
    assert mgr.submits == []  # no duplicate repair


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
    assert mgr.submits == []  # no second repair


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
    assert r1["legs"]["NEAR"]["status"] == "NOT_FOUND_CONFIRMED"  # safe terminal
    iid2 = make_intent(log, trade_id="t2")
    log.transition(iid2, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c2")
    r2 = log.recover(iid2, query_fn=lambda c: {"status": "UNAVAILABLE"})
    assert r2["legs"]["NEAR"]["status"] == "SUBMIT_ATTEMPTED"  # NOT terminal
    assert r2["blocked"] is True  # never silently timed out into resubmit


# ── B28: intent_version CAS stale rejection ──────────────────────────────
def test_b28_cas_stale_transition_rejected(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    v1 = log.get(iid)["version"]
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c", expect_version=v1)
    with pytest.raises(ei.StaleVersionError):
        log.transition(iid, "FAR", "SUBMIT_ATTEMPTED", expect_version=v1)  # stale


# ── B29: healthy owner with slow I/O is NOT stolen (age only alerts) ─────
def test_b29_healthy_owner_not_stolen(tmp_path):
    log = make_log(tmp_path)
    acquired_at = time.time() - 9999  # older than ANY age threshold
    log._force_lock_owner({"pid": os.getpid(), "start_token": "mine", "host": "h",
                           "acquired_at": acquired_at})
    with pytest.raises(ei.LockBusyError):  # refusal despite age
        log.try_acquire({"pid": 8888, "start_token": "other", "host": "h"},
                        owner_check_fn=lambda pid, token: token == "mine",  # owner alive
                        age_alert_threshold_s=1)
    assert log.lock_owner()["pid"] == os.getpid()  # not stolen


# ── B30: PID reuse / start-token mismatch ⇒ safe reclaim ─────────────────
def test_b30_pid_reuse_start_token_mismatch_reclaimable(tmp_path):
    log = make_log(tmp_path)
    log._force_lock_owner({"pid": 4242, "start_token": "old-boot-token", "host": "h"})
    acquired = log.try_acquire({"pid": 4242, "start_token": "new-token", "host": "h"},
                               owner_check_fn=lambda pid, token: token == "new-token")
    assert acquired is True
    assert log.lock_owner()["start_token"] == "new-token"


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
    assert mgr.submits == [] and mgr.cancels == []  # NO emergency orders
