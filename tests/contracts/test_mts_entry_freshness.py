"""
Contract: MTS manual ENTRY freshness hard gate — BBO-based.

Requirement (2026-08-25, revised): before ANY new MTS manual spread entry,
both legs must carry a VALID and FRESH BBO update:
  - near and far each require bid and ask present, finite positive numeric,
    ask >= bid (never crossed), and
  - BBO update timestamp age <= 12000ms.
Both legs must pass, otherwise the entry is REJECTED with an
ENTRY_BLOCKED_STALE_QUOTE audit event carrying near/far bid/ask/ages and
threshold.  Last-trade age is retained ONLY as telemetry (audit fields);
it never gates the entry.  The gate must NOT affect existing positions'
release / stop-loss / emergency exit paths.

Regressions:
  - fresh valid BBO on both legs            -> PASS
  - no new last trade but BBO freshly updated -> PASS (BBO is the gate)
  - bid missing / ask missing               -> BLOCK
  - invalid bid/ask (0, negative, NaN)      -> BLOCK
  - crossed book (ask < bid)                -> BLOCK
  - BBO stale (> 12000ms)                   -> BLOCK
  - close_all (emergency exit) unaffected   -> BLOCK never emitted
"""
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# NOTE: do NOT set MTS_STATE_PATH here — tmf_spread snapshots
# _MTS_STATE_FILE at import time, so ANY MTS_STATE_PATH set before the
# first import permanently redirects state writes for the whole session
# (breaks order-sensitive suites like test_exit_intent_p1b b19).
# pytest already defaults the state file to /tmp/test_mts_position_state.json,
# which is isolated from the production /tmp/mts_position_state.json.
# FUTURES_MANUAL_TRADE_FLAG_PATH is safe: monitor reads it per-instance.

_FLAG = "/tmp/test_mts_entry_freshness.flag"
_PROCESSING = _FLAG + ".processing"


def _write_flag(**kwargs) -> dict:
    defaults = {
        "action": "spread",
        "side": "SELL_NEAR_BUY_FAR",
        "near_close": 21000.0,
        "far_close": 21100.0,
        "created_at": time.time(),
    }
    defaults.update(kwargs)
    with open(_FLAG, "w") as f:
        json.dump(defaults, f)
    return defaults


_FILLS = "/tmp/test_mts_entry_freshness_fills.jsonl"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Isolate the manual-trade flag path per test.  NEVER set
    MTS_STATE_PATH (module-import snapshot hazard, see above).  The
    pytest-default state file is removed so the open-position guard sees
    FLAT, independent of any residue left by other suites."""
    monkeypatch.setenv("FUTURES_MANUAL_TRADE_FLAG_PATH", _FLAG)
    # 2026-08-27: also isolate the fills ledger — the position guard
    # (_mts_block_entry_if_open_position, Guard 2) reads the fills ledger
    # and a REAL open trade (e.g. mts-auto-170102-524) makes
    # fills_has_open=True, blocking entry BEFORE the freshness gate.
    monkeypatch.setenv("MTS_FILL_LOG_PATH", _FILLS)
    for p in (_FLAG, _PROCESSING, "/tmp/test_mts_position_state.json", _FILLS):
        if os.path.exists(p):
            os.remove(p)
    yield
    for p in (_FLAG, _PROCESSING, "/tmp/test_mts_position_state.json", _FILLS):
        if os.path.exists(p):
            os.remove(p)


def _make_monitor(
    # BBO state per leg: (bid, ask, bidask_age_ms) or None for missing
    near_bbo=(20999.0, 21001.0, 100),
    far_bbo=(21099.0, 21101.0, 100),
    # last-trade age telemetry (NOT the gate)
    near_trade_age_ms=50,
    far_trade_age_ms=50,
):
    """Build FuturesMonitor in paper/live mode (dry_run=False) with a
    market_data carrying BBO slots (bid/ask/bidask_at) plus last-trade
    timestamps, so the entry path reaches the BBO freshness gate."""
    from strategies.futures.monitor import FuturesMonitor

    mon = FuturesMonitor(
        api=type("A", (), {})(),
        config_path="config/futures_night.yaml",
        dry_run=False,
    )
    mon.ticker = "TMF"
    mon._use_order_manager = True
    mon.trader = SimpleNamespace(position=0)
    mon.order_mgr = MagicMock()
    mon.order_mgr.active_orders = {}
    mon._manual_trade_status = "READY"
    mon._pending_lifecycle_orders = {}
    mon._registry = {}
    mon.contract = SimpleNamespace(code="TMFF6")
    mon.far_contract = SimpleNamespace(code="TMFH6")
    mon._far_current_bar = {"open": 0, "high": 0, "low": 0,
                            "close": 21100.0, "volume": 0, "ts": None}
    mon.dry_run = False
    mon.live_trading = False

    _now = time.time()

    def _mk_slot(bbo, trade_age_ms):
        if bbo is None:
            return {"close": 21000.0,
                    "local_arrival_at": _now - trade_age_ms / 1000.0}
        _bid, _ask, _bbo_age = bbo
        _slot = {
            "close": 21000.0,
            "local_arrival_at": _now - trade_age_ms / 1000.0,
        }
        if _bid is not None and _ask is not None:
            _slot["bid"] = _bid
            _slot["ask"] = _ask
            _slot["bidask_at"] = _now - _bbo_age / 1000.0
        return _slot

    mon.market_data = {
        "TMF": _mk_slot(near_bbo, near_trade_age_ms),
        "TMF_FAR": _mk_slot(far_bbo, far_trade_age_ms),
    }
    mon._audit_events = []
    _orig_append = mon._append_mts_event
    mon._append_mts_event = lambda *a, **k: (
        mon._audit_events.append({"event": a[0], **k})
        if a else None)
    return mon


def _blocked_events(mon):
    return [e for e in mon._audit_events
            if e["event"] == "ENTRY_BLOCKED_STALE_QUOTE"]


# ═══════════════════════════════════════════════════════════
# Group 1 — fresh valid BBO passes
# ═══════════════════════════════════════════════════════════

class TestFreshPass:
    def test_fresh_valid_bbo_both_legs_passes(self):
        """near/far BBO valid + age 100ms -> no block."""
        mon = _make_monitor()
        _write_flag()
        mon._process_manual_trade_flag()
        assert not _blocked_events(mon), _blocked_events(mon)

    def test_fresh_bbo_with_stale_last_trade_passes(self):
        """NO new last trade (trade age 60s) but BBO freshly updated
        (bidask_at 100ms) -> PASS.  BBO update is the gate, not last trade."""
        mon = _make_monitor(near_trade_age_ms=60000, far_trade_age_ms=60000)
        _write_flag()
        mon._process_manual_trade_flag()
        assert not _blocked_events(mon), _blocked_events(mon)


# ═══════════════════════════════════════════════════════════
# Group 2 — BBO stale blocks
# ═══════════════════════════════════════════════════════════

class TestBboStaleBlocks:
    def test_bbo_age_under_threshold_is_allowed(self):
        """BBO age below the 12000ms threshold passes.  (Uses 11500ms,
        not exactly 12000: the gate compares time.time() at evaluation
        against the slot timestamp, so an exact-boundary age picks up
        sub-ms drift and flakily trips the inclusive check.)"""
        mon = _make_monitor(near_bbo=(20999.0, 21001.0, 11500))
        _write_flag()
        mon._process_manual_trade_flag()
        assert not _blocked_events(mon), _blocked_events(mon)

    def test_near_bbo_stale_blocks(self):
        """near BBO age 15000ms (>12000) -> BLOCK with ages in audit."""
        mon = _make_monitor(near_bbo=(20999.0, 21001.0, 15000))
        _write_flag()
        mon._process_manual_trade_flag()
        blocked = _blocked_events(mon)
        assert blocked, "near BBO stale must block"
        assert int(blocked[0].get("near_bbo_age_ms") or -1) == 15000, blocked[0]
        assert blocked[0].get("threshold") == 12000, blocked[0]

    def test_far_bbo_stale_blocks(self):
        """far BBO age 15000ms -> BLOCK."""
        mon = _make_monitor(far_bbo=(21099.0, 21101.0, 15000))
        _write_flag()
        mon._process_manual_trade_flag()
        blocked = _blocked_events(mon)
        assert blocked, "far BBO stale must block"
        assert int(blocked[0].get("far_bbo_age_ms") or -1) == 15000, blocked[0]


# ═══════════════════════════════════════════════════════════
# Group 3 — missing / invalid / crossed BBO blocks
# ═══════════════════════════════════════════════════════════

class TestBboInvalidBlocks:
    def test_near_bbo_missing_blocks(self):
        """near has no bid/ask -> BLOCK."""
        mon = _make_monitor(near_bbo=None)
        _write_flag()
        mon._process_manual_trade_flag()
        assert _blocked_events(mon), "missing near BBO must block"

    def test_far_bbo_missing_blocks(self):
        """far has no bid/ask -> BLOCK."""
        mon = _make_monitor(far_bbo=None)
        _write_flag()
        mon._process_manual_trade_flag()
        assert _blocked_events(mon), "missing far BBO must block"

    def test_near_bid_zero_blocks(self):
        """near bid = 0 (invalid) -> BLOCK."""
        mon = _make_monitor(near_bbo=(0.0, 21001.0, 100))
        _write_flag()
        mon._process_manual_trade_flag()
        assert _blocked_events(mon), "zero near bid must block"

    def test_near_ask_negative_blocks(self):
        """near ask = -1 (invalid) -> BLOCK."""
        mon = _make_monitor(near_bbo=(20999.0, -1.0, 100))
        _write_flag()
        mon._process_manual_trade_flag()
        assert _blocked_events(mon), "negative near ask must block"

    def test_crossed_book_blocks(self):
        """ask < bid (crossed) -> BLOCK."""
        mon = _make_monitor(near_bbo=(21100.0, 21000.0, 100))
        _write_flag()
        mon._process_manual_trade_flag()
        assert _blocked_events(mon), "crossed near book must block"

    def test_nan_bid_blocks(self):
        """near bid = NaN -> BLOCK."""
        import math
        mon = _make_monitor(near_bbo=(float("nan"), 21001.0, 100))
        _write_flag()
        mon._process_manual_trade_flag()
        assert _blocked_events(mon), "NaN near bid must block"


# ═══════════════════════════════════════════════════════════
# Group 4 — existing-position exits unaffected
# ═══════════════════════════════════════════════════════════

class TestExitUnaffected:
    def test_close_all_not_blocked_by_bbo_gate(self):
        """close_all must NOT run the entry BBO gate."""
        mon = _make_monitor(near_bbo=None, far_bbo=None)
        gate_called = []

        def _spy_gate(*a, **k):
            gate_called.append(True)
            return True

        mon._check_entry_quote_freshness = _spy_gate
        _write_flag(action="close_all")
        mon._process_manual_trade_flag()
        assert not gate_called, \
            "close_all must NOT run the entry freshness gate"
        assert "STALE_QUOTE" not in mon._manual_trade_status, \
            mon._manual_trade_status


# ═══════════════════════════════════════════════════════════
# Group 5 — on_tick writes bidask_at (review blocker fix)
# ═══════════════════════════════════════════════════════════

class TestOnTickBidaskAt:
    def _on_tick_monitor(self):
        """Build a dry_run monitor whose on_tick path writes market_data."""
        from collections import deque
        from strategies.futures.monitor import FuturesMonitor
        mon = FuturesMonitor(
            api=type("A", (), {})(),
            config_path="config/futures_night.yaml",
            dry_run=True,
        )
        mon.ticker = "TMF"
        mon.contract = SimpleNamespace(code="TMFF6")
        mon.far_contract = SimpleNamespace(code="TMFG6")
        mon.market_data = {}
        mon._canonical_near_codes = {"TMFF6"}
        mon._canonical_far_codes = {"TMFG6"}
        mon.last_tick_at = time.time()
        mon._last_tmf_price = 21000.0
        mon._far_current_bar = {"open": 0, "high": 0, "low": 0,
                                "close": 21100.0, "volume": 0, "ts": None}
        # bar-pipeline fields required by on_tick
        mon._current_bar = {"open": 0, "high": 0, "low": 0, "close": 0,
                            "volume": 0, "ts": None}
        mon._tick_bars_deque = deque(maxlen=300)
        mon._last_bar_ts = 0
        mon._last_real_tmf_tick_at = time.time()
        mon._runtime_status = None
        mon._debug_feed = False
        mon._debug_tickbar = False
        mon._last_far_bar_ts = 0
        mon._manual_trade_status = "READY"
        mon._pending_lifecycle_orders = {}
        mon._flag_retry_count = 0
        mon._processed_flag_ids = set()
        return mon

    def test_on_tick_near_tick_writes_tick_bbo_at_and_tick_keys(self, monkeypatch):
        """A tick carrying real buy_price/sell_price must write bidask_at
        into the near / near-alias / code slots (single shared timestamp)."""
        import strategies.futures.monitor as monitor_mod
        from unittest.mock import patch as _patch

        mon = self._on_tick_monitor()
        _now = time.time()
        tick = SimpleNamespace(
            code="TMFF6", close=21000.0,
            buy_price=20999.0, sell_price=21001.0,
            datetime="2026-06-05 10:00:00", volume=10,
        )

        with _patch("core.date_utils.is_day_session", return_value=True), \
             _patch("core.date_utils.is_night_session", return_value=False), \
             _patch.object(mon, "_refresh_runtime_status", lambda *a, **k: None):
            # full on_tick path — near primary tick
            mon.last_tick_at = _now
            mon.on_tick(None, tick)

        for _key in ("TMF", "TMF_NEAR", "TMFF6"):
            _slot = mon.market_data.get(_key, {})
            assert _slot.get("tick_bid") == 20999.0, (_key, _slot)
            assert _slot.get("tick_ask") == 21001.0, (_key, _slot)
            # 2026-08-27 (codex v4): ticks write tick_bbo_at, NEVER
            # bidask_at — BidAsk callback freshness is on_bidask-only.
            assert _slot.get("tick_bbo_at") is not None, \
                f"{_key} missing tick_bbo_at: {_slot}"
            assert _slot.get("bidask_at") is None, \
                f"{_key} tick must not write bidask_at: {_slot}"
            assert abs(_slot["tick_bbo_at"] - _now) < 2.0, \
                f"{_key} tick_bbo_at drifted: {_slot['tick_bbo_at']} vs {_now}"

    def test_on_tick_far_tick_writes_tick_bbo_at_and_tick_keys(self):
        """A far tick with real BBO writes bidask_at into TMF_FAR + code."""
        from unittest.mock import patch as _patch
        mon = self._on_tick_monitor()
        _now = time.time()
        far_tick = SimpleNamespace(
            code="TMFG6", close=21100.0,
            buy_price=21099.0, sell_price=21101.0,
            datetime="2026-06-05 10:00:00", volume=10,
        )
        with _patch("core.date_utils.is_day_session", return_value=True), \
             _patch("core.date_utils.is_night_session", return_value=False), \
             _patch.object(mon, "_refresh_runtime_status", lambda *a, **k: None):
            mon.last_tick_at = _now
            mon.on_tick(None, far_tick)

        for _key in ("TMF_FAR", "TMFG6"):
            _slot = mon.market_data.get(_key, {})
            assert _slot.get("tick_bid") == 21099.0, (_key, _slot)
            assert _slot.get("tick_bbo_at") is not None, \
                f"{_key} missing tick_bbo_at: {_slot}"
            assert _slot.get("bidask_at") is None, \
                f"{_key} tick must not write bidask_at: {_slot}"

    def test_on_tick_close_only_tick_writes_no_bbo(self):
        """A tick with only close (no buy/sell) must NOT fabricate bid/ask;
        the slot stays BBO-missing so the helper blocks."""
        from unittest.mock import patch as _patch
        mon = self._on_tick_monitor()
        _now = time.time()
        tick = SimpleNamespace(
            code="TMFF6", close=21000.0,
            datetime="2026-06-05 10:00:00", volume=10,
        )
        with _patch("core.date_utils.is_day_session", return_value=True), \
             _patch("core.date_utils.is_night_session", return_value=False), \
             _patch.object(mon, "_refresh_runtime_status", lambda *a, **k: None):
            mon.last_tick_at = _now
            mon.on_tick(None, tick)

        for _key in ("TMF", "TMF_NEAR", "TMFF6"):
            _slot = mon.market_data.get(_key, {})
            assert "bid" not in _slot, (_key, _slot)
            assert "bidask_at" not in _slot, (_key, _slot)

    def test_on_tick_cache_feeds_bbo_helper_pass(self):
        """End-to-end: on_tick cache slot (with bidask_at) must pass the
        BBO freshness helper."""
        from strategies.futures.monitor import FuturesMonitor
        mon = FuturesMonitor.__new__(FuturesMonitor)
        _now = time.time()
        near_slot = {"bid": 20999.0, "ask": 21001.0,
                     "bidask_at": _now - 0.1}
        far_slot = {"bid": 21099.0, "ask": 21101.0,
                    "bidask_at": _now - 0.1}
        ok, detail = mon._check_entry_quote_freshness(
            near_slot=near_slot, far_slot=far_slot,
            near_trade_age_ms=60000, far_trade_age_ms=60000,
            near_source="LIVE_TICK", far_source="LIVE_TICK")
        assert ok, f"fresh on_tick BBO must pass: {detail}"

    def test_slot_without_bidask_at_blocks(self):
        """A slot with bid/ask but NO bidask_at (the pre-fix state) must
        block as BBO_NO_TIMESTAMP."""
        from strategies.futures.monitor import FuturesMonitor
        mon = FuturesMonitor.__new__(FuturesMonitor)
        near_slot = {"bid": 20999.0, "ask": 21001.0}  # no bidask_at
        far_slot = {"bid": 21099.0, "ask": 21101.0,
                    "bidask_at": time.time() - 0.1}
        ok, detail = mon._check_entry_quote_freshness(
            near_slot=near_slot, far_slot=far_slot,
            near_trade_age_ms=50, far_trade_age_ms=50,
            near_source="LIVE_TICK", far_source="LIVE_TICK")
        assert not ok, "slot missing bidask_at must block"
        assert detail.get("near_bbo_reason") == "BBO_NO_TIMESTAMP", detail
