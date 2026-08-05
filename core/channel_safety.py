#!/usr/bin/env python3
"""
Partial Connectivity Fail-Safe — degraded state and safety gates.

Tracks channel health independently for Quote, Account, and Order channels.
When account/order channels are degraded, safety gates block new entries
and prevent heartbeat from overwriting authoritative position state.

Design:
  - Module-level singleton for cross-component access
  - Thread-safe via Lock
  - Start state: ACCOUNT_UNKNOWN (after restart, before reconciliation)
  - ACCOUNT_CHANNEL_DEGRADED: Solace NotReady or ShioajiConnectionError
  - ORDER_CHANNEL_DEGRADED: order submission/status failures
  - RECONCILIATION_PENDING: restart occurred, broker state not yet verified
"""

from __future__ import annotations

import json
import os
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any


# ── Channel States ──

class ChannelHealth(str, Enum):
    UNKNOWN = "UNKNOWN"           # Startup / not yet checked
    HEALTHY = "HEALTHY"           # Last check succeeded
    DEGRADED = "DEGRADED"         # Last check failed (e.g., NotReady)
    FAILED = "FAILED"             # Repeated failures / unrecoverable


class AccountDegradedReason(str, Enum):
    NONE = "NONE"
    SHIOAJI_CONNECTION_ERROR = "SHIOAJI_CONNECTION_ERROR"   # Solace NotReady
    LIST_POSITIONS_FAILED = "LIST_POSITIONS_FAILED"         # Generic failure
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"       # Restart, not yet synced


SAFETY_STATUS_PATH = Path(
    os.environ.get("TRADING_SAFETY_STATUS_PATH", "/tmp/tw_trading_safety_state.json")
)


def _write_safety_snapshot(snapshot: dict[str, Any]) -> None:
    """Atomically publish cross-process safety state for read-only consumers."""
    try:
        SAFETY_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = SAFETY_STATUS_PATH.with_name(
            f".{SAFETY_STATUS_PATH.name}.{os.getpid()}.tmp"
        )
        temporary_path.write_text(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
        os.replace(temporary_path, SAFETY_STATUS_PATH)
    except OSError:
        # Status export must never weaken or crash the in-process entry gate.
        return


def read_persisted_safety_snapshot(path: Path | None = None) -> dict[str, Any] | None:
    """Read the trading-process safety snapshot without sharing process memory."""
    try:
        payload = json.loads((path or SAFETY_STATUS_PATH).read_text())
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


# ── Safety State ──

class ChannelSafetyState:
    """Thread-safe channel health and safety gates.

    Usage::

        state = ChannelSafetyState()
        state.set_account_degraded(AccountDegradedReason.SHIOAJI_CONNECTION_ERROR)
        if state.entry_allowed("TMF"):
            submit_entry()
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Channel health
        self._quote_health: ChannelHealth = ChannelHealth.UNKNOWN
        self._account_health: ChannelHealth = ChannelHealth.UNKNOWN
        self._order_health: ChannelHealth = ChannelHealth.UNKNOWN

        # Degraded reasons
        self._account_degraded_reason: AccountDegradedReason = AccountDegradedReason.NONE
        self._account_degraded_at: float | None = None
        self._account_degraded_message: str | None = None

        # Reconciliation
        self._reconciled_after_restart: bool = False
        self._reconciled_at: float | None = None

        # Degraded entry block
        self._entry_blocked_reason: str | None = None
        self._updated_at: float = time.time()
        # Replace any stale status left by a previous process before broker sync.
        self._publish_locked()

    # ── Account Channel ──

    @property
    def account_healthy(self) -> bool:
        with self._lock:
            return self._account_health == ChannelHealth.HEALTHY

    @property
    def account_degraded(self) -> bool:
        with self._lock:
            return self._account_health in (ChannelHealth.DEGRADED, ChannelHealth.FAILED)

    @property
    def account_degraded_reason(self) -> AccountDegradedReason:
        with self._lock:
            return self._account_degraded_reason

    def set_account_healthy(self) -> None:
        with self._lock:
            self._account_health = ChannelHealth.HEALTHY
            self._account_degraded_reason = AccountDegradedReason.NONE
            self._account_degraded_at = None
            self._account_degraded_message = None
            self._publish_locked()

    def set_account_degraded(self, reason: AccountDegradedReason, message: str | None = None) -> None:
        with self._lock:
            self._account_health = ChannelHealth.DEGRADED
            self._account_degraded_reason = reason
            self._account_degraded_at = __import__("time").time()
            self._account_degraded_message = message
            self._publish_locked()

    # ── Reconciliation ──

    @property
    def reconciled(self) -> bool:
        with self._lock:
            return self._reconciled_after_restart

    def set_reconciled(self) -> None:
        with self._lock:
            self._reconciled_after_restart = True
            self._reconciled_at = __import__("time").time()
            self._publish_locked()

    def reset_reconciled(self) -> None:
        """Call on process start before first broker sync."""
        with self._lock:
            self._reconciled_after_restart = False
            self._reconciled_at = None
            self._publish_locked()

    # ── Entry Gate ──

    def entry_allowed(self, ticker: str = "") -> bool:
        """Check if entry is allowed. Returns True if safe to enter."""
        with self._lock:
            if not self._reconciled_after_restart:
                self._entry_blocked_reason = f"RECONCILIATION_PENDING"
                return False
            if self._account_health in (ChannelHealth.DEGRADED, ChannelHealth.FAILED):
                self._entry_blocked_reason = f"ACCOUNT_DEGRADED:{self._account_degraded_reason.value}"
                return False
            self._entry_blocked_reason = None
            return True

    @property
    def entry_blocked_reason(self) -> str | None:
        with self._lock:
            return self._entry_blocked_reason

    # ── Heartbeat Gate ──

    def heartbeat_may_write_position(self) -> bool:
        """Heartbeat may only persist position state if account channel is verified."""
        with self._lock:
            if not self._reconciled_after_restart:
                return False
            if self._account_health in (ChannelHealth.DEGRADED, ChannelHealth.FAILED):
                return False
            return True

    # ── Snapshot ──

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        entry_allowed = (
            self._reconciled_after_restart
            and self._account_health == ChannelHealth.HEALTHY
        )
        blocked_reason = self._entry_blocked_reason
        if not entry_allowed and not blocked_reason:
            if not self._reconciled_after_restart:
                blocked_reason = "RECONCILIATION_PENDING"
            elif self._account_health in (ChannelHealth.DEGRADED, ChannelHealth.FAILED):
                blocked_reason = f"ACCOUNT_DEGRADED:{self._account_degraded_reason.value}"
        return {
                "quote_health": self._quote_health.value,
                "account_health": self._account_health.value,
                "order_health": self._order_health.value,
                "account_degraded_reason": self._account_degraded_reason.value,
                "account_degraded_at": self._account_degraded_at,
                "reconciled_after_restart": self._reconciled_after_restart,
                "reconciled_at": self._reconciled_at,
                "entry_allowed": entry_allowed,
                "entry_blocked_reason": blocked_reason,
                "account_degraded_message": self._account_degraded_message,
                "updated_at": self._updated_at,
                "pid": os.getpid(),
        }

    def _publish_locked(self) -> None:
        self._updated_at = time.time()
        _write_safety_snapshot(self._snapshot_locked())


# ── Module-level singleton ──
_safety_state: ChannelSafetyState | None = None
_safety_lock = threading.Lock()


def get_safety_state() -> ChannelSafetyState:
    """Get or create the singleton safety state."""
    global _safety_state
    if _safety_state is None:
        with _safety_lock:
            if _safety_state is None:
                _safety_state = ChannelSafetyState()
    return _safety_state


def reset_safety_state() -> None:
    """Reset for testing or clean restart."""
    global _safety_state
    with _safety_lock:
        _safety_state = ChannelSafetyState()
