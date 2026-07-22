#!/usr/bin/env python3
"""Tests for core.channel_safety."""

import time
import pytest

from core.channel_safety import (
    AccountDegradedReason,
    ChannelHealth,
    ChannelSafetyState,
    get_safety_state,
    reset_safety_state,
)


class TestChannelSafetyState:
    def test_initial_state_entry_blocked(self) -> None:
        """Before reconciliation, entry is blocked."""
        s = ChannelSafetyState()
        assert s.entry_allowed() is False
        assert "RECONCILIATION_PENDING" in (s.entry_blocked_reason or "")

    def test_reconciled_but_not_healthy_entry_still_blocked(self) -> None:
        """Reconciled + account degraded = entry blocked."""
        s = ChannelSafetyState()
        s.reset_reconciled()
        s.set_account_degraded(AccountDegradedReason.SHIOAJI_CONNECTION_ERROR)
        s.set_reconciled()
        assert s.entry_allowed() is False
        assert "ACCOUNT_DEGRADED" in (s.entry_blocked_reason or "")

    def test_reconciled_and_healthy_entry_allowed(self) -> None:
        """Reconciled + account healthy = entry allowed."""
        s = ChannelSafetyState()
        s.reset_reconciled()
        s.set_reconciled()
        s.set_account_healthy()
        assert s.entry_allowed() is True
        assert s.entry_blocked_reason is None

    def test_reconciled_blocked_if_degraded_after_healthy(self) -> None:
        """Transition HEALTHY → DEGRADED blocks entry."""
        s = ChannelSafetyState()
        s.reset_reconciled()
        s.set_reconciled()
        s.set_account_healthy()
        assert s.entry_allowed() is True
        s.set_account_degraded(AccountDegradedReason.SHIOAJI_CONNECTION_ERROR)
        assert s.entry_allowed() is False

    def test_heartbeat_blocked_before_reconciliation(self) -> None:
        s = ChannelSafetyState()
        assert s.heartbeat_may_write_position() is False

    def test_heartbeat_blocked_when_degraded(self) -> None:
        s = ChannelSafetyState()
        s.reset_reconciled()
        s.set_reconciled()
        s.set_account_degraded(AccountDegradedReason.SHIOAJI_CONNECTION_ERROR)
        assert s.heartbeat_may_write_position() is False

    def test_heartbeat_allowed_when_reconciled_and_healthy(self) -> None:
        s = ChannelSafetyState()
        s.reset_reconciled()
        s.set_reconciled()
        s.set_account_healthy()
        assert s.heartbeat_may_write_position() is True

    def test_account_degraded_records_timestamp(self) -> None:
        s = ChannelSafetyState()
        before = time.time()
        s.set_account_degraded(AccountDegradedReason.SHIOAJI_CONNECTION_ERROR)
        after = time.time()
        assert s._account_degraded_at is not None
        assert before <= s._account_degraded_at <= after

    def test_account_degraded_records_message(self) -> None:
        s = ChannelSafetyState()
        s.set_account_degraded(AccountDegradedReason.SHIOAJI_CONNECTION_ERROR, "SolClient NotReady")
        assert s._account_degraded_message == "SolClient NotReady"

    def test_set_healthy_clears_degraded_reason(self) -> None:
        s = ChannelSafetyState()
        s.set_account_degraded(AccountDegradedReason.SHIOAJI_CONNECTION_ERROR)
        s.set_account_healthy()
        assert s.account_degraded is False
        assert s.account_degraded_reason == AccountDegradedReason.NONE

    def test_reset_reconciled_blocks_entry(self) -> None:
        s = ChannelSafetyState()
        s.reset_reconciled()
        s.set_reconciled()
        s.set_account_healthy()
        assert s.entry_allowed() is True
        s.reset_reconciled()
        assert s.entry_allowed() is False
        assert "RECONCILIATION_PENDING" in (s.entry_blocked_reason or "")

    def test_snapshot_contains_all_keys(self) -> None:
        s = ChannelSafetyState()
        snap = s.snapshot()
        expected_keys = {
            "quote_health", "account_health", "order_health",
            "account_degraded_reason", "account_degraded_at",
            "reconciled_after_restart", "reconciled_at",
            "entry_allowed", "entry_blocked_reason",
        }
        assert expected_keys.issubset(snap.keys())

    def test_singleton_shared_across_calls(self) -> None:
        reset_safety_state()
        s1 = get_safety_state()
        s2 = get_safety_state()
        assert s1 is s2

    def test_reset_creates_new_singleton(self) -> None:
        reset_safety_state()
        s1 = get_safety_state()
        reset_safety_state()
        s2 = get_safety_state()
        assert s1 is not s2

    def test_entry_blocked_reason_none_when_allowed(self) -> None:
        s = ChannelSafetyState()
        s.reset_reconciled()
        s.set_reconciled()
        s.set_account_healthy()
        s.entry_allowed()
        assert s.entry_blocked_reason is None

    def test_entry_blocked_reason_has_detail_when_blocked(self) -> None:
        s = ChannelSafetyState()
        s.reset_reconciled()
        s.set_reconciled()
        s.set_account_degraded(AccountDegradedReason.SHIOAJI_CONNECTION_ERROR)
        s.entry_allowed()
        assert s.entry_blocked_reason is not None
        assert "SHIOAJI_CONNECTION_ERROR" in s.entry_blocked_reason
        assert "ACCOUNT_DEGRADED" in s.entry_blocked_reason
