"""
mode_transition.py — Paper → Live 模式轉換模型 (P0-A).

定義三個分離的概念：
  - requested_mode: 使用者意圖 (來自 config)
  - effective_mode: 系統實際運行的模式狀態 (FSM 收斂結果)
  - live_order_allowed: 最終授權 (所有 gate 通過後才為 True)

核心不變量:
  requested_mode == LIVE 不代表 effective_mode == LIVE_READY
  更不代表 live_order_allowed == True
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ExecutionMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class ModeTransitionState(str, Enum):
    """Mode transition FSM states.

    Flow: PAPER_ACTIVE → PAPER_DRAINING → PAPER_DRAINED
          → LIVE_PREFLIGHT → LIVE_RECONCILING
          → LIVE_READY (success) or LIVE_QUARANTINED (failure)

    After broker preflight passes but before commit:
          PAPER_DRAINED → READY_FOR_COMMIT

    Any failure → TRANSITION_BLOCKED
    """
    PAPER_ACTIVE = "paper_active"
    PAPER_DRAINING = "paper_draining"
    PAPER_DRAINED = "paper_drained"
    READY_FOR_COMMIT = "ready_for_commit"
    LIVE_PREFLIGHT = "live_preflight"
    LIVE_RECONCILING = "live_reconciling"
    LIVE_QUARANTINED = "live_quarantined"
    LIVE_READY = "live_ready"
    TRANSITION_BLOCKED = "transition_blocked"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LiveOrderBlocked(Exception):
    """Raised when a live order is attempted but transition is not complete."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"LIVE_ORDER_BLOCKED: {reason}")


class EntryBlocked(Exception):
    """Raised when a paper entry is blocked because drain is in progress.

    Raised during PAPER_DRAINING or PAPER_DRAINED — new entries
    cannot start while a drain is active.
    """
    def __init__(self, reason: str = "PAPER_DRAIN_IN_PROGRESS"):
        self.reason = reason
        super().__init__(f"ENTRY_BLOCKED: {reason}")


class DrainBlocked(Exception):
    """Raised when paper drain cannot proceed because conditions are not met."""
    def __init__(self, reason: str, failed_checks: tuple[str, ...] = ()):
        self.reason = reason
        self.failed_checks = failed_checks
        super().__init__(f"DRAIN_BLOCKED: {reason}")


class ModeStateMismatchError(Exception):
    """Raised when state file mode does not match requested execution mode."""
    pass


# ---------------------------------------------------------------------------
# Execution Context
# ---------------------------------------------------------------------------


def _generate_process_start_id() -> str:
    """Generate a unique process start identifier.

    Combines hostname, PID, and boot time for uniqueness across restarts.
    PM2 restart always produces a new ID.
    """
    hostname = platform.node() or "unknown"
    pid = os.getpid()
    try:
        boot_time = subprocess.check_output(
            ["sysctl", "-n", "kern.boottime"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        boot_time = datetime.now().isoformat()
    raw = f"{hostname}:{pid}:{boot_time}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _hash_account_id(account_id: str | None) -> str | None:
    """Hash account ID for state metadata. Never store raw account IDs."""
    if account_id is None:
        return None
    return hashlib.sha256(account_id.encode()).hexdigest()


def _hash_config(config_yaml: str | None) -> str | None:
    """Hash config content for state metadata."""
    if config_yaml is None:
        return None
    return hashlib.sha256(config_yaml.encode()).hexdigest()


@dataclass(frozen=True)
class ExecutionContext:
    """Immutable execution context binding.

    All components (OrderManager, PositionProvider, StateStore, etc.)
    must share the same ExecutionContext.  Mode/namespace/account
    inconsistency causes startup failure.
    """
    requested_mode: str                    # "paper" | "live"  from config
    effective_mode: str                    # current FSM state
    live_order_allowed: bool = False       # True only after full transition

    # Identity
    account_id_hash: str | None = None
    session_id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d_%H%M%S"))
    process_start_id: str = field(default_factory=_generate_process_start_id)
    config_hash: str | None = None

    # Namespace
    state_namespace: str = "paper"         # "paper" | "live"

    def is_live_ready(self) -> bool:
        """Shorthand: fully LIVE with authorization."""
        return (
            self.requested_mode == ExecutionMode.LIVE.value
            and self.effective_mode == ModeTransitionState.LIVE_READY.value
            and self.live_order_allowed
        )

    def assert_live_order_allowed(self) -> None:
        """Raise LiveOrderBlocked if conditions are not met.

        This is the central hard gate — re-verifies full context
        rather than depending on a single boolean flag.
        """
        if self.requested_mode != ExecutionMode.LIVE.value:
            return  # paper mode: not a live order

        if self.effective_mode != ModeTransitionState.LIVE_READY.value:
            raise LiveOrderBlocked(
                f"EFFECTIVE_MODE_NOT_LIVE_READY (effective={self.effective_mode})"
            )

        if not self.live_order_allowed:
            raise LiveOrderBlocked("LIVE_ORDER_FLAG_FALSE")

    def assert_entry_allowed(self) -> None:
        """Raise EntryBlocked if paper drain is in progress.

        During PAPER_DRAINING or PAPER_DRAINED, new entries are
        blocked to prevent new positions from opening while a
        drain is active.
        """
        if self.effective_mode in (
            ModeTransitionState.PAPER_DRAINING.value,
            ModeTransitionState.PAPER_DRAINED.value,
            ModeTransitionState.READY_FOR_COMMIT.value,
        ):
            raise EntryBlocked(
                f"PAPER_DRAIN_IN_PROGRESS (effective={self.effective_mode})"
            )

    def to_dict(self) -> dict:
        return {
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "live_order_allowed": self.live_order_allowed,
            "account_id_hash": self.account_id_hash,
            "session_id": self.session_id,
            "process_start_id": self.process_start_id,
            "config_hash": self.config_hash,
            "state_namespace": self.state_namespace,
        }


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def paper_context(
    account_id: str | None = None,
    config_yaml: str | None = None,
) -> ExecutionContext:
    """Create a paper-mode ExecutionContext."""
    return ExecutionContext(
        requested_mode=ExecutionMode.PAPER.value,
        effective_mode=ModeTransitionState.PAPER_ACTIVE.value,
        live_order_allowed=False,
        account_id_hash=_hash_account_id(account_id) if account_id else None,
        config_hash=_hash_config(config_yaml) if config_yaml else None,
        state_namespace="paper",
    )


def live_preflight_context(
    account_id: str | None = None,
    config_yaml: str | None = None,
) -> ExecutionContext:
    """Create a live-mode ExecutionContext in preflight state.

    Effective mode starts at LIVE_PREFLIGHT — no live orders allowed
    until transition completes to LIVE_READY.
    """
    return ExecutionContext(
        requested_mode=ExecutionMode.LIVE.value,
        effective_mode=ModeTransitionState.LIVE_PREFLIGHT.value,
        live_order_allowed=False,
        account_id_hash=_hash_account_id(account_id) if account_id else None,
        config_hash=_hash_config(config_yaml) if config_yaml else None,
        state_namespace="live",
    )


def with_effective_mode(
    ctx: ExecutionContext,
    new_mode: str | ModeTransitionState,
    live_order_allowed: bool | None = None,
) -> ExecutionContext:
    """Create a new ExecutionContext with updated effective_mode.

    Since ExecutionContext is frozen, this is the only way to change
    the transition state.  Returns a new instance; the original is
    unchanged.

    Validates new_mode is a known ModeTransitionState value.
    """
    mode_value = (
        new_mode.value if isinstance(new_mode, ModeTransitionState) else new_mode
    )

    # Validate: must be a known ModeTransitionState
    known_values = {s.value for s in ModeTransitionState}
    if mode_value not in known_values:
        raise ValueError(
            f"Invalid effective_mode '{mode_value}'. "
            f"Must be one of: {', '.join(sorted(known_values))}"
        )

    updates: dict = {"effective_mode": mode_value}
    if live_order_allowed is not None:
        updates["live_order_allowed"] = live_order_allowed

    return replace(ctx, **updates)


# ---------------------------------------------------------------------------
# Paper Drain Model
# ---------------------------------------------------------------------------

PAPER_DRAIN_DEFAULT_TIMEOUT_SECONDS: int = 600  # 10 minutes


@dataclass(frozen=True)
class PaperDrainSnapshot:
    """Immutable snapshot of paper state for drain evaluation.

    All fields are pre-decision — captured before evaluating whether
    drain is complete.
    """
    position_qty: int = 0
    lifecycle_phase: str = "FLAT"
    pending_order_count: int = 0
    inflight_callback_count: int = 0
    active_trade_id: str | None = None
    pending_action: str | None = None
    unresolved_fill_count: int = 0


@dataclass(frozen=True)
class PaperDrainResult:
    """Result of evaluating whether paper drain is complete."""
    drained: bool
    failed_checks: tuple[str, ...] = ()
    snapshot: PaperDrainSnapshot | None = None


# Known drain failure reasons
DRAIN_FAIL_POSITION_NONZERO = "PAPER_POSITION_NOT_FLAT"
DRAIN_FAIL_LIFECYCLE_NOT_FLAT = "PAPER_LIFECYCLE_NOT_FLAT"
DRAIN_FAIL_PENDING_ORDERS = "PAPER_PENDING_ORDERS_EXIST"
DRAIN_FAIL_CALLBACKS_INFLIGHT = "PAPER_CALLBACKS_INFLIGHT"
DRAIN_FAIL_ACTIVE_TRADE = "PAPER_ACTIVE_TRADE_EXISTS"
DRAIN_FAIL_PENDING_ACTION = "PAPER_PENDING_ACTION"
DRAIN_FAIL_UNRESOLVED_FILLS = "PAPER_LEDGER_UNRESOLVED"
DRAIN_FAIL_TIMEOUT = "PAPER_DRAIN_TIMEOUT"


def evaluate_paper_drain(
    snapshot: PaperDrainSnapshot,
) -> PaperDrainResult:
    """Pure function: is the paper drain complete?

    All conditions must be satisfied for drain to complete.
    Returns a PaperDrainResult with:
    - drained=True if all conditions pass
    - drained=False + failed_checks listing every unmet condition
    """
    failed: list[str] = []

    if snapshot.position_qty != 0:
        failed.append(DRAIN_FAIL_POSITION_NONZERO)

    if snapshot.lifecycle_phase != "FLAT":
        failed.append(DRAIN_FAIL_LIFECYCLE_NOT_FLAT)

    if snapshot.pending_order_count > 0:
        failed.append(DRAIN_FAIL_PENDING_ORDERS)

    if snapshot.inflight_callback_count > 0:
        failed.append(DRAIN_FAIL_CALLBACKS_INFLIGHT)

    if snapshot.active_trade_id is not None:
        failed.append(DRAIN_FAIL_ACTIVE_TRADE)

    if snapshot.pending_action is not None:
        failed.append(DRAIN_FAIL_PENDING_ACTION)

    if snapshot.unresolved_fill_count > 0:
        failed.append(DRAIN_FAIL_UNRESOLVED_FILLS)

    return PaperDrainResult(
        drained=len(failed) == 0,
        failed_checks=tuple(failed),
        snapshot=snapshot,
    )


def assert_paper_drained(
    snapshot: PaperDrainSnapshot,
) -> PaperDrainResult:
    """Raise DrainBlocked if paper drain is not complete.

    Separates evaluation (PaperDrainResult) from control flow (exception).
    Use this when active decision-making is needed; use evaluate_paper_drain()
    for pure inspection.
    """
    result = evaluate_paper_drain(snapshot)
    if not result.drained:
        raise DrainBlocked(
            reason="PAPER_DRAIN_INCOMPLETE",
            failed_checks=result.failed_checks,
        )
    return result


def evaluate_paper_drain_timeout(
    started_at: datetime,
    now: datetime,
    timeout_seconds: int = PAPER_DRAIN_DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Check if paper drain has exceeded the allowed timeout.

    Returns True if timed out, False if still within limit.
    """
    elapsed = (now - started_at).total_seconds()
    return elapsed >= timeout_seconds


def cancel_paper_drain(
    ctx: ExecutionContext,
) -> ExecutionContext:
    """Cancel an active paper drain and return to PAPER_ACTIVE.

    Only allowed when:
    - effective_mode is PAPER_DRAINING or PAPER_DRAINED
    - No broker preflight has started (no LIVE_PREFLIGHT+ state)

    Raises ValueError if cancellation is not allowed from the
    current state (e.g. broker preflight already started).
    """
    if ctx.effective_mode not in (
        ModeTransitionState.PAPER_DRAINING.value,
        ModeTransitionState.PAPER_DRAINED.value,
    ):
        raise ValueError(
            f"Cannot cancel drain from state '{ctx.effective_mode}'. "
            f"Must be PAPER_DRAINING or PAPER_DRAINED."
        )

    if ctx.live_order_allowed:
        raise ValueError(
            "Cannot cancel drain after broker preflight has started. "
            "Transition must be rolled back from the live side."
        )

    return with_effective_mode(ctx, ModeTransitionState.PAPER_ACTIVE.value)


# ---------------------------------------------------------------------------
# Broker Preflight Model (PR 3)
# ---------------------------------------------------------------------------

BROKER_PREFLIGHT_DEFAULT_TIMEOUT_SECONDS: int = 30
SNAPSHOT_MAX_AGE_SECONDS: int = 30
SNAPSHOT_MAX_CLOCK_SKEW_SECONDS: int = 5


@dataclass(frozen=True)
class BrokerSnapshot:
    """Immutable snapshot of broker state for preflight evaluation.

    First version: clean-start-only check.  Does NOT include
    lifecycle, fills, trade_id, or strategy mapping.
    """
    connected: bool = False
    authenticated: bool = False
    account_id_hash: str | None = None

    position_count: int = 0
    open_order_count: int = 0

    position_snapshot_time: datetime | None = None
    order_snapshot_time: datetime | None = None


@dataclass(frozen=True)
class BrokerPreflightResult:
    """Result of evaluating whether broker is ready for live transition."""
    passed: bool
    failed_checks: tuple[str, ...] = ()
    snapshot: BrokerSnapshot | None = None


# Known preflight failure reasons
PREFLIGHT_FAIL_NOT_CONNECTED = "BROKER_NOT_CONNECTED"
PREFLIGHT_FAIL_AUTH_FAILED = "BROKER_AUTH_FAILED"
PREFLIGHT_FAIL_ACCOUNT_MISMATCH = "BROKER_ACCOUNT_MISMATCH"
PREFLIGHT_FAIL_POSITION_NOT_FLAT = "BROKER_POSITION_NOT_FLAT"
PREFLIGHT_FAIL_OPEN_ORDERS_EXIST = "BROKER_OPEN_ORDERS_EXIST"
PREFLIGHT_FAIL_POSITION_SNAPSHOT_STALE = "BROKER_POSITION_SNAPSHOT_STALE"
PREFLIGHT_FAIL_ORDER_SNAPSHOT_STALE = "BROKER_ORDER_SNAPSHOT_STALE"
PREFLIGHT_FAIL_TIMEOUT = "BROKER_PREFLIGHT_TIMEOUT"


def _is_snapshot_fresh(
    snapshot_time: datetime | None,
    now: datetime | None = None,
    max_age_seconds: int = SNAPSHOT_MAX_AGE_SECONDS,
    max_clock_skew_seconds: int = SNAPSHOT_MAX_CLOCK_SKEW_SECONDS,
) -> bool:
    """Check if a broker snapshot is within the acceptable age window.

    All datetimes should be UTC-aware.  Future timestamps (beyond allowed
    clock skew) are treated as stale to prevent timezone confusion.
    """
    if snapshot_time is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)

    # Ensure both are UTC-aware for subtraction
    if snapshot_time.tzinfo is None:
        # If snapshot has no tz, assume UTC
        snapshot_time = snapshot_time.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    age = (now - snapshot_time).total_seconds()

    # Future timestamps: clock skew beyond allowed threshold = stale
    if age < -max_clock_skew_seconds:
        return False

    # Age check: allow small clock skew in negative direction
    return -max_clock_skew_seconds <= age <= max_age_seconds


def evaluate_broker_preflight(
    snapshot: BrokerSnapshot,
    expected_account_hash: str | None = None,
) -> BrokerPreflightResult:
    """Pure function: is the broker ready for live transition?

    ALL conditions must pass.  Unknown = Failure.

    Returns BrokerPreflightResult with:
    - passed=True if all conditions pass
    - passed=False + failed_checks listing every unmet condition
    """
    failed: list[str] = []

    if not snapshot.connected:
        failed.append(PREFLIGHT_FAIL_NOT_CONNECTED)

    if not snapshot.authenticated:
        failed.append(PREFLIGHT_FAIL_AUTH_FAILED)

    if expected_account_hash is not None and snapshot.account_id_hash != expected_account_hash:
        failed.append(PREFLIGHT_FAIL_ACCOUNT_MISMATCH)

    # Unknown = Failure: None snapshot times mean "we don't know" = stale
    if snapshot.position_snapshot_time is None:
        failed.append(PREFLIGHT_FAIL_POSITION_SNAPSHOT_STALE)
    elif not _is_snapshot_fresh(snapshot.position_snapshot_time):
        failed.append(PREFLIGHT_FAIL_POSITION_SNAPSHOT_STALE)

    if snapshot.order_snapshot_time is None:
        failed.append(PREFLIGHT_FAIL_ORDER_SNAPSHOT_STALE)
    elif not _is_snapshot_fresh(snapshot.order_snapshot_time):
        failed.append(PREFLIGHT_FAIL_ORDER_SNAPSHOT_STALE)

    if snapshot.position_count > 0:
        failed.append(PREFLIGHT_FAIL_POSITION_NOT_FLAT)

    if snapshot.open_order_count > 0:
        failed.append(PREFLIGHT_FAIL_OPEN_ORDERS_EXIST)

    return BrokerPreflightResult(
        passed=len(failed) == 0,
        failed_checks=tuple(failed),
        snapshot=snapshot,
    )
