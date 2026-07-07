"""
Contract: MTS manual trade flag lifecycle — retry, TTL, schema, idempotency.

P0: A manual trade flag MUST survive a NO_LIVE_TICK rejection so the next
tick can retry. Additional validity checks (schema, TTL, idempotency) MUST
reject definitively and NOT leave orphaned processing files.

Test groups (parallel to implementation steps):
  Group A (Step 2): retry, schema, idempotency, TTL
  Group B (Step 3): fallback chain in paper mode
  Group C (Step 4): far-month tick does not consume flag

Usage:
    pytest tests/contracts/test_mts_no_live_tick_retry.py -v
"""
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# 2026-06-26 Gemini CLI: Isolate manual trade flag path for test stability
os.environ["FUTURES_MANUAL_TRADE_FLAG_PATH"] = "/tmp/test_mts_no_live_tick_retry.flag"

# ── Flag path (matches the environment-configured path in monitor.py) ──
_FLAG = "/tmp/test_mts_no_live_tick_retry.flag"
_PROCESSING = _FLAG + ".processing"


def _write_flag(**kwargs) -> dict:
    """Write a manual trade flag to disk with defaults."""
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


@pytest.fixture(autouse=True)
def _clean_flags():
    """Remove any flag files before and after each test."""
    for p in (_FLAG, _PROCESSING):
        if os.path.exists(p):
            os.remove(p)
    yield
    for p in (_FLAG, _PROCESSING):
        if os.path.exists(p):
            os.remove(p)


# ═══════════════════════════════════════════════════════════
# Group A — These tests target Step 2 (atomic lifecycle)
# ═══════════════════════════════════════════════════════════


def _make_minimal_monitor():
    """Create a FuturesMonitor in dry_run with minimal setup."""
    from strategies.futures.monitor import FuturesMonitor

    dummy_api = type("A", (), {})()
    mon = FuturesMonitor(api=dummy_api, config_path="config/futures_night.yaml", dry_run=True)
    mon.ticker = "TMF"
    mon._use_order_manager = True

    # Setup the basic attributes that _process_manual_trade_flag() reads
    mon.trader = SimpleNamespace(position=0)
    mon.order_mgr = MagicMock()
    mon.order_mgr.active_orders = {}
    mon._manual_trade_status = "READY"
    mon._pending_lifecycle_orders = {}
    mon._registry = {}

    # 2026-07-07 Hermes Agent: contract mocks required (placeholder guard)
    mon.contract = SimpleNamespace(code="TMFF6")
    mon.far_contract = SimpleNamespace(code="TMFH6")

    # market_data init value — the minimal state that triggers NO_LIVE_TICK
    mon.market_data = {mon.ticker: {"close": None}}
    mon._far_current_bar = {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ts": None}
    mon.dry_run = True
    mon.live_trading = False
    return mon


class TestGroupA_RetryAndValidity:
    """Group A: validated by Step 2 (atomic lifecycle + idempotency)."""

    def test_flag_survives_no_live_tick_retry(self):
        """
        When _process_manual_trade_flag() hits a retryable rejection,
        the flag MUST still exist (as .processing) so the next tick can retry.
        
        Tests live mode NO_LIVE_TICK: market_data has no local_arrival_at,
        live_trading=True so fallback chain is NOT used.
        """
        mon = _make_minimal_monitor()
        mon.live_trading = True   # Live mode → no fallback chain
        mon.dry_run = False       # Not dry-run
        _write_flag()

        # Process once — should hit NO_LIVE_TICK (live mode, no tick yet)
        mon._process_manual_trade_flag()
        assert "NO_LIVE_TICK" in mon._manual_trade_status, (
            f"Expected NO_LIVE_TICK, got {mon._manual_trade_status}"
        )

        # Flag (.processing) must still exist — retry possible
        assert os.path.exists(_PROCESSING) or os.path.exists(_FLAG), (
            "Flag file is gone — no retry possible on next tick"
        )

    def test_flag_survives_no_price_source_retry(self):
        """
        In paper/dry-run mode, when all price tiers fail,
        flag MUST still exist (as .processing) for retry.
        """
        mon = _make_minimal_monitor()
        # Write flag without advisory prices to ensure all tiers fail
        _write_flag(near_close=None, far_close=None)

        mon._process_manual_trade_flag()
        assert "NO_PRICE_SOURCE" in mon._manual_trade_status, (
            f"Expected NO_PRICE_SOURCE, got {mon._manual_trade_status}"
        )
        assert os.path.exists(_PROCESSING) or os.path.exists(_FLAG), (
            "Flag file is gone — no retry possible on next tick"
        )

    def test_rejects_missing_action_key(self):
        """
        Flag without 'action' key → FAILED: INVALID_FLAG_SCHEMA.
        This is a terminal failure — no retry.
        """
        mon = _make_minimal_monitor()
        _write_flag()
        # Remove action key after writing
        d = json.load(open(_FLAG))
        del d["action"]
        json.dump(d, open(_FLAG, "w"))

        mon._process_manual_trade_flag()
        assert "INVALID_FLAG" in mon._manual_trade_status, (
            f"Expected INVALID_FLAG, got {mon._manual_trade_status}"
        )
        # No processing file should remain — terminal failure
        assert not os.path.exists(_PROCESSING), (
            ".processing file should be cleaned up on terminal failure"
        )

    def test_same_flag_idempotent(self):
        """
        Submitting the same flag twice produces only one order.
        Second call skips processing entirely.
        """
        mon = _make_minimal_monitor()
        mon._manual_trade_status = "READY"
        # Override price source by giving market data a valid entry
        mon.market_data[mon.ticker] = {"close": 21000.0, "local_arrival_at": time.time(), "datetime": "2026-06-05 10:00:00"}
        mon.dry_run = False
        mon.live_trading = False
        # Need _tick_bars_deque for paper mode fallback
        from collections import deque
        mon._tick_bars_deque = deque(maxlen=300)

        _write_flag()

        # First call
        mon._process_manual_trade_flag()
        first_status = mon._manual_trade_status

        # Set manual_trade_status back to READY — simulate a new tick cycle
        mon._manual_trade_status = "READY"

        # Dashboard writes the same flag again (user double-clicks)
        _write_flag()

        # Second call — should skip due to idempotency
        mon._process_manual_trade_flag()
        second_status = mon._manual_trade_status

        # The second call MUST NOT proceed to submission.
        # It should be SKIPPED by idempotency check.
        assert "SKIPPED" in second_status or "IDEM" in second_status, (
            f"Second call with same flag should be idempotent, got {second_status}"
        )

    def test_ttl_expired_flag_rejected(self):
        """
        Flag older than TTL_SECONDS → REJECTED: FLAG_EXPIRED.
        Flag file cleaned up — terminal failure.
        """
        mon = _make_minimal_monitor()
        # Write a flag with created_at far in the past
        _write_flag(created_at=time.time() - 7200)  # 2 hours ago

        mon._process_manual_trade_flag()
        assert "FLAG_EXPIRED" in mon._manual_trade_status, (
            f"Expected FLAG_EXPIRED, got {mon._manual_trade_status}"
        )
        # No processing file should remain
        assert not os.path.exists(_PROCESSING), (
            ".processing file should be cleaned up on FLAG_EXPIRED"
        )

    def test_ttl_backward_compat_no_created_at(self):
        """
        Flag without created_at key → skip TTL check (backward compatible).
        Processing proceeds (or fails on next check, but not on TTL).
        """
        mon = _make_minimal_monitor()
        _write_flag()
        # Remove created_at to simulate old dashboard
        d = json.load(open(_FLAG))
        del d["created_at"]
        json.dump(d, open(_FLAG, "w"))

        mon._process_manual_trade_flag()
        # Should NOT be FLAG_EXPIRED — TTL was skipped
        assert "FLAG_EXPIRED" not in mon._manual_trade_status, (
            "Flag without created_at should skip TTL check"
        )


# ═══════════════════════════════════════════════════════════
# Group B — These tests target Step 3 (price fallback)
# ═══════════════════════════════════════════════════════════


class TestGroupB_FallbackChain:
    """Group B: validated by Step 3 (price fallback chain — dry_run only).
    
    2026-06-05 我的Clawbot: Step 3 revised — fallback chain is dry_run-only.
    Paper mode (dry_run=False) gets real Shioaji ticks → LIVE_TICK path.
    """

    def test_dry_run_uses_bar_fallback(self):
        """
        In dry_run mode (no Shioaji), when _tick_bars_deque has data,
        the price should be resolved from BAR_CLOSE (Tier 2).
        """
        mon = _make_minimal_monitor()
        # dry_run=True is already the default in _make_minimal_monitor

        # Seed the tick bars deque with a valid bar
        import pandas as pd
        from collections import deque
        mon._tick_bars_deque = deque(maxlen=300)
        mon._tick_bars_deque.append({
            "open": 20950.0, "high": 21010.0, "low": 20950.0,
            "close": 21000.0, "volume": 100,
            "ts": pd.Timestamp("2026-06-05 09:00:00"),
        })

        _write_flag()

        # Process — should resolve price from deque (BAR_CLOSE), not hit NO_LIVE_TICK
        mon._process_manual_trade_flag()

        # Should have passed price check and proceeded (may fail on margin/order_mgr)
        assert "NO_LIVE_TICK" not in mon._manual_trade_status, (
            f"dry_run should use fallback chain, not NO_LIVE_TICK. Got: {mon._manual_trade_status}"
        )
        assert "NO_PRICE_SOURCE" not in mon._manual_trade_status, (
            f"BAR_CLOSE should resolve. Got: {mon._manual_trade_status}"
        )

    def test_paper_mode_gets_live_tick(self):
        """
        Paper mode (dry_run=False, live_trading=False) receives real Shioaji
        ticks and resolves price via LIVE_TICK — same as live mode.
        
        2026-06-05 我的Clawbot: core insight — paper and live share tick source.
        """
        mon = _make_minimal_monitor()
        mon.dry_run = False
        mon.live_trading = False  # paper mode

        # Simulate: Shioaji tick has populated market_data
        mon.market_data[mon.ticker] = {
            "close": 21000.0,
            "local_arrival_at": time.time(),
            "datetime": "2026-06-05 10:00:00",
        }

        _write_flag()

        mon._process_manual_trade_flag()

        # Paper mode should resolve via LIVE_TICK, not fallback chain
        assert "NO_LIVE_TICK" not in mon._manual_trade_status, (
            f"Paper mode with Shioaji tick should get LIVE_TICK. Got: {mon._manual_trade_status}"
        )

    def test_all_tiers_fail_returns_no_price_source(self):
        """
        In dry_run mode, when all 5 price tiers return None, must reject with
        REJECTED: NO_PRICE_SOURCE — never proceed with None price.
        """
        mon = _make_minimal_monitor()
        # dry_run=True is default — no _tick_bars_deque, far bar close=0

        _write_flag()
        # Remove near_close from flag — Tier 4 fails
        d = json.load(open(_FLAG))
        d.pop("near_close", None)
        json.dump(d, open(_FLAG, "w"))

        mon._process_manual_trade_flag()

        assert "NO_PRICE_SOURCE" in mon._manual_trade_status, (
            f"Expected NO_PRICE_SOURCE when all tiers fail, got {mon._manual_trade_status}"
        )


# ═══════════════════════════════════════════════════════════
# Group C — These tests target Step 4 (far-month gate)
# ═══════════════════════════════════════════════════════════


def _make_live_monitor():
    """Create a monitor in live mode (ticks arrive via on_tick)."""
    import pandas as pd
    from collections import deque
    from datetime import datetime
    from strategies.futures.monitor import FuturesMonitor

    dummy_api = type("A", (), {})()
    mon = FuturesMonitor(api=dummy_api, config_path="config/futures_night.yaml", dry_run=False)
    mon.ticker = "TMF"
    mon._use_order_manager = True
    mon.trader = SimpleNamespace(position=0)
    mon.order_mgr = MagicMock()
    mon.order_mgr.active_orders = {}
    mon._manual_trade_status = "READY"
    mon._pending_lifecycle_orders = {}
    mon._registry = {}
    mon.live_trading = True

    # Simulate a live near-month contract
    mon.contract = SimpleNamespace(code="TMFF6")
    mon.far_contract = SimpleNamespace(code="TMFG6")

    # Required by on_tick() bar pipeline
    mon._current_bar = {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ts": None}
    mon._tick_bars_deque = deque(maxlen=300)
    mon._last_bar_ts = 0
    mon._last_real_tmf_tick_at = time.time()
    mon._last_tmf_price = 21000.0
    mon.last_tick_at = time.time()
    mon._runtime_status = None
    mon._debug_feed = False
    mon._debug_tickbar = False

    # market_data populated (as it would be after the first near-month tick)
    mon.market_data = {mon.ticker: {"close": 21000.0, "local_arrival_at": time.time(), "datetime": "2026-06-05 10:00:00"}}
    mon._far_current_bar = {"open": 0, "high": 0, "low": 0, "close": 21100.0, "volume": 0, "ts": None}
    mon._last_far_bar_ts = 0
    return mon


class TestGroupC_FarMonthIsolation:
    """Group C: validated by Step 4 (far-month tick gate)."""

    def test_far_tick_does_not_consume_flag(self):
        """
        When a far-month tick arrives, on_tick() must NOT consume
        the manual trade flag. Only near-month ticks consume flags.
        """
        # 2026-06-25 Gemini CLI: Debug prints for flag path existence
        mon = _make_live_monitor()
        _write_flag()
        print(f"\n[DEBUG_TEST] Before on_tick: flag exists={os.path.exists(_FLAG)}, processing exists={os.path.exists(_PROCESSING)}")

        with patch("core.date_utils.is_day_session", return_value=True), \
             patch("core.date_utils.is_night_session", return_value=False):

            # Simulate a far-month tick
            far_tick = SimpleNamespace(
                code="TMFG6", close=21100.0,
                datetime="2026-06-05 10:00:00", volume=10
            )
            mon.on_tick(None, far_tick)
            print(f"[DEBUG_TEST] After on_tick: flag exists={os.path.exists(_FLAG)}, processing exists={os.path.exists(_PROCESSING)}")

            # Flag should NOT have been consumed (not renamed to .processing)
            assert os.path.exists(_FLAG), (
                "Far-month tick should NOT consume the flag"
            )
            assert not os.path.exists(_PROCESSING), (
                "Far-month tick should NOT rename flag to .processing"
            )

    def test_near_tick_consumes_flag(self):
        """
        When a near-month tick arrives, on_tick() MUST consume the flag.
        """
        mon = _make_live_monitor()
        _write_flag()

        with patch("core.date_utils.is_day_session", return_value=True), \
             patch("core.date_utils.is_night_session", return_value=False):

            # Simulate a near-month tick
            near_tick = SimpleNamespace(
                code="TMFF6", close=21000.0,
                datetime="2026-06-05 10:00:00", volume=10
            )
            mon.on_tick(None, near_tick)

            # Flag should have been renamed to .processing
            assert not os.path.exists(_FLAG), (
                "Near-month tick should consume the flag"
            )
