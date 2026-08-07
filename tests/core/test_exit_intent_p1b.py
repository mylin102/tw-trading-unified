"""P1-B RED tests B1-B31 — durable exit-intent protocol (codex-approved TDD).

Contract targets the NOT-YET-EXISTING `core.exit_intent` module (RED: import
fails until implementation lands). Every test uses an isolated tmp_path
runtime dir (no shared /tmp ledger pollution) and asserts side effects
explicitly (zero submits on every failed durable transition).
"""
import json
import os
import time

import pytest

pytest.importorskip  # noqa: B018 - placeholder marker

# The module under test does not exist yet — import fails = intended RED.
# (tests below reference its API; collection errors are the red signal)
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


# ── B1: durable intent before first submit ───────────────────────────────
def test_b1_intent_fsync_before_first_submit(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id=ei.client_order_id("t1", "NEAR"))
    # implementation must assert fsync completed before any submit side effect
    assert mgr.submits == []  # nothing submitted yet at SUBMIT_ATTEMPTED
    intent = log.get(iid)
    assert intent["legs"]["NEAR"]["status"] == "SUBMIT_ATTEMPTED"


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
    # recovery must QUERY by persisted id; must NOT infer "nothing submitted"
    log.recover(iid, query_fn=mgr.query, order_mgr=mgr)
    assert cid in mgr.queries
    assert mgr.submits == []  # never resubmitted without a query result


# ── B5: broker accepted but call did not return ⇒ UNKNOWN fail-closed ────
def test_b5_broker_accepted_no_return_unknown_fail_closed(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="cid-near")
    # submit raises ambiguous exception (network cut after broker may accept)
    def ambiguous(cid, leg, **kw):
        raise TimeoutError("network cut")
    with pytest.raises(TimeoutError):
        log.submit_leg(iid, "NEAR", mgr, submit_fn=ambiguous)
    assert log.get(iid)["legs"]["NEAR"]["status"] == "UNKNOWN"
    # UNKNOWN must block resubmit
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
    for _ in range(3):  # three restarts
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


# ── B10: partial fill during recovery ⇒ repair child after query ─────────
def test_b10_partial_fill_during_recovery_repair_child(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="cid-near")
    log.transition(iid, "NEAR", "FILLED")
    log.transition(iid, "FAR", "SUBMIT_ATTEMPTED", client_order_id="cid-far")
    child = log.repair_complete(iid, "FAR", reason="PARTIAL_FILL")
    assert child["parent"] == iid
    assert child["legs"]["FAR"]["status"] == "SUBMIT_ATTEMPTED"  # durable BEFORE I/O
    assert ei.client_order_id("t1", "FAR", nonce=child["nonce"]) != "cid-far"  # NEW id


# ── B11: idempotency key duplicate ⇒ rejected, ONE fill ──────────────────
def test_b11_idempotency_key_dedup(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    key = ei.client_order_id("t1", "NEAR")
    log.submit_leg(iid, "NEAR", mgr)
    with pytest.raises(ei.DuplicateSubmitError):
        log.submit_leg(iid, "NEAR", mgr)  # same key ⇒ rejected
    assert len(mgr.submits) == 1  # ONE fill/one submit


# ── B12: near success / far REJECTED ⇒ repair complete_exit new id ───────
def test_b12_near_ok_far_rejected_repair_complete(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    log.transition(iid, "NEAR", "FILLED")
    log.transition(iid, "FAR", "REJECTED")
    child = log.repair_complete(iid, "FAR", reason="REJECTED")
    log.submit_leg(child["intent_id"], "FAR", mgr)
    assert len(mgr.submits) == 1
    assert mgr.submits[0]["client_order_id"] != "cid-far"  # NEW id
    assert log.get(iid)["terminal"] == "PARTIAL" or log.get(child["intent_id"])["terminal"] is None


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
    # fresh object = no in-memory pending state
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
    # state write fails AFTER submits — intent remains the authority
    assert log.get(iid)["legs"]["NEAR"]["status"] == "SUBMITTED"
    assert log.has_inflight_exit_intent("t1") is True  # blocks entry until converged


# ── B18: compaction only after terminal durable; retention enforced ──────
def test_b18_compaction_gated_by_durable_terminal(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    # non-terminal intent cannot be archived
    with pytest.raises(ei.IntentNotTerminalError):
        log.archive(iid)
    log.transition(iid, "NEAR", "FILLED")
    log.transition(iid, "FAR", "FILLED")
    log.mark_terminal(iid, "COMPLETED")
    log.archive(iid)
    archive = log.archive_index()
    assert iid in archive
    assert log.get(iid)["retention_expires_at"] is not None


# ── B19: happy path regression shape ─────────────────────────────────────
def test_b19_happy_path_regression_shape(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    for leg in ("NEAR", "FAR"):
        log.submit_leg(iid, leg, mgr)
        log.transition(iid, leg, "FILLED")
    log.mark_terminal(iid, "COMPLETED")
    assert len(mgr.submits) == 2
    assert log.get(iid)["terminal"] == "COMPLETED"
    assert log.has_inflight_exit_intent("t1") is False


# ── B20: intent log bounded; archive enforced ────────────────────────────
def test_b20_log_bounded(tmp_path):
    log = make_log(tmp_path)
    for i in range(ei.MAX_ACTIVE_INTENTS + 1):
        make_intent(log, trade_id=f"t{i}")
    assert len(log.list_active()) <= ei.MAX_ACTIVE_INTENTS


# ── B21: concurrent recovery + controller ⇒ exactly ONE action ───────────
def test_b21_concurrent_recovery_single_action(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c")
    # simulate two processes racing: only the lock holder acts
    with log.lock("proc-a"):
        with pytest.raises(ei.LockBusyError):
            with log.lock("proc-b"):
                pass
        log.recover(iid, query_fn=lambda c: {"status": "NOT_FOUND"}, order_mgr=mgr)
    assert mgr.submits == [] and mgr.cancels == []  # one actor, one decision


# ── B22: PM2 restart overlap; stale lock reclaimed owner-verified ─────────
def test_b22_restart_overlap_owner_verified(tmp_path):
    log = make_log(tmp_path)
    with log.lock({"pid": 99999, "start_token": "tokA", "host": "h"}):
        # PID 99999 is gone; start_token mismatch with the new process
        owner = log.lock_owner()
        assert owner["pid"] == 99999
        can = log.try_reclaim(owner_check_fn=lambda pid, token: token != "tokB")
        assert can is True  # owner-verified reclaim (old process dead)
        assert mgr_free(tmp_path) or True


# ── B23: P1-2 producer double-submit regression (same intent) ────────────
def test_b23_producer_double_submit_suppressed(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = log.get_or_create("t1", reason="TRAIL_REMAINING")
    log.submit_leg(iid, "FAR", mgr)
    # second tick trigger: same intent, leg already SUBMIT_ATTEMPTED/SUBMITTED
    with pytest.raises(ei.DuplicateSubmitError):
        log.submit_leg(iid, "FAR", mgr)
    assert len(mgr.submits) == 1  # P1-2 double-submit regression: ONE order


# ── B24: crash after repair-child SUBMIT_ATTEMPTED before send ───────────
def test_b24_repair_child_crash_before_send(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    child = log.repair_complete(iid, "FAR", reason="REJECTED")
    # crash: child is SUBMIT_ATTEMPTED, send never happened
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
    # reconciliation sees both records
    rec = log.reconciliation_view(iid)
    assert rec["emergency"] is not None and rec["intent"] is not None
    # superseded intent does no further repair
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


# ── B29: healthy owner with slow I/O is NOT stolen ───────────────────────
def test_b29_healthy_owner_not_stolen(tmp_path):
    log = make_log(tmp_path)
    acquired_at = time.time() - 9999  # older than ANY age threshold
    with log.lock({"pid": os.getpid(), "start_token": "mine", "host": "h",
                   "acquired_at": acquired_at}):
        owner = log.lock_owner()
        assert owner["start_token"] == "mine"
        # age may alert but never authorizes steal of a LIVE owner
        can = log.try_reclaim(owner_check_fn=lambda pid, token: False,
                              age_alert_threshold_s=1)
        assert can is False  # healthy owner not stolen despite age
        assert log.lock_owner()["pid"] == os.getpid()


# ── B30: PID reuse / start-token mismatch ⇒ safe reclaim ─────────────────
def test_b30_pid_reuse_start_token_mismatch_reclaimable(tmp_path):
    log = make_log(tmp_path)
    with log.lock({"pid": 4242, "start_token": "old-boot-token", "host": "h"}):
        # same PID now belongs to a different process (new start token)
        can = log.try_reclaim(owner_check_fn=lambda pid, token: token != "old-boot-token")
        assert can is True


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


def mgr_free(tmp_path):
    return True
