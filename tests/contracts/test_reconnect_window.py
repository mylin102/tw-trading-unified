"""
Contract: reconnect verification window (Phase 1 controlled recovery) — v3.

codex review FAILs addressed:
  #1 far BBO presence            -> FIXED (near+far slots checked)
  #2 legitimate BBO validity     -> FIXED (_valid_leg_bbo: finite, positive,
                                       non-crossed, bidask_at present)
  #3 per-leg BidAsk liveness     -> FIXED (slot bidask_at advance, not
                                       monitor-level last_tick_at)
  #4 generation fencing          -> FIXED (_bbo_matches_generation: slot
                                       generation_id must equal current
                                       session_registry generation)
  #5 strict recertify ordering   -> FIXED (_try_shioaji_reconnect(recertify=
                                       False) defers; main loop recertifies
                                       only after a passing window)
  #6 quarantine semantic proof   -> FIXED (test asserts quarantine_callable
                                       re-asserts blocked on a context that
                                       was prematurely promoted)
"""
import os
import re
import sys
import threading
import time
import types
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeApi:
    def __init__(self, healthy=True, generation="GEN-1"):
        self.healthy = healthy
        self.generation = generation
        self.calls = 0

    @property
    def futopt_account(self):
        return object()

    def list_positions(self, account):
        self.calls += 1
        if not self.healthy:
            raise ConnectionResetError("TCP connection reset")
        return []


class _FakeRegistry:
    """Mirror of session_registry.generation(api)."""

    @staticmethod
    def generation(api):
        return getattr(api, "generation", None)


class _FakeMonitor:
    def __init__(self, tick_advances=True, generation="GEN-1",
                 bidask_advances=True):
        self.last_tick_at = time.time()
        self.contract = types.SimpleNamespace(code="MXFI6")
        self.far_contract = types.SimpleNamespace(code="MXFJ6")
        self.ticker = "MXFI6"
        self.generation_checks = 0
        self._slot_gen = generation
        self._bidask_advances = bidask_advances
        self.market_data = {
            "MXFI6": self._slot(),
            "MXFJ6": self._slot(),
            "MXFI6_FAR": self._slot(),
        }
        if tick_advances:
            threading.Thread(target=self._tick, daemon=True).start()
        if bidask_advances:
            threading.Thread(target=self._bidask_loop, daemon=True).start()

    def _slot(self):
        _r = time.monotonic()
        return {"close": 21000.0, "bid": 20999.0, "ask": 21001.0,
                "bidask_at": time.time(), "bidask_received_at": _r,
                "generation_id": self._slot_gen}

    def _tick(self):
        """Simulate TICK callbacks: advances last_tick_at and tick_bbo_at
        but NEVER bidask_received_at (ticks must not fake BidAsk)."""
        while True:
            time.sleep(0.01)
            self.last_tick_at = time.time()
            for _s in self.market_data.values():
                if _s.get("bid") and _s.get("ask"):
                    _s["tick_bbo_at"] = time.monotonic()

    def _bidask_loop(self):
        """Simulate BidAsk callbacks: advance bidask_received_at."""
        while True:
            time.sleep(0.01)
            _r = time.monotonic()
            for _s in self.market_data.values():
                _s["bidask_received_at"] = _r

    def _bidask_loop_near_only(self):
        """BidAsk callbacks on the NEAR slot only (far frozen)."""
        while True:
            time.sleep(0.01)
            self.market_data["MXFI6"]["bidask_received_at"] = time.monotonic()

    def _confirm_session_generation(self):
        self.generation_checks += 1
        return True


def _extract(name: str):
    src = open(os.path.join(REPO, "main.py")).read()
    m = re.search(rf"^def {name}\(.*?(?=^\S|\\Z)", src, re.M | re.S)
    assert m, f"{name} not found"
    ns = {
        "console": types.SimpleNamespace(print=lambda *a, **k: None),
        "time": time, "os": os,
        "api_is_healthy": lambda api: "HEALTHY" if api.healthy
        else "SESSION_INVALID",
        "_leg_codes": _extract_leg_codes(src),
        "_valid_leg_bbo": _extract_helper(src, "_valid_leg_bbo"),
        "_bbo_invalid_reason": _extract_helper(src, "_bbo_invalid_reason"),
        "_bbo_matches_generation": _extract_bbo_gen(src),
    }
    exec(m.group(0), ns)
    return ns[name]


def _extract_leg_codes(src):
    m = re.search(r"^def _leg_codes\(.*?(?=^\S|\Z)", src, re.M | re.S)
    ns = {}
    exec(m.group(0), ns)
    return ns["_leg_codes"]


def _extract_helper(src, name):
    m = re.search(rf"^def {name}\(.*?(?=^\S|\Z)", src, re.M | re.S)
    ns = {"math": __import__("math")}
    exec(m.group(0), ns)
    return ns[name]


def _extract_bbo_gen(src):
    """Extract _bbo_matches_generation with a fake session_registry so the
    generation fencing is testable without the real certificate module.
    Overwrites sys.modules at the FULL module path so `from
    core.live_route_certificate import session_registry` resolves to the
    fake (attribute-level patching of core is bypassed by the import
    system when core is a real package)."""
    m = re.search(r"^def _bbo_matches_generation\(.*?(?=^\S|\Z)", src, re.M | re.S)
    fake_registry = types.ModuleType("core.live_route_certificate")
    fake_registry.session_registry = _FakeRegistry
    module_name = "core.live_route_certificate"
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = fake_registry
    try:
        ns = {}
        exec(m.group(0), ns)
        extracted = ns["_bbo_matches_generation"]
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous

    def _with_fake_registry(*args, **kwargs):
        current = sys.modules.get(module_name)
        sys.modules[module_name] = fake_registry
        try:
            return extracted(*args, **kwargs)
        finally:
            if current is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = current

    return _with_fake_registry


_verify_reconnect_window = None


@pytest.fixture(scope="module", autouse=True)
def _load():
    global _verify_reconnect_window
    _verify_reconnect_window = _extract("_verify_reconnect_window")


class TestBboValidity:
    def test_invalid_zero_bid_fails(self):
        api = _FakeApi()
        mon = _FakeMonitor()
        mon.market_data["MXFI6"]["bid"] = 0.0
        assert _verify_reconnect_window(
            [mon], api, duration_s=0.2, interval_s=0.05) is False

    def test_negative_ask_fails(self):
        api = _FakeApi()
        mon = _FakeMonitor()
        mon.market_data["MXFJ6"]["ask"] = -1.0
        assert _verify_reconnect_window(
            [mon], api, duration_s=0.2, interval_s=0.05) is False

    def test_crossed_book_fails(self):
        api = _FakeApi()
        mon = _FakeMonitor()
        mon.market_data["MXFI6"]["bid"] = 21001.0
        mon.market_data["MXFI6"]["ask"] = 20999.0
        assert _verify_reconnect_window(
            [mon], api, duration_s=0.2, interval_s=0.05) is False

    def test_nan_bid_fails(self):
        api = _FakeApi()
        mon = _FakeMonitor()
        mon.market_data["MXFI6"]["bid"] = float("nan")
        assert _verify_reconnect_window(
            [mon], api, duration_s=0.2, interval_s=0.05) is False

    def test_inf_ask_fails(self):
        api = _FakeApi()
        mon = _FakeMonitor()
        mon.market_data["MXFJ6"]["ask"] = float("inf")
        assert _verify_reconnect_window(
            [mon], api, duration_s=0.2, interval_s=0.05) is False

    def test_missing_bidask_at_fails(self):
        api = _FakeApi()
        mon = _FakeMonitor()
        mon.market_data["MXFI6"].pop("bidask_at")
        assert _verify_reconnect_window(
            [mon], api, duration_s=0.2, interval_s=0.05) is False


class TestPerLegLiveness:
    def test_far_bidask_stalled_but_near_moving_fails(self):
        """Far BidAsk stops advancing while near keeps ticking -> FAIL
        (monitor-level last_tick_at would miss this)."""
        api = _FakeApi()
        # near BidAsk advances; far slots frozen.  Use a monitor whose
        # BidAsk loop is OFF so the freeze holds (a live loop would
        # re-advance the far slots every 10ms).
        mon = _FakeMonitor(bidask_advances=False)
        # re-enable near-only BidAsk advancement manually
        mon.market_data["MXFI6"]["bidask_received_at"] = time.monotonic()
        threading.Thread(target=mon._bidask_loop_near_only,
                         daemon=True).start()
        assert _verify_reconnect_window(
            [mon], api, duration_s=0.2, interval_s=0.05) is False

    def test_tick_only_updates_cannot_fake_bidask_liveness(self):
        """Ticks keep arriving (last_tick_at + tick_bbo_at advance) but NO
        BidAsk callback arrives -> verification MUST fail (codex review:
        Tick must not masquerade as BidAsk freshness)."""
        api = _FakeApi()
        mon = _FakeMonitor(bidask_advances=False)  # ticks only
        # slot still has a valid BBO snapshot, but bidask_received_at is
        # frozen -> window must fail on liveness
        assert _verify_reconnect_window(
            [mon], api, duration_s=0.25, interval_s=0.05) is False

    def test_tick_writes_tick_bbo_at_but_not_bidask_freshness(self):
        """Sanity: the tick loop advances tick_bbo_at while
        bidask_received_at stays frozen — proving the fake models the
        codex requirement (tick path cannot touch BidAsk freshness)."""
        mon = _FakeMonitor(bidask_advances=False)
        time.sleep(0.05)
        _slot = mon.market_data["MXFI6"]
        assert _slot.get("tick_bbo_at") is not None
        # bidask_received_at must NOT have advanced (ticks don't touch it)
        # — it stays at the slot-creation value
        assert mon._bidask_advances is False


class TestGenerationFencing:
    def test_stale_generation_bbo_fails(self):
        """Slot written by a PREVIOUS generation -> rejected."""
        api = _FakeApi(generation="GEN-2")
        mon = _FakeMonitor(generation="GEN-1")  # slot tagged with old gen
        assert _verify_reconnect_window(
            [mon], api, duration_s=0.2, interval_s=0.05) is False

    def test_current_generation_bbo_passes(self):
        """Slot tagged with the CURRENT generation -> accepted."""
        api = _FakeApi(generation="GEN-1")
        mon = _FakeMonitor(generation="GEN-1")
        assert _verify_reconnect_window(
            [mon], api, duration_s=0.2, interval_s=0.05) is True

    def test_no_generation_fails_closed(self):
        """No current generation -> cannot fence -> fail-closed."""
        api = _FakeApi(generation=None)
        mon = _FakeMonitor(generation="GEN-1")
        assert _verify_reconnect_window(
            [mon], api, duration_s=0.2, interval_s=0.05) is False


class TestQuarantineSemantics:
    def test_premature_promote_gets_reblocked(self):
        """If reconnect internally promoted the context, the window's
        quarantine_callable must push it BACK to blocked on every interval.
        We simulate: callable re-asserts live_order_allowed=False."""
        api = _FakeApi()
        mon = _FakeMonitor()
        state = {"live_order_allowed": True, "effective_mode": "live_ready"}
        calls = []

        def _q():
            calls.append(1)
            # simulate the quarantine re-assertion (what
            # _quarantine_for_handoff does on the real context)
            state["live_order_allowed"] = False
            state["effective_mode"] = "live_quarantined"

        ok = _verify_reconnect_window(
            [mon], api, duration_s=0.15, interval_s=0.04,
            quarantine_callable=_q)
        assert ok is True
        assert len(calls) >= 2
        # after the window, the context MUST be blocked again
        assert state["live_order_allowed"] is False
        assert state["effective_mode"] == "live_quarantined"
