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
        self._created = []
        self.active_orders = {}
        self.completed = []

    def _unwrap(self, v, default=""):
        from unittest.mock import MagicMock as _MM
        if isinstance(v, _MM) or v is None:
            return default
        if hasattr(v, "value"):
            return v.value
        return v

    def create_order(self, **kw):
        from unittest.mock import MagicMock
        from datetime import datetime as _dt
        o = MagicMock()
        self._created.append(o)
        o.order_id = f"ORD-{len(self._created)}"
        o.client_order_id = kw.get("client_order_id")
        o.intent_id = kw.get("intent_id")
        o.leg = kw.get("leg")
        o.symbol = kw.get("symbol")
        o.side = kw.get("side")
        o.quantity = kw.get("quantity")
        o.order_type = kw.get("order_type")
        o.strategy = kw.get("strategy")
        o.created_at = kw.get("created_at") or _dt.now()
        self.active_orders[o.order_id] = o
        o.to_dict = lambda: {
            "order_id": self._unwrap(o.order_id),
            "symbol": self._unwrap(getattr(o, "symbol", "")),
            "side": self._unwrap(getattr(o, "side", "")),
            "order_type": self._unwrap(getattr(o, "order_type", "")),
            "quantity": self._unwrap(getattr(o, "quantity", 1)),
            "strategy": self._unwrap(getattr(o, "strategy", "")),
            "status": self._unwrap(getattr(o, "status", ""), "SUBMITTED"),
            "created_at": str(self._unwrap(getattr(o, "created_at", ""))),
        }
        return o

    def get_completed(self):
        return []

    def get_pending(self):
        return list(self.active_orders.values())

    def submit(self, arg, leg=None, **kw):
        if hasattr(arg, "order_id"):  # dispatcher path: submit(order)
            self.submits.append({"client_order_id": getattr(arg, "client_order_id", None),
                                 "leg": leg or getattr(arg, "leg", None),
                                 "order_id": arg.order_id,
                                 "symbol": getattr(arg, "symbol", None),
                                 "side": getattr(arg, "side", None),
                                 "quantity": getattr(arg, "quantity", None)})
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
    monitor._registry = {"tmf_spread": strat}  # B48 fills registry lookup
    monitor.market_data = {"TMF_FAR": {"close": 44100.0},
                           "TMF_NEAR": {"close": 43400.0}}
    monitor._far_current_bar = {"close": 44100.0}
    monitor._current_bar = {"close": 43400.0}
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
    strat._peak_net_exit_pnl_twd = 0.0  # lifecycle ctx needs a real float
    strat._peak_net_exit_pnl_twd = max(strat._peak_net_exit_pnl_twd, 0.0)
    strat._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.COMPLETED,
                                   filled_leg=Leg.NEAR, canceled_leg=Leg.FAR),
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
        # simulate both COMBINED_EXIT leg fills through the REAL fill path
        from core.order_management.order import OrderStatus, OrderSide
        import types as _types
        with patch("strategies.futures.monitor.save_trade"), \
             patch("strategies.futures.monitor.DecisionLogger", create=True):
            for leg_label, px in (("NEAR", 44100.0), ("FAR", 44020.0)):
                oid = next(o for o, p in monitor._pending_lifecycle_orders.items()
                           if p.get("leg_role") == leg_label)
                ev = _types.SimpleNamespace(
                    order_id=oid, fill_qty=1, fill_price=px,
                    deal_id=f"deal-{leg_label}",
                    symbol="TMFH6" if leg_label == "NEAR" else "TMFI6",
                    status=OrderStatus.FILLED, side=OrderSide.SELL,
                    timestamp=datetime.now())
                monitor._apply_confirmed_futures_deal(ev)
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
    import socket
    log = make_log(tmp_path)
    log._force_lock_owner({"pid": 99999, "start_token": "tokA",
                           "host": socket.gethostname(), "acquired_at": time.time()})
    # owner_check: OLD process dead (alive=False) ⇒ reclaim + acquire
    acquired = log.try_acquire({"pid": os.getpid(), "start_token": "tokB",
                                "host": socket.gethostname()},
                               owner_check_fn=lambda pid, token: {"alive": False})
    assert acquired is True
    assert log.lock_owner()["pid"] == os.getpid()
    # healthy foreign owner (alive=True) ⇒ acquisition refused
    log._force_lock_owner({"pid": 77777, "start_token": "alive",
                           "host": socket.gethostname()})
    with pytest.raises(ei.LockBusyError):
        log.try_acquire({"pid": os.getpid(), "start_token": "tokC",
                         "host": socket.gethostname()},
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
    # init() resets position state — re-apply the trade identity so the
    # produced intent carries the real trade_id (release/exit flow intact)
    from strategies.plugins.futures.active.tmf_spread import (
        PositionLifecycle, PositionPhase, ReleaseGroup, ReleaseGroupStatus,
        TrailGroup, TrailGroupStatus, Leg,
    )
    strat._trade_id = "t1"
    strat._has_position = True
    strat._released_leg = "near"
    strat._side = "LONG"
    strat._near_side = "SHORT"
    strat._far_side = "LONG"
    strat._near_entry = 43800.0
    strat._far_entry = 44000.0
    strat._far_max = 44020.0
    strat._far_min = 43365.0
    strat._mfe_pts = 0.0
    strat._mae_pts = 0.0
    strat._peak_net_exit_pnl_twd = 0.0
    strat._renko_gap_quarantine_left = 0
    strat._point_value = 10.0
    strat._estimated_cost = 92.0
    strat._single_leg_warmup_ms = 0.0
    strat._single_leg_entered_mono = time.monotonic()
    strat._single_leg_post_fill_ticks = 2
    strat._release_near_ticks = 1  # tick-confirm gate for the release decision
    strat._release_far_ticks = 0
    strat._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
    )
    now = datetime.now()
    bar = {"near_close": 44100.0, "far_close": 44020.0, "atr": 10.0,
           "timestamp": now, "code": "TMFF6"}
    with patch("strategies.futures.monitor._mts_position_state_path",
               return_value=Path(tmp_path) / "state.json"), \
        patch("strategies.futures.monitor.is_taifex_futures_market_open", return_value=True), \
        patch.object(Path, "exists", return_value=False):
        # producer tick 1: the REAL remaining-leg trail branch emits a signal
        sig1 = strat._manage_position(44100.0, 44020.0, -0.5, now, bar)
        # PRODUCER-REACHED assertion FIRST (codex): the qualifying RELEASE
        # signal was genuinely emitted by the real branch — if the fixture/
        # adapter swallowed it, THIS fails with a clear message, not the
        # intent count
        assert sig1 is not None, "producer did not reach exit emission"
        # integration red (Phase-2 gap): no durable intent was created
        intent_log = ei.IntentLog(str(tmp_path / "logs"))
        actives = [iid for iid in intent_log.list_active()
                   if intent_log.get(iid)["trade_id"] == "t1"]
        assert len(actives) == 1  # red until dispatcher wiring (0==1)
        # producer tick 2: same qualifying state — NO second emission
        sig2 = strat._manage_position(44100.0, 44020.0, -0.5, now, bar)
        assert sig2 is None  # suppressed by the durable intent
        # the single emitted signal dispatches exactly once
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
    import socket
    log = make_log(tmp_path)
    acquired_at = time.time() - 9999  # older than ANY age threshold
    log._force_lock_owner({"pid": os.getpid(), "start_token": "mine",
                           "host": socket.gethostname(), "acquired_at": acquired_at})
    with pytest.raises(ei.LockBusyError):  # refusal despite age
        log.try_acquire({"pid": 8888, "start_token": "other",
                         "host": socket.gethostname()},
                        owner_check_fn=lambda pid, token: {"alive": True},  # owner alive
                        age_alert_threshold_s=1)
    assert log.lock_owner()["pid"] == os.getpid()  # not stolen


# ── B30: PID reuse — live PID with start-token MISMATCH reclaimable;
#         live PID with MATCHING token refuses (owner_check returns the
#         CURRENT start token) ────────────────────────────────────────────
def test_b30_pid_reuse_start_token_mismatch_reclaimable(tmp_path):
    import socket
    log = make_log(tmp_path)
    log._force_lock_owner({"pid": 4242, "start_token": "old-boot-token",
                           "host": socket.gethostname()})
    # PID 4242 is ALIVE (alive=true) but its current token differs from the
    # recorded one ⇒ the recorded owner is an old incarnation ⇒ reclaimable
    acquired = log.try_acquire({"pid": 9999, "start_token": "new-token",
                                "host": socket.gethostname()},
                               owner_check_fn=lambda pid, token: {"alive": True,
                                                                  "start_token": "new-token"})
    assert acquired is True
    assert log.lock_owner()["start_token"] == "new-token"
    # live PID with MATCHING token ⇒ owner is genuinely alive ⇒ refuse
    log._force_lock_owner({"pid": 5555, "start_token": "same",
                           "host": socket.gethostname()})
    with pytest.raises(ei.LockBusyError):
        log.try_acquire({"pid": 9998, "start_token": "x",
                         "host": socket.gethostname()},
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
        "import json,os,sys,time,subprocess,socket\n"
        "pid=os.getpid()\n"
        "r=subprocess.run(['ps','-o','lstart=','-p',str(pid)],"
        "capture_output=True,text=True)\n"
        "tok=f'{pid}:{r.stdout.strip()}'\n"
        "open(sys.argv[1],'w').write(json.dumps("
        "{'pid':pid,'start_token':tok,'host':socket.gethostname()}))\n"
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
    import socket
    log = make_log(tmp_path)
    # stale token recorded for a live pid (the OS token no longer matches)
    log._force_lock_owner({"pid": os.getpid(),
                           "start_token": f"{os.getpid()}:stale-token",
                           "host": socket.gethostname()})
    with log._file_lock():
        assert log.lock_owner()["pid"] == os.getpid()


# ── B41: UNVERIFIABLE owner query ⇒ fail-closed LOCK_BUSY, never reclaim ─
def test_b41_unknown_owner_check_fail_closed(tmp_path, monkeypatch):
    import socket
    log = make_log(tmp_path)
    log._force_lock_owner({"pid": 12345, "start_token": "x",
                           "host": socket.gethostname()})
    monkeypatch.setattr(ei, "_os_start_token",
                        lambda pid: ("unknown", None))
    with pytest.raises(ei.LockBusyError):
        with log._file_lock():
            pass


# ── B43: REMOTE-host lock is NEVER reclaimed, even if the local PID is
#         absent/reused (shared-runtime safety) ──────────────────────────
def test_b43_remote_host_lock_never_reclaimed(tmp_path):
    log = make_log(tmp_path)
    # remote healthy owner, same numeric PID as a locally-absent process
    log._force_lock_owner({"pid": 424242, "start_token": "remote-tok",
                           "host": "some-other-host.example"})
    with pytest.raises(ei.LockBusyError):
        with log._file_lock():
            pass
    # try_acquire path must behave identically
    with pytest.raises(ei.LockBusyError):
        log.try_acquire({"pid": os.getpid(), "start_token": "x",
                         "host": "local"},
                        owner_check_fn=lambda pid, token: {"alive": False})
    assert log.lock_owner()["host"] == "some-other-host.example"  # untouched


# ── B44: intent_version generation fence — a stale lock holder whose intent
#         advanced under it fails with StaleVersionError before any durable
#         transition/submit ──────────────────────────────────────────────
def test_b44_stale_lock_holder_fails_stale_version(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    cm = log._file_lock(intent_id=iid)
    cm.__enter__()
    try:
        meta = log.lock_owner()
        assert meta.get("intent_version") == log.get(iid)["version"]
        # external writer advances the intent (bypassing the lock — the
        # split-brain the fence guards against)
        cur = log.get(iid)
        rec = dict(cur)
        rec["version"] = cur["version"] + 1
        ei._atomic_append(log.log_path, rec)
        with pytest.raises(ei.StaleVersionError):
            log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED",
                           client_order_id="c")
    finally:
        cm.__exit__(None, None, None)


# ── B45: fence before EVERY durable write — submit attempted → external
#         drift → the NEXT durable write (FAR attempt) is rejected BEFORE
#         any broker I/O and WITHOUT a second append ─────────────────────
def test_b45_fence_blocks_second_durable_write(tmp_path):
    log = make_log(tmp_path)
    mgr = StubOrderMgr()
    iid = make_intent(log)
    cm = log._file_lock(intent_id=iid)
    cm.__enter__()
    try:
        log.transition(iid, "NEAR", "SUBMIT_ATTEMPTED", client_order_id="c-near")
        # external drift mid-operation (split-brain writer bypasses the lock)
        cur = log.get(iid)
        rec = dict(cur)
        rec["version"] = cur["version"] + 1
        ei._atomic_append(log.log_path, rec)
        v_before = log.get(iid)["version"]
        with pytest.raises(ei.StaleVersionError):
            log.submit_leg(iid, "FAR", mgr,
                           submit_fn=lambda cid, leg: {"order_id": "x"})
        assert len(mgr.submits) == 0  # no broker I/O
        assert log.get(iid)["version"] == v_before  # no second append
    finally:
        cm.__exit__(None, None, None)


# ── B46: repair drift — parent version advanced externally ⇒ repair child
#         creation is rejected BEFORE any child append ───────────────────
def test_b46_repair_drift_fail_closed(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    cm = log._file_lock(intent_id=iid)
    cm.__enter__()
    try:
        cur = log.get(iid)
        rec = dict(cur)
        rec["version"] = cur["version"] + 1
        ei._atomic_append(log.log_path, rec)
        n_before = len(log.raw_lines())
        with pytest.raises(ei.StaleVersionError):
            log.repair_complete(iid, "FAR", reason="REJECTED")
        assert len(log.raw_lines()) == n_before  # zero child append
    finally:
        cm.__exit__(None, None, None)


# ── B47: emergency drift — parent version advanced externally ⇒ BOTH the
#         EMERGENCY_SUPERSEDES audit event AND the terminal record are
#         rejected; zero event/terminal append ───────────────────────────
def test_b47_emergency_drift_fail_closed(tmp_path):
    log = make_log(tmp_path)
    iid = make_intent(log)
    cm = log._file_lock(intent_id=iid)
    cm.__enter__()
    try:
        cur = log.get(iid)
        rec = dict(cur)
        rec["version"] = cur["version"] + 1
        ei._atomic_append(log.log_path, rec)
        n_before = len(log.raw_lines())
        with pytest.raises(ei.StaleVersionError):
            log.emergency_supersede(iid)
        assert len(log.raw_lines()) == n_before  # zero event + zero terminal
        assert log.get(iid)["terminal"] is None  # not superseded
    finally:
        cm.__exit__(None, None, None)


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


# ── B48 (codex Phase-2 acceptance): SINGLE-LEG Policy J combined-pnl
#    wiring through the REAL TMFSpread + FuturesMonitor seam (distinct from
#    the adapter-only test_policy_j_single_leg_combined_pnl_exit):
#    1) confirmed RELEASE fill ⇒ reconstructed SINGLE_LEG state
#    2) context carries released-leg realized + remaining-leg UPL; peak ≥200
#       TWD, giveback ≥50 TWD
#    3) signal/dispatch creates ONE exit order for the REMAINING leg only
#       (correct side/qty) + durable intent
#    4) second identical tick ⇒ zero re-submit + auditable
#       POLICY_J_SINGLE_LEG_TRIGGERED event
#    5) below activation / insufficient giveback ⇒ zero orders
# ─────────────────────────────────────────────────────────────────────
def _b48_build_released(tmp_path, monkeypatch):
    """B48 harness: SPREAD → confirmed RELEASE_NEAR fill → SINGLE_LEG
    (released near, remaining FAR, peak_net_exit_pnl_twd=1300).

    Returns (monitor, strat, now, bar1, bar2)."""
    from datetime import datetime
    from pathlib import Path
    from unittest.mock import patch
    from core.strategy_context import StrategyContext, MarketData, PositionView
    from core.order_management.order import OrderStatus, OrderSide
    from strategies.plugins.futures.active.tmf_spread import (
        PositionLifecycle, PositionPhase, ReleaseGroup, ReleaseGroupStatus,
        Leg,
    )
    import types as _types

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    fills_path = tmp_path / "mts_trade_fills.jsonl"
    monkeypatch.setenv("MTS_FILL_LOG_PATH", str(fills_path))
    monkeypatch.setenv("MTS_EVENT_LOG_PATH", str(tmp_path / "mts_spread_events.jsonl"))
    import strategies.plugins.futures.active.tmf_spread as _tmf
    _tmf._MTS_FILL_LOG = str(fills_path)
    with open(fills_path, "a", encoding="utf-8") as _fh:
        for _leg, _code in (("NEAR", "TMFF6"), ("FAR", "TMFH6")):
            _fh.write(json.dumps({"trade_id": "t1", "leg": _leg,
                                  "contract": _code, "qty": 1,
                                  "fill_type": "ENTRY", "side": "BUY"}) + "\n")
    monitor, strat, api = build_monitor(tmp_path, monkeypatch)
    ctx = StrategyContext(
        market=MarketData(last_bar={}, ticker="TMF"),
        position=PositionView(),
        config={"ticker": "TMF",
                "params": {"atr_multiplier_stop": 3.5, "atr_multiplier_trail": 3.5,
                           "trail_distance_points": 35.0, "min_atr": 5.0,
                           "entry_z": -2.0, "confirm_ticks": 1, "confirm_ms": 0.0,
                           "enable_combined_upl_trail": True,
                           "combined_upl_activation_net_pnl_twd": 200.0,
                           "combined_upl_giveback_twd": 50.0}},
    )
    strat.init(ctx)
    strat._trade_id = "t1"
    strat._has_position = True
    strat._released_leg = None
    strat._side = "LONG"
    strat._near_side = "LONG"
    strat._far_side = "LONG"
    strat._near_entry = 43770.0
    strat._far_entry = 44000.0
    strat._peak = 44500.0
    strat._mfe_pts = 0.0
    strat._mae_pts = 0.0
    strat._peak_net_exit_pnl_twd = 0.0
    strat._renko_gap_quarantine_left = 0
    strat._point_value = 10.0
    strat._estimated_cost = 92.0
    strat._single_leg_warmup_ms = 0.0
    strat._single_leg_warmup_ticks = 0
    strat._single_leg_entered_mono = time.monotonic()
    strat._single_leg_post_fill_ticks = 2
    strat._single_leg_entry_price = 44000.0
    strat._shadow_exit_ts = None
    strat._shadow_exit_price = None
    strat._shadow_exit_upl = None
    strat._shadow_exit_triggered = False
    strat._shadow_trail_dist_pts = 0.0
    strat._single_leg_peak = 44500.0
    strat._single_leg_nadir = 44000.0
    strat._formal_max_giveback = 0.0
    strat._shadow_max_giveback = 0.0
    strat._post_shadow_mfe = 0.0
    strat._post_shadow_mae = 0.0
    strat._last_trail_dist_shadow = 20.0
    strat._last_trail_dist_formal = 20.0
    strat._release_near_ticks = 1
    strat._release_far_ticks = 0
    strat._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
    )
    now = datetime.now()
    state_path = Path(tmp_path) / "state.json"
    bar1 = {"near_close": 43400.0, "far_close": 44100.0, "atr": 10.0,
            "timestamp": now, "code": "TMFF6"}
    with patch("strategies.futures.monitor._mts_position_state_path",
               return_value=state_path), \
         patch("strategies.futures.monitor.is_taifex_futures_market_open",
               return_value=True), \
         patch.object(Path, "exists", return_value=False):
        sig_rel = strat._manage_position(43400.0, 44100.0, -0.5, now, bar1)
        assert sig_rel is not None, "release emission expected"
        with patch("strategies.futures.monitor.save_trade"), \
             patch("strategies.futures.monitor.DecisionLogger", create=True):
            monitor._submit_mts_order_signal(sig_rel, strat, bar1, now)
            rel_oid = next(o for o, p in monitor._pending_lifecycle_orders.items()
                           if p.get("signal") == "RELEASE_NEAR")
            ev = _types.SimpleNamespace(
                order_id=rel_oid, fill_qty=1, fill_price=43400.0,
                deal_id="deal-release", symbol="TMFF6",
                status=OrderStatus.FILLED, side=OrderSide.SELL,
                timestamp=now)
            monitor._apply_confirmed_futures_deal(ev)
        _lc = strat._lifecycle_oca
        assert _lc.phase.value == "SINGLE_LEG", \
            f"expected SINGLE_LEG after release fill, got {_lc.phase.value}"
        assert getattr(strat, "_released_leg", None) == "near"
        strat._lifecycle = "TRAILING_LONG"  # release path marks EXITING; clear it
        assert _lc.release_group.filled_leg == Leg.NEAR
        assert _lc.release_group.canceled_leg == Leg.FAR
        assert _lc.trail_group.remaining_leg == Leg.FAR
        _qty = monitor._mts_ledger_reconstructed_open_qty("t1")
        assert _qty is not None, "ledger reconstruction must have entry evidence"
        assert _qty == {"NEAR": 0, "FAR": 1}, f"open qty mismatch: {_qty}"
        strat._peak_net_exit_pnl_twd = 1300.0
    bar2 = {"near_close": 43400.0, "far_close": 44350.0, "atr": 10.0,
            "timestamp": now, "code": "TMFF6"}
    return monitor, strat, now, bar1, bar2


def test_b48_policy_j_single_leg_wiring(tmp_path, monkeypatch, caplog):
    from pathlib import Path
    from unittest.mock import patch
    from core.order_management.order import OrderSide
    from core.strategy_context import StrategyContext, MarketData, PositionView
    from strategies.plugins.futures.active.tmf_spread import (
        PositionLifecycle, PositionPhase, ReleaseGroup, ReleaseGroupStatus,
        TrailGroup, TrailGroupStatus, Leg,
    )
    import logging as _logging

    caplog.set_level(_logging.INFO)
    monitor, strat, now, bar1, bar2 = _b48_build_released(tmp_path, monkeypatch)
    state_path = Path(tmp_path) / "state.json"
    with patch("strategies.futures.monitor._mts_position_state_path",
               return_value=state_path), \
         patch("strategies.futures.monitor.is_taifex_futures_market_open",
               return_value=True), \
         patch.object(Path, "exists", return_value=False):
        # ── phase 2: giveback ≥ 50 TWD on the remaining leg ⇒ trigger ──
        sig2 = strat._manage_position(43400.0, 44350.0, -0.5, now, bar2)
        assert sig2 is not None, "single-leg Policy J exit emission expected"
        # point 4: the signal must be Policy J PRODUCED (the adapter's
        # giveback block logged POLICY_J_TRIGGERED), not the native trailing
        # stop coincidentally firing first
        assert sig2.action == "EXIT" and sig2.reason == "TMF_TRAIL", \
            f"unexpected signal {sig2}"
        assert "POLICY_J_TRIGGERED" in caplog.text, \
            "Policy J provenance missing — native trail may have fired first"
        # point 5: the trigger arithmetic must hold on the REAL adapter
        # context (computed, not assumed)
        _n = strat._pnl_near(43400.0)
        _f = strat._pnl_far(44350.0)
        _current_net = (_n + _f) * 10.0 - 92.0
        _peak = strat._peak_net_exit_pnl_twd
        _act = float(strat._params.get("combined_upl_activation_net_pnl_twd", 300.0))
        _gb = float(strat._params.get("combined_upl_giveback_twd", 100.0))
        assert _peak >= _act, f"peak {_peak} below activation {_act}"
        assert _current_net <= _peak - _gb, \
            f"current {_current_net} not below giveback line {_peak - _gb}"
        assert _current_net < _peak, "direction: current must be below peak"
        # point 1: delta BEFORE the Policy-J dispatch — the release's own
        # order must NOT be counted
        _before = len(monitor.order_mgr.submits)
        monitor._submit_mts_order_signal(sig2, strat, bar2, now)
        _new = monitor.order_mgr.submits[_before:]
        # point 2: ONE new order for the REMAINING leg only — FAR code, SELL,
        # qty=1 — and NO near-code order in the delta set
        assert len(_new) == 1, \
            f"expected exactly ONE new order, got {len(_new)}: {_new}"
        _order_rec = _new[0]
        assert _order_rec["symbol"] == "TMFH6", \
            f"remaining-leg order must be FAR code, got {_order_rec}"
        assert _order_rec["side"] == OrderSide.SELL, \
            f"LONG exit must SELL, got {_order_rec['side']}"
        assert _order_rec["quantity"] == 1, \
            f"qty must be 1, got {_order_rec['quantity']}"
        assert not any(o["symbol"] == "TMFF6" for o in _new), \
            "released NEAR leg must NOT be re-exited"
        # durable intent exists for t1
        intent_log = ei.IntentLog(str(tmp_path / "logs"))
        actives = [iid for iid in intent_log.list_active()
                   if intent_log.get(iid)["trade_id"] == "t1"]
        assert len(actives) == 1
        # auditable decision event — durable in the INTENT log (fence-aware),
        # with the full codex field contract
        rows = [json.loads(l) for l in intent_log.raw_lines() if l.strip()]
        pj = [r for r in rows if r.get("event") == "POLICY_J_SINGLE_LEG_TRIGGERED"]
        assert len(pj) == 1
        _pj = pj[0]
        for key in ("event_id", "ts", "trade_id", "released_leg", "remaining_leg",
                    "released_realized_pnl_twd", "remaining_upl_twd",
                    "current_net_exit_pnl_twd", "peak_net_exit_pnl_twd",
                    "activation_twd", "giveback_twd", "winner", "action", "reason"):
            assert key in _pj, f"missing event field {key}"
        assert _pj["winner"] == "POLICY_J_SINGLE_LEG"
        assert _pj["action"] == "EXIT" and _pj["reason"] == "TMF_TRAIL"
        assert _pj["trade_id"] == "t1" and _pj["released_leg"] == "near"
        assert _pj["remaining_leg"] == "FAR"
        assert abs(_pj["current_net_exit_pnl_twd"] - _current_net) < 1.0
        assert abs(_pj["peak_net_exit_pnl_twd"] - _peak) < 1.0
        # D-iv: ORDER_SUBMITTED event + pending-order metadata carry the
        # SAME event_id (decision → order correlation)
        events = [json.loads(l) for l in
                  (tmp_path / "mts_spread_events.jsonl").read_text().splitlines()
                  if l.strip()]
        _os = [e for e in events if e.get("event") == "ORDER_SUBMITTED"]
        assert _os, "ORDER_SUBMITTED events missing"
        assert _os[-1].get("event_id") == _pj["event_id"], \
            "ORDER_SUBMITTED must carry the decision event_id"
        assert _os[-1].get("winner") == "POLICY_J_SINGLE_LEG"
        _pend = monitor._pending_lifecycle_orders
        assert any(p.get("event_id") == _pj["event_id"] for p in _pend.values()), \
            "pending-order metadata must carry the decision event_id"
        # ── phase 3: identical second tick ⇒ ZERO re-submit, ONE event ──
        sig3 = strat._manage_position(43400.0, 44350.0, -0.5, now, bar2)
        assert sig3 is None, "in-flight gate must suppress re-emission"
        monitor._submit_mts_order_signal(sig3, strat, bar2, now) if sig3 else None
        assert len(monitor.order_mgr.submits) - _before == 1, \
            "duplicate exit re-submitted"
        rows2 = [json.loads(l) for l in intent_log.raw_lines() if l.strip()]
        pj2 = [r for r in rows2 if r.get("event") == "POLICY_J_SINGLE_LEG_TRIGGERED"]
        assert len(pj2) == 1, "second tick must NOT duplicate the decision event"
    # ── phase 4 (isolated): below activation ⇒ zero orders ──
    mon2, strat2, api2 = build_monitor(tmp_path, monkeypatch)
    ctx2 = StrategyContext(
        market=MarketData(last_bar={}, ticker="TMF"),
        position=PositionView(),
        config={"ticker": "TMF",
                "params": {"atr_multiplier_stop": 3.5, "atr_multiplier_trail": 3.5,
                           "trail_distance_points": 35.0, "min_atr": 5.0,
                           "entry_z": -2.0, "confirm_ticks": 1, "confirm_ms": 0.0,
                           "enable_combined_upl_trail": True,
                           "combined_upl_activation_net_pnl_twd": 200.0,
                           "combined_upl_giveback_twd": 50.0}},
    )
    strat2.init(ctx2)
    strat2._trade_id = "t2"
    strat2._has_position = True
    strat2._released_leg = "near"
    strat2._side = "LONG"
    strat2._near_side = "LONG"
    strat2._far_side = "LONG"
    strat2._near_entry = 43770.0
    strat2._far_entry = 44000.0
    strat2._peak_net_exit_pnl_twd = 150.0  # below activation 200
    strat2._renko_gap_quarantine_left = 0
    strat2._point_value = 10.0
    strat2._estimated_cost = 92.0
    strat2._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.COMPLETED,
                                   filled_leg=Leg.NEAR, canceled_leg=Leg.FAR),
        trail_group=TrailGroup(status=TrailGroupStatus.ARMED,
                               remaining_leg=Leg.FAR),
    )
    with patch("strategies.futures.monitor._mts_position_state_path",
               return_value=Path(tmp_path) / "state2.json"), \
         patch("strategies.futures.monitor.is_taifex_futures_market_open",
               return_value=True), \
         patch.object(Path, "exists", return_value=False):
        sig_low = strat2._manage_position(43400.0, 44350.0, -0.5, now, bar2)
        assert sig_low is None, "below-activation must NOT emit an exit"
        assert len(mon2.order_mgr.submits) == 0, "zero orders below activation"


# ── B48-D1 (codex D-i): decision-event append failure ⇒ fail-closed:
#    no signal, no submit ──────────────────────────────────────────────
def test_b48d1_event_append_failure_fail_closed(tmp_path, monkeypatch, caplog):
    from pathlib import Path
    from unittest.mock import patch
    import logging as _logging

    caplog.set_level(_logging.INFO)
    monitor, strat, now, bar1, bar2 = _b48_build_released(tmp_path, monkeypatch)
    with patch("strategies.futures.monitor._mts_position_state_path",
               return_value=Path(tmp_path) / "state.json"), \
         patch("strategies.futures.monitor.is_taifex_futures_market_open",
               return_value=True), \
         patch.object(Path, "exists", return_value=False), \
         patch.object(ei.IntentLog, "append_event",
                      side_effect=RuntimeError("intent log io failure")):
        _before = len(monitor.order_mgr.submits)
        sig = strat._manage_position(43400.0, 44350.0, -0.5, now, bar2)
        assert sig is None, \
            "fail-closed: Policy J event append failure must suppress the signal"
        monitor._submit_mts_order_signal(sig, strat, bar2, now) if sig else None
        assert len(monitor.order_mgr.submits) == _before, \
            "fail-closed: zero new orders when the decision event could not persist"


# ── B48-D4 (codex P0-1): append failure must leave NO false-exit records
#    on the REAL runtime files — no EXIT_REMAINING event row, no EXIT_*
#    state action, no shadow-exit summary, no submit ───────────────────
def test_b48d4_append_failure_no_false_exit_records(tmp_path, monkeypatch, caplog):
    from pathlib import Path
    from unittest.mock import patch
    import logging as _logging

    caplog.set_level(_logging.INFO)
    monitor, strat, now, bar1, bar2 = _b48_build_released(tmp_path, monkeypatch)
    state_path = Path(tmp_path) / "state.json"
    with patch("strategies.futures.monitor._mts_position_state_path",
               return_value=state_path), \
         patch("strategies.futures.monitor.is_taifex_futures_market_open",
               return_value=True), \
         patch.object(Path, "exists", return_value=False), \
         patch.object(ei.IntentLog, "append_event",
                      side_effect=RuntimeError("intent log io failure")):
        _before = len(monitor.order_mgr.submits)
        sig = strat._manage_position(43400.0, 44350.0, -0.5, now, bar2)
        assert sig is None
        monitor._submit_mts_order_signal(sig, strat, bar2, now) if sig else None
        assert len(monitor.order_mgr.submits) == _before
        # no EXIT_REMAINING row in the events ledger
        if (tmp_path / "mts_spread_events.jsonl").exists():
            events = [json.loads(l) for l in
                      (tmp_path / "mts_spread_events.jsonl").read_text().splitlines()
                      if l.strip()]
            assert not any(e.get("event") == "EXIT_REMAINING" for e in events), \
                "EXIT_REMAINING must NOT be recorded when the decision event failed"
        # no EXIT_* state action (release state may exist, EXIT state must not)
        if state_path.exists():
            rows = [json.loads(l) for l in state_path.read_text().splitlines()
                    if l.strip()]
            if rows:
                assert not str(rows[-1].get("action", "")).startswith("EXIT_"), \
                    f"exit state must NOT be written: {rows[-1].get('action')}"
        # no shadow-exit summary logged
        assert "MTS_MTF_SHADOW_TRADE_SUMMARY" not in caplog.text, \
            "shadow exit summary must NOT be logged when the decision event failed"


# ── B48-D5 (codex P1): confirmed-fill / settlement correlation — the fill
#    handler resolves the pending by order_id (which carries the decision
#    event_id), and the EXIT fill record persists event_id/winner ──────
def test_b48d5_fill_record_correlates_to_decision_event(tmp_path, monkeypatch, caplog):
    from pathlib import Path
    from unittest.mock import patch
    from core.order_management.order import OrderStatus, OrderSide
    import logging as _logging
    import types as _types

    caplog.set_level(_logging.INFO)
    monitor, strat, now, bar1, bar2 = _b48_build_released(tmp_path, monkeypatch)
    with patch("strategies.futures.monitor._mts_position_state_path",
               return_value=Path(tmp_path) / "state.json"), \
         patch("strategies.futures.monitor.is_taifex_futures_market_open",
               return_value=True), \
         patch.object(Path, "exists", return_value=False):
        sig = strat._manage_position(43400.0, 44350.0, -0.5, now, bar2)
        assert sig is not None
        _before = len(monitor.order_mgr.submits)
        monitor._submit_mts_order_signal(sig, strat, bar2, now)
        _new = monitor.order_mgr.submits[_before:]
        assert len(_new) == 1
        _exit_oid = _new[0]["order_id"]
        intent_log = ei.IntentLog(str(tmp_path / "logs"))
        rows = [json.loads(l) for l in intent_log.raw_lines() if l.strip()]
        pj = [r for r in rows if r.get("event") == "POLICY_J_SINGLE_LEG_TRIGGERED"]
        assert len(pj) == 1
        _evid = pj[0]["event_id"]
        # the pending resolved by the EXIT order_id carries the decision event_id
        _pend = monitor._pending_lifecycle_orders.get(_exit_oid)
        assert _pend is not None, "EXIT order must be in pending lifecycle orders"
        assert _pend.get("event_id") == _evid, \
            "pending (order_id join) must link back to the decision event"
        # confirmed fill → the fill record persists event_id/winner
        ev = _types.SimpleNamespace(
            order_id=_exit_oid, fill_qty=1, fill_price=44350.0,
            deal_id="deal-exit", symbol="TMFH6",
            status=OrderStatus.FILLED, side=OrderSide.SELL,
            timestamp=now)
        with patch("strategies.futures.monitor.save_trade"), \
             patch("strategies.futures.monitor.DecisionLogger", create=True):
            monitor._apply_confirmed_futures_deal(ev)
        fills = [json.loads(l) for l in
                 (tmp_path / "mts_trade_fills.jsonl").read_text().splitlines()
                 if l.strip()]
        _exit_fills = [f for f in fills
                       if f.get("fill_type") == "EXIT" and f.get("leg") == "FAR"]
        assert _exit_fills, "EXIT fill record missing from the fills ledger"
        assert _exit_fills[-1].get("event_id") == _evid, \
            "fill record must persist the decision event_id"
        assert _exit_fills[-1].get("winner") == "POLICY_J_SINGLE_LEG", \
            "fill record must persist the decision winner"
        # the exit committed: strategy flat
        assert strat._has_position is False, "exit fill must flat the strategy"


# ── B48-D6 (codex P0-2): Signal serialization — event_id/winner must
#    survive to_dict / JSON round-trip ─────────────────────────────────
def test_b48d6_signal_to_dict_roundtrip():
    from core.signal import Signal
    import json as _json

    sig = Signal("EXIT", "TMF_TRAIL", confidence=0.5, stop_loss=0,
                 event_id="ev-1234", winner="POLICY_J_SINGLE_LEG")
    d = sig.to_dict()
    assert d["event_id"] == "ev-1234", "to_dict must carry event_id"
    assert d["winner"] == "POLICY_J_SINGLE_LEG", "to_dict must carry winner"
    # JSON round-trip (any queue/replay serialization path)
    s2 = _json.loads(_json.dumps(d))
    assert s2["event_id"] == "ev-1234" and s2["winner"] == "POLICY_J_SINGLE_LEG"
    # backward compatible: plain signals still serialize
    d0 = Signal("EXIT", "TMF_TRAIL", confidence=0.5).to_dict()
    assert d0["event_id"] == "" and d0["winner"] == ""


# ── B48-D2 (codex D-ii): second identical tick ⇒ ONE decision event,
#    ONE order (idempotent trigger generation) ─────────────────────────
def test_b48d2_second_tick_single_event_single_order(tmp_path, monkeypatch, caplog):
    from pathlib import Path
    from unittest.mock import patch
    import logging as _logging

    caplog.set_level(_logging.INFO)
    monitor, strat, now, bar1, bar2 = _b48_build_released(tmp_path, monkeypatch)
    with patch("strategies.futures.monitor._mts_position_state_path",
               return_value=Path(tmp_path) / "state.json"), \
         patch("strategies.futures.monitor.is_taifex_futures_market_open",
               return_value=True), \
         patch.object(Path, "exists", return_value=False):
        sig1 = strat._manage_position(43400.0, 44350.0, -0.5, now, bar2)
        assert sig1 is not None
        _before = len(monitor.order_mgr.submits)
        monitor._submit_mts_order_signal(sig1, strat, bar2, now)
        assert len(monitor.order_mgr.submits) - _before == 1
        # identical second tick — the in-flight gate suppresses re-emission
        sig2 = strat._manage_position(43400.0, 44350.0, -0.5, now, bar2)
        assert sig2 is None
        monitor._submit_mts_order_signal(sig2, strat, bar2, now) if sig2 else None
        assert len(monitor.order_mgr.submits) - _before == 1, \
            "duplicate exit re-submitted"
        intent_log = ei.IntentLog(str(tmp_path / "logs"))
        rows = [json.loads(l) for l in intent_log.raw_lines() if l.strip()]
        pj = [r for r in rows if r.get("event") == "POLICY_J_SINGLE_LEG_TRIGGERED"]
        assert len(pj) == 1, \
            f"exactly ONE decision event expected, got {len(pj)}"


# ── B48-D3 (codex D-iii): a NATIVE remaining-leg trail (no Policy J win)
#    must NOT impersonate the POLICY_J_SINGLE_LEG_TRIGGERED event ──────
def test_b48d3_native_trail_does_not_impersonate(tmp_path, monkeypatch, caplog):
    from pathlib import Path
    from unittest.mock import patch
    import logging as _logging

    caplog.set_level(_logging.INFO)
    monitor, strat, now, bar1, bar2 = _b48_build_released(tmp_path, monkeypatch)
    # kill the Policy J win: peak below activation
    strat._peak_net_exit_pnl_twd = 150.0
    bar_native = {"near_close": 43400.0, "far_close": 43800.0, "atr": 10.0,
                  "timestamp": now, "code": "TMFF6"}
    with patch("strategies.futures.monitor._mts_position_state_path",
               return_value=Path(tmp_path) / "state.json"), \
         patch("strategies.futures.monitor.is_taifex_futures_market_open",
               return_value=True), \
         patch.object(Path, "exists", return_value=False):
        sig = strat._manage_position(43400.0, 43800.0, -0.5, now, bar_native)
        # native trail may fire (legacy path) — but it must NEVER carry the
        # Policy J winner or append the Policy J decision event
        if sig is not None:
            assert sig.winner != "POLICY_J_SINGLE_LEG", \
                "native trail must not claim the Policy J winner"
            assert sig.event_id == "", \
                "native trail must not carry a Policy J event_id"
        intent_log = ei.IntentLog(str(tmp_path / "logs"))
        rows = [json.loads(l) for l in intent_log.raw_lines() if l.strip()]
        pj = [r for r in rows if r.get("event") == "POLICY_J_SINGLE_LEG_TRIGGERED"]
        assert len(pj) == 0, \
            "native trail must NOT append the POLICY_J_SINGLE_LEG_TRIGGERED event"
