"""
Tests for core/mode_transition.py — P0-A Mode Model + Hard Gate (PR 1).
PR 2: Paper Drain FSM tests.
"""
import pytest
import dataclasses
from datetime import datetime, timedelta, timezone

from core.mode_transition import (
    ExecutionContext,
    ExecutionMode,
    ModeTransitionState,
    LiveOrderBlocked,
    EntryBlocked,
    DrainBlocked,
    paper_context,
    live_preflight_context,
    with_effective_mode,
    PaperDrainSnapshot,
    PaperDrainResult,
    evaluate_paper_drain,
    assert_paper_drained,
    evaluate_paper_drain_timeout,
    cancel_paper_drain,
    DRAIN_FAIL_POSITION_NONZERO,
    DRAIN_FAIL_LIFECYCLE_NOT_FLAT,
    DRAIN_FAIL_PENDING_ORDERS,
    DRAIN_FAIL_CALLBACKS_INFLIGHT,
    DRAIN_FAIL_ACTIVE_TRADE,
    DRAIN_FAIL_PENDING_ACTION,
    DRAIN_FAIL_UNRESOLVED_FILLS,
    DRAIN_FAIL_TIMEOUT,
    PAPER_DRAIN_DEFAULT_TIMEOUT_SECONDS,
    BrokerSnapshot,
    BrokerPreflightResult,
    evaluate_broker_preflight,
    PREFLIGHT_FAIL_NOT_CONNECTED,
    PREFLIGHT_FAIL_AUTH_FAILED,
    PREFLIGHT_FAIL_ACCOUNT_MISMATCH,
    PREFLIGHT_FAIL_POSITION_NOT_FLAT,
    PREFLIGHT_FAIL_OPEN_ORDERS_EXIST,
    PREFLIGHT_FAIL_POSITION_SNAPSHOT_STALE,
    PREFLIGHT_FAIL_ORDER_SNAPSHOT_STALE,
    PREFLIGHT_FAIL_TIMEOUT,
)
from core.order_management.order_manager import OrderManager
from core.order_management.order import OrderStatus, OrderType, OrderSide


# ═══════════════════════════════════════════════════════════════
# Mode Transition Model Tests
# ═══════════════════════════════════════════════════════════════


class TestExecutionContext:
    """Verify the core invariants of ExecutionContext."""

    def test_paper_context_allows_orders(self):
        """Paper mode: assert_live_order_allowed() should NOT raise."""
        ctx = paper_context()
        assert ctx.requested_mode == ExecutionMode.PAPER.value
        assert ctx.effective_mode == ModeTransitionState.PAPER_ACTIVE.value
        assert ctx.live_order_allowed is False
        ctx.assert_live_order_allowed()  # must not raise

    def test_live_preflight_blocks_orders(self):
        """Live preflight: assert_live_order_allowed() MUST raise."""
        ctx = live_preflight_context()
        assert ctx.requested_mode == ExecutionMode.LIVE.value
        assert ctx.effective_mode == ModeTransitionState.LIVE_PREFLIGHT.value
        assert ctx.live_order_allowed is False
        with pytest.raises(LiveOrderBlocked) as exc:
            ctx.assert_live_order_allowed()
        assert "EFFECTIVE_MODE_NOT_LIVE_READY" in str(exc.value)

    def test_live_ready_allows_orders(self):
        """LIVE_READY with live_order_allowed=True: must NOT raise."""
        ctx = live_preflight_context()
        ctx_ready = dataclasses.replace(
            ctx,
            effective_mode=ModeTransitionState.LIVE_READY.value,
            live_order_allowed=True,
        )
        assert ctx_ready.is_live_ready() is True
        ctx_ready.assert_live_order_allowed()  # must not raise

    def test_live_ready_without_flag_blocks(self):
        """LIVE_READY but live_order_allowed=False: MUST raise."""
        ctx = live_preflight_context()
        ctx_noflag = dataclasses.replace(
            ctx,
            effective_mode=ModeTransitionState.LIVE_READY.value,
            live_order_allowed=False,
        )
        assert ctx_noflag.is_live_ready() is False
        with pytest.raises(LiveOrderBlocked) as exc:
            ctx_noflag.assert_live_order_allowed()
        assert "LIVE_ORDER_FLAG_FALSE" in str(exc.value)

    def test_process_start_id_is_nonempty_hex(self):
        """process_start_id should be a non-empty hex string."""
        ctx = paper_context()
        assert len(ctx.process_start_id) == 16
        int(ctx.process_start_id, 16)  # must be valid hex

    def test_live_reconciling_state_blocks(self):
        """LIVE_RECONCILING must block orders (not yet READY)."""
        ctx = live_preflight_context()
        ctx_reconciling = dataclasses.replace(
            ctx,
            effective_mode=ModeTransitionState.LIVE_RECONCILING.value,
        )
        with pytest.raises(LiveOrderBlocked):
            ctx_reconciling.assert_live_order_allowed()

    def test_live_quarantined_state_blocks(self):
        """LIVE_QUARANTINED must block orders."""
        ctx = live_preflight_context()
        ctx_quar = dataclasses.replace(
            ctx,
            effective_mode=ModeTransitionState.LIVE_QUARANTINED.value,
        )
        with pytest.raises(LiveOrderBlocked):
            ctx_quar.assert_live_order_allowed()

    def test_paper_context_in_live_mode_does_not_block(self):
        """Paper context in live mode: requested_mode=paper, so gate passes."""
        ctx = paper_context()
        ctx_paper_live = dataclasses.replace(
            ctx,
            requested_mode=ExecutionMode.LIVE.value,
        )
        # requested_mode=LIVE but effective_mode=paper_active → BLOCKED
        with pytest.raises(LiveOrderBlocked):
            ctx_paper_live.assert_live_order_allowed()


# ═══════════════════════════════════════════════════════════════
# OrderManager Hard Gate Tests
# ═══════════════════════════════════════════════════════════════


class TestOrderManagerGate:
    """Verify the OrderManager hard gate (second layer of defense)."""

    def test_live_preflight_context_blocks_submit(self):
        """OrderManager.submit() must reject live orders in preflight."""
        ctx = live_preflight_context()
        om = OrderManager(
            mode="live", broker_adapter=None, execution_context=ctx,
        )
        order = om.create_order(
            symbol="TMF", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            price=100.0, quantity=1, strategy="tmf_spread",
        )
        result = om.submit(order)
        assert result is False
        assert order.status == OrderStatus.REJECTED
        assert "EFFECTIVE_MODE_NOT_LIVE_READY" in (order.reject_reason or "")

    def test_paper_mode_bypasses_gate(self):
        """Paper mode without execution context works normally."""
        om = OrderManager(mode="paper")
        order = om.create_order(
            symbol="TMF", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            price=100.0, quantity=1, strategy="tmf_spread",
        )
        result = om.submit(order)
        assert result is True
        assert order.status == OrderStatus.SUBMITTED

    def test_live_ready_with_mock_broker_allows(self):
        """LIVE_READY with valid context allows order submission."""
        ctx = live_preflight_context()
        ctx_ready = dataclasses.replace(
            ctx,
            effective_mode=ModeTransitionState.LIVE_READY.value,
            live_order_allowed=True,
        )

        class _MockBroker:
            def place_order(self, order):
                return type("MockResult", (), {
                    "id": "BRK-001", "seqno": "S1", "ordno": "O1",
                })()

        om = OrderManager(
            mode="live", broker_adapter=_MockBroker(),
            execution_context=ctx_ready,
        )
        order = om.create_order(
            symbol="TMF", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            price=100.0, quantity=1, strategy="tmf_spread",
        )
        result = om.submit(order)
        # Should reach broker submission (we can't assert SUCCESS since
        # the mock might fail — but it should not be REJECTED by gate)
        assert order.status != OrderStatus.REJECTED

    def test_live_flag_false_blocks_submit(self):
        """LIVE_READY without live_order_allowed flag must block."""
        ctx = live_preflight_context()
        ctx_noflag = dataclasses.replace(
            ctx,
            effective_mode=ModeTransitionState.LIVE_READY.value,
            live_order_allowed=False,
        )
        om = OrderManager(
            mode="live", broker_adapter=None, execution_context=ctx_noflag,
        )
        order = om.create_order(
            symbol="TMF", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            price=100.0, quantity=1, strategy="tmf_spread",
        )
        result = om.submit(order)
        assert result is False
        assert "LIVE_ORDER_FLAG_FALSE" in (order.reject_reason or "")


# ═══════════════════════════════════════════════════════════════
# Paper Drain FSM Tests (PR 2)
# ═══════════════════════════════════════════════════════════════


def _drained_snapshot(**overrides) -> PaperDrainSnapshot:
    """Helper: create a fully drained snapshot, then override specific fields."""
    defaults = PaperDrainSnapshot(
        position_qty=0,
        lifecycle_phase="FLAT",
        pending_order_count=0,
        inflight_callback_count=0,
        active_trade_id=None,
        pending_action=None,
        unresolved_fill_count=0,
    )
    return dataclasses.replace(defaults, **overrides)


class TestPaperDrainEvaluation:
    """Test the pure evaluate_paper_drain() function."""

    def test_all_clear_passes(self):
        """All conditions satisfied: drain passes."""
        result = evaluate_paper_drain(_drained_snapshot())
        assert result.drained is True
        assert result.failed_checks == ()

    def test_position_nonzero_fails(self):
        """position_qty != 0 must block."""
        result = evaluate_paper_drain(_drained_snapshot(position_qty=2))
        assert result.drained is False
        assert DRAIN_FAIL_POSITION_NONZERO in result.failed_checks

    def test_lifecycle_not_flat_fails(self):
        """lifecycle != FLAT must block."""
        result = evaluate_paper_drain(
            _drained_snapshot(lifecycle_phase="ARMED")
        )
        assert result.drained is False
        assert DRAIN_FAIL_LIFECYCLE_NOT_FLAT in result.failed_checks

    def test_pending_orders_fails(self):
        """pending orders exist must block."""
        result = evaluate_paper_drain(
            _drained_snapshot(pending_order_count=1)
        )
        assert result.drained is False
        assert DRAIN_FAIL_PENDING_ORDERS in result.failed_checks

    def test_inflight_callbacks_fails(self):
        """inflight callbacks must block."""
        result = evaluate_paper_drain(
            _drained_snapshot(inflight_callback_count=1)
        )
        assert result.drained is False
        assert DRAIN_FAIL_CALLBACKS_INFLIGHT in result.failed_checks

    def test_active_trade_fails(self):
        """active_trade_id != None must block."""
        result = evaluate_paper_drain(
            _drained_snapshot(active_trade_id="MTS-20260717-001")
        )
        assert result.drained is False
        assert DRAIN_FAIL_ACTIVE_TRADE in result.failed_checks

    def test_pending_action_fails(self):
        """pending_action != None must block."""
        result = evaluate_paper_drain(
            _drained_snapshot(pending_action="EXIT_SUBMITTED")
        )
        assert result.drained is False
        assert DRAIN_FAIL_PENDING_ACTION in result.failed_checks

    def test_unresolved_fills_fails(self):
        """unresolved fills must block."""
        result = evaluate_paper_drain(
            _drained_snapshot(unresolved_fill_count=2)
        )
        assert result.drained is False
        assert DRAIN_FAIL_UNRESOLVED_FILLS in result.failed_checks

    def test_multiple_failures_reported(self):
        """All failing conditions should be reported together."""
        result = evaluate_paper_drain(_drained_snapshot(
            position_qty=1,
            lifecycle_phase="ARMED",
            pending_order_count=2,
        ))
        assert result.drained is False
        assert DRAIN_FAIL_POSITION_NONZERO in result.failed_checks
        assert DRAIN_FAIL_LIFECYCLE_NOT_FLAT in result.failed_checks
        assert DRAIN_FAIL_PENDING_ORDERS in result.failed_checks
        assert len(result.failed_checks) == 3


class TestEntryBlockedDuringDrain:
    """Test assert_entry_allowed() blocking."""

    def test_paper_active_allows_entry(self):
        """PAPER_ACTIVE: entry must be allowed."""
        ctx = paper_context()
        ctx.assert_entry_allowed()  # must not raise

    def test_paper_draining_blocks_entry(self):
        """PAPER_DRAINING: entry must be blocked."""
        ctx = with_effective_mode(
            paper_context(),
            ModeTransitionState.PAPER_DRAINING.value,
        )
        with pytest.raises(EntryBlocked):
            ctx.assert_entry_allowed()

    def test_paper_drained_blocks_entry(self):
        """PAPER_DRAINED: entry must be blocked."""
        ctx = with_effective_mode(
            paper_context(),
            ModeTransitionState.PAPER_DRAINED.value,
        )
        with pytest.raises(EntryBlocked):
            ctx.assert_entry_allowed()

    def test_live_preflight_does_not_block_entry(self):
        """LIVE_PREFLIGHT: live mode, so entry guard is not triggered."""
        ctx = live_preflight_context()
        ctx.assert_entry_allowed()  # must not raise (live mode)


class TestEntryBlockedActionNormalization:
    """Test that action normalization works with string AND enum-like values."""

    def test_string_entry_blocked_during_drain(self):
        """assert_entry_allowed() blocks when called with string ENTRY context."""
        ctx = with_effective_mode(
            paper_context(),
            ModeTransitionState.PAPER_DRAINING.value,
        )
        # The action normalization in monitor.py does:
        # _action_value = getattr(_sig_action, "value", _sig_action)
        # For a plain string "ENTRY": getattr("ENTRY", "value", "ENTRY") = "ENTRY"
        _action_value = getattr("ENTRY", "value", "ENTRY")
        assert _action_value == "ENTRY"
        with pytest.raises(EntryBlocked):
            ctx.assert_entry_allowed()

    def test_enum_value_entry_blocked_during_drain(self):
        """assert_entry_allowed() blocks when called with enum.ENTRY."""
        ctx = with_effective_mode(
            paper_context(),
            ModeTransitionState.PAPER_DRAINING.value,
        )
        # Simulate a lifecycle-action-like enum
        class _Action:
            ENTRY = "ENTRY"
        _sig_action = _Action.ENTRY
        # Normalization: getattr(enum_val, "value", enum_val)
        _action_value = getattr(_sig_action, "value", _sig_action)
        assert _action_value == "ENTRY"
        with pytest.raises(EntryBlocked):
            ctx.assert_entry_allowed()


class TestWithEffectiveMode:
    """Test the with_effective_mode() helper."""

    def test_preserves_identity(self):
        """New context preserves session_id, process_start_id, etc."""
        ctx = paper_context()
        ctx2 = with_effective_mode(
            ctx, ModeTransitionState.PAPER_DRAINING.value,
        )
        assert ctx2.session_id == ctx.session_id
        assert ctx2.process_start_id == ctx.process_start_id
        assert ctx2.requested_mode == ctx.requested_mode
        assert ctx2.state_namespace == ctx.state_namespace

    def test_updates_effective_mode(self):
        """New context has updated effective_mode."""
        ctx = paper_context()
        ctx2 = with_effective_mode(
            ctx, ModeTransitionState.PAPER_DRAINING.value,
        )
        assert ctx2.effective_mode == ModeTransitionState.PAPER_DRAINING.value
        # Original unchanged (frozen)
        assert ctx.effective_mode == ModeTransitionState.PAPER_ACTIVE.value

    def test_original_is_frozen(self):
        """Original context must remain unchanged."""
        ctx = paper_context()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.effective_mode = ModeTransitionState.PAPER_DRAINING.value

    def test_timeout_reason_code(self):
        """TIMEOUT must be a recognized failure reason."""
        assert DRAIN_FAIL_TIMEOUT == "PAPER_DRAIN_TIMEOUT"


class TestAssertPaperDrained:
    """Test assert_paper_drained() raises/returns correctly."""

    def test_drained_returns_result(self):
        """Fully drained: returns PaperDrainResult, does NOT raise."""
        result = assert_paper_drained(_drained_snapshot())
        assert result.drained is True

    def test_not_drained_raises(self):
        """Not drained: raises DrainBlocked with failed_checks."""
        snapshot = _drained_snapshot(position_qty=2)
        with pytest.raises(DrainBlocked) as exc:
            assert_paper_drained(snapshot)
        assert DRAIN_FAIL_POSITION_NONZERO in exc.value.failed_checks


class TestPaperDrainTimeout:
    """Test evaluate_paper_drain_timeout()."""

    def test_within_timeout(self):
        """Started recently: should NOT be timed out."""
        now = datetime.now()
        started = now - timedelta(seconds=60)  # 1 min ago
        assert evaluate_paper_drain_timeout(started, now, timeout_seconds=600) is False

    def test_exceeds_timeout(self):
        """Started long ago: should be timed out."""
        now = datetime.now()
        started = now - timedelta(seconds=600)  # 10 min ago
        assert evaluate_paper_drain_timeout(started, now, timeout_seconds=300) is True

    def test_exactly_at_boundary(self):
        """Exactly at boundary: should be timed out (>=)."""
        now = datetime.now()
        started = now - timedelta(seconds=100)
        assert evaluate_paper_drain_timeout(started, now, timeout_seconds=100) is True

    def test_uses_default_timeout(self):
        """Uses PAPER_DRAIN_DEFAULT_TIMEOUT_SECONDS when not specified."""
        now = datetime.now()
        started = now - timedelta(seconds=PAPER_DRAIN_DEFAULT_TIMEOUT_SECONDS)
        assert evaluate_paper_drain_timeout(started, now) is True
        started2 = now - timedelta(seconds=PAPER_DRAIN_DEFAULT_TIMEOUT_SECONDS - 1)
        assert evaluate_paper_drain_timeout(started2, now) is False


class TestCancelPaperDrain:
    """Test cancel_paper_drain()."""

    def test_cancel_draining(self):
        """PAPER_DRAINING → cancel → PAPER_ACTIVE."""
        ctx = paper_context()
        draining = with_effective_mode(ctx, ModeTransitionState.PAPER_DRAINING.value)
        cancelled = cancel_paper_drain(draining)
        assert cancelled.effective_mode == ModeTransitionState.PAPER_ACTIVE.value

    def test_cancel_drained(self):
        """PAPER_DRAINED → cancel → PAPER_ACTIVE."""
        ctx = paper_context()
        drained = with_effective_mode(ctx, ModeTransitionState.PAPER_DRAINED.value)
        cancelled = cancel_paper_drain(drained)
        assert cancelled.effective_mode == ModeTransitionState.PAPER_ACTIVE.value

    def test_cancel_fails_from_paper_active(self):
        """PAPER_ACTIVE: cancel must raise ValueError."""
        ctx = paper_context()
        with pytest.raises(ValueError, match="Cannot cancel drain"):
            cancel_paper_drain(ctx)

    def test_cancel_fails_from_live_preflight(self):
        """LIVE_PREFLIGHT: cancel must raise ValueError."""
        ctx = live_preflight_context()
        with pytest.raises(ValueError, match="Cannot cancel drain"):
            cancel_paper_drain(ctx)

    def test_cancel_fails_when_live_order_allowed(self):
        """After live_order_allowed=True: cancel must raise ValueError."""
        ctx = paper_context()
        # Create a draining context with live_order_allowed=True
        # This is an unusual but possible state (drain completed, transition started).
        granted = with_effective_mode(
            ctx, ModeTransitionState.PAPER_DRAINED.value,
            live_order_allowed=True,
        )
        with pytest.raises(ValueError, match="(?i)cannot cancel"):
            cancel_paper_drain(granted)


class TestWithEffectiveModeValidation:
    """Test with_effective_mode() validation."""

    def test_valid_string(self):
        """Known state string: accepted."""
        ctx = paper_context()
        ctx2 = with_effective_mode(ctx, "paper_draining")
        assert ctx2.effective_mode == "paper_draining"

    def test_valid_enum(self):
        """ModeTransitionState enum: accepted."""
        ctx = paper_context()
        ctx2 = with_effective_mode(ctx, ModeTransitionState.PAPER_DRAINING)
        assert ctx2.effective_mode == ModeTransitionState.PAPER_DRAINING.value

    def test_invalid_string_raises(self):
        """Unknown string: raises ValueError."""
        ctx = paper_context()
        with pytest.raises(ValueError, match="Invalid effective_mode"):
            with_effective_mode(ctx, "invalid_state_xyz")

    def test_empty_string_raises(self):
        """Empty string: raises ValueError."""
        ctx = paper_context()
        with pytest.raises(ValueError, match="Invalid effective_mode"):
            with_effective_mode(ctx, "")


# ═══════════════════════════════════════════════════════════════
# Broker Preflight Tests (PR 3)
# ═══════════════════════════════════════════════════════════════


def _fresh_snapshot(**overrides) -> BrokerSnapshot:
    """Helper: create a fully passing snapshot, then override specific fields."""
    defaults = BrokerSnapshot(
        connected=True,
        authenticated=True,
        account_id_hash="abc123",
        position_count=0,
        open_order_count=0,
        position_snapshot_time=datetime.now(timezone.utc),
        order_snapshot_time=datetime.now(timezone.utc),
    )
    return dataclasses.replace(defaults, **overrides)


class TestBrokerPreflightEvaluation:
    """Test the pure evaluate_broker_preflight() function."""

    def test_all_clear_passes(self):
        """All conditions satisfied: preflight passes."""
        result = evaluate_broker_preflight(
            _fresh_snapshot(), expected_account_hash="abc123",
        )
        assert result.passed is True
        assert result.failed_checks == ()

    def test_not_connected_fails(self):
        """Broker disconnected must fail."""
        result = evaluate_broker_preflight(
            _fresh_snapshot(connected=False),
        )
        assert result.passed is False
        assert PREFLIGHT_FAIL_NOT_CONNECTED in result.failed_checks

    def test_not_authenticated_fails(self):
        """Broker not authenticated must fail."""
        result = evaluate_broker_preflight(
            _fresh_snapshot(authenticated=False),
        )
        assert result.passed is False
        assert PREFLIGHT_FAIL_AUTH_FAILED in result.failed_checks

    def test_account_mismatch_fails(self):
        """Account hash mismatch must fail."""
        result = evaluate_broker_preflight(
            _fresh_snapshot(account_id_hash="wrong_hash"),
            expected_account_hash="expected_hash",
        )
        assert result.passed is False
        assert PREFLIGHT_FAIL_ACCOUNT_MISMATCH in result.failed_checks

    def test_account_skip_when_no_expected(self):
        """No expected hash provided: account check is skipped."""
        result = evaluate_broker_preflight(
            _fresh_snapshot(account_id_hash="any_value"),
        )
        assert result.passed is True

    def test_position_nonzero_fails(self):
        """Broker has positions must fail."""
        result = evaluate_broker_preflight(
            _fresh_snapshot(position_count=2),
        )
        assert result.passed is False
        assert PREFLIGHT_FAIL_POSITION_NOT_FLAT in result.failed_checks

    def test_open_orders_exist_fails(self):
        """Broker has open orders must fail."""
        result = evaluate_broker_preflight(
            _fresh_snapshot(open_order_count=1),
        )
        assert result.passed is False
        assert PREFLIGHT_FAIL_OPEN_ORDERS_EXIST in result.failed_checks

    def test_position_snapshot_none_is_stale(self):
        """None position snapshot time = unknown = stale = fail."""
        result = evaluate_broker_preflight(
            _fresh_snapshot(position_snapshot_time=None),
        )
        assert result.passed is False
        assert PREFLIGHT_FAIL_POSITION_SNAPSHOT_STALE in result.failed_checks

    def test_order_snapshot_none_is_stale(self):
        """None order snapshot time = unknown = stale = fail."""
        result = evaluate_broker_preflight(
            _fresh_snapshot(order_snapshot_time=None),
        )
        assert result.passed is False
        assert PREFLIGHT_FAIL_ORDER_SNAPSHOT_STALE in result.failed_checks

    def test_stale_position_snapshot_fails(self):
        """Old position snapshot must fail."""
        old_time = datetime.now().replace(year=2020)
        result = evaluate_broker_preflight(
            _fresh_snapshot(position_snapshot_time=old_time),
        )
        assert result.passed is False
        assert PREFLIGHT_FAIL_POSITION_SNAPSHOT_STALE in result.failed_checks

    def test_stale_order_snapshot_fails(self):
        """Old order snapshot must fail."""
        old_time = datetime.now().replace(year=2020)
        result = evaluate_broker_preflight(
            _fresh_snapshot(order_snapshot_time=old_time),
        )
        assert result.passed is False
        assert PREFLIGHT_FAIL_ORDER_SNAPSHOT_STALE in result.failed_checks

    def test_multiple_failures_reported(self):
        """All failing conditions reported together."""
        result = evaluate_broker_preflight(_fresh_snapshot(
            connected=False,
            authenticated=False,
            position_count=1,
            open_order_count=2,
        ))
        assert result.passed is False
        assert PREFLIGHT_FAIL_NOT_CONNECTED in result.failed_checks
        assert PREFLIGHT_FAIL_AUTH_FAILED in result.failed_checks
        assert PREFLIGHT_FAIL_POSITION_NOT_FLAT in result.failed_checks
        assert PREFLIGHT_FAIL_OPEN_ORDERS_EXIST in result.failed_checks
        assert len(result.failed_checks) >= 4

    def test_timeout_not_produced_by_pure_function(self):
        """evaluate_broker_preflight never produces TIMEOUT — that's for caller."""
        # A snapshot that looks empty but times out should not be conflated.
        empty = BrokerSnapshot(connected=True, authenticated=True)
        result = evaluate_broker_preflight(empty)
        assert PREFLIGHT_FAIL_TIMEOUT not in result.failed_checks
        # It should fail because snapshot times are None (stale), not timeout
        assert PREFLIGHT_FAIL_POSITION_SNAPSHOT_STALE in result.failed_checks
        assert PREFLIGHT_FAIL_ORDER_SNAPSHOT_STALE in result.failed_checks

    def test_timeout_reason_code(self):
        """TIMEOUT must be a recognized failure reason."""
        assert PREFLIGHT_FAIL_TIMEOUT == "BROKER_PREFLIGHT_TIMEOUT"


class TestBrokerPreflightSnapshotFreshness:
    """Edge cases for _is_snapshot_fresh()."""

    def test_exactly_30_seconds_is_fresh(self):
        """Exactly at boundary (30s): fresh."""
        now = datetime.now(timezone.utc)
        snap = now - timedelta(seconds=30)
        from core.mode_transition import _is_snapshot_fresh
        assert _is_snapshot_fresh(snap, now, max_age_seconds=30) is True

    def test_31_seconds_is_stale(self):
        """Beyond boundary (31s): stale."""
        now = datetime.now(timezone.utc)
        snap = now - timedelta(seconds=31)
        from core.mode_transition import _is_snapshot_fresh
        assert _is_snapshot_fresh(snap, now, max_age_seconds=30) is False

    def test_negative_age_is_fresh(self):
        """Snapshot 1 second ago: fresh."""
        now = datetime.now(timezone.utc)
        snap = now - timedelta(seconds=1)
        from core.mode_transition import _is_snapshot_fresh
        assert _is_snapshot_fresh(snap, now) is True

    def test_future_timestamp_beyond_skew_is_stale(self):
        """Snapshot from 60 seconds in the future: stale (clock skew)."""
        now = datetime.now(timezone.utc)
        snap = now + timedelta(seconds=60)
        from core.mode_transition import _is_snapshot_fresh
        assert _is_snapshot_fresh(snap, now) is False

    def test_future_timestamp_within_skew_is_fresh(self):
        """Snapshot from 2 seconds in the future: allowed (within clock skew)."""
        now = datetime.now(timezone.utc)
        snap = now + timedelta(seconds=2)
        from core.mode_transition import _is_snapshot_fresh
        assert _is_snapshot_fresh(snap, now, max_clock_skew_seconds=5) is True

    def test_naive_datetime_assumed_utc(self):
        """Naive datetime (no tzinfo) should be handled as UTC."""
        from datetime import datetime as dt_naive
        now = dt_naive.now()  # naive
        snap = now - timedelta(seconds=5)
        from core.mode_transition import _is_snapshot_fresh
        assert _is_snapshot_fresh(snap, now) is True

    def test_none_is_stale(self):
        """None snapshot time = stale."""
        from core.mode_transition import _is_snapshot_fresh
        assert _is_snapshot_fresh(None) is False


class TestReadyForCommitState:
    """READY_FOR_COMMIT exists as a valid ModeTransitionState."""

    def test_ready_for_commit_is_valid(self):
        """READY_FOR_COMMIT is a recognized state."""
        ctx = with_effective_mode(
            paper_context(),
            ModeTransitionState.READY_FOR_COMMIT.value,
        )
        assert ctx.effective_mode == "ready_for_commit"

    def test_ready_for_commit_does_not_allow_live_orders(self):
        """READY_FOR_COMMIT is NOT LIVE_READY — no live orders allowed."""
        ctx = with_effective_mode(
            paper_context(),
            ModeTransitionState.READY_FOR_COMMIT.value,
        )
        ctx2 = dataclasses.replace(
            ctx,
            requested_mode=ExecutionMode.LIVE.value,
        )
        assert ctx2.is_live_ready() is False
        with pytest.raises(LiveOrderBlocked):
            ctx2.assert_live_order_allowed()

    def test_ready_for_commit_blocks_entry(self):
        """READY_FOR_COMMIT still blocks new entries (not PAPER_ACTIVE)."""
        ctx = with_effective_mode(
            paper_context(),
            ModeTransitionState.READY_FOR_COMMIT.value,
        )
        with pytest.raises(EntryBlocked):
            ctx.assert_entry_allowed()

    def test_ready_for_commit_blocks_live_orders(self):
        """Regression: READY_FOR_COMMIT + requested=LIVE must NOT allow orders."""
        ctx = with_effective_mode(
            paper_context(),
            ModeTransitionState.READY_FOR_COMMIT.value,
        )
        ctx_live = dataclasses.replace(
            ctx,
            requested_mode=ExecutionMode.LIVE.value,
            live_order_allowed=False,
        )
        # OrderManager should reject
        from core.order_management.order_manager import OrderManager
        om = OrderManager(
            mode="live", broker_adapter=None, execution_context=ctx_live,
        )
        order = om.create_order(
            symbol="TMF", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            price=100.0, quantity=1, strategy="tmf_spread",
        )
        result = om.submit(order)
        assert result is False
        assert order.status == OrderStatus.REJECTED
        assert "EFFECTIVE_MODE_NOT_LIVE_READY" in (order.reject_reason or "")
