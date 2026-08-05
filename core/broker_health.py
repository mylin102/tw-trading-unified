"""Broker health classification and recovery state machine (2026-08-05).

ADR-broker-health-recovery-reconciliation-DRAFT implementation (Phase 2-4).

Pure logic, no I/O, no sleeps — unit-testable with fault injection.
Runtime wiring (main.py api_is_healthy replacement) is a SEPARATE commit
after state-machine review; this module only classifies and tracks state.
"""
from __future__ import annotations

import enum
import time
import random
import threading
from dataclasses import dataclass, field


class BrokerErrorClass(str, enum.Enum):
    AUTHENTICATION_FAILURE = "AUTHENTICATION_FAILURE"
    AUTHORIZATION_FAILURE = "AUTHORIZATION_FAILURE"
    REQUEST_VALIDATION_FAILURE = "REQUEST_VALIDATION_FAILURE"
    TRANSIENT_SERVER_5XX = "TRANSIENT_SERVER_5XX"
    RATE_LIMITED = "RATE_LIMITED"
    MAINTENANCE = "MAINTENANCE"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    CONNECTION_RESET = "CONNECTION_RESET"
    SDK_SESSION_CLOSED = "SDK_SESSION_CLOSED"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    UNKNOWN_BROKER_ERROR = "UNKNOWN_BROKER_ERROR"


class HealthState(str, enum.Enum):
    HEALTHY = "HEALTHY"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    DEGRADED = "DEGRADED"
    SESSION_INVALID = "SESSION_INVALID"
    RECOVERING = "RECOVERING"
    RECOVERED = "RECOVERED"
    PROCESS_RESTART_REQUIRED = "PROCESS_RESTART_REQUIRED"


# ── per-class policy ─────────────────────────────────────────────────────
# (retryable, safe, initial_backoff_s, max_backoff_s, max_consecutive,
#  needs_relogin, needs_restart)
_CLASS_POLICY = {
    BrokerErrorClass.AUTHENTICATION_FAILURE: (False, False, 0, 0, 1, True, True),
    BrokerErrorClass.AUTHORIZATION_FAILURE: (False, False, 0, 0, 1, True, True),
    BrokerErrorClass.REQUEST_VALIDATION_FAILURE: (False, False, 0, 0, 1, False, False),
    BrokerErrorClass.TRANSIENT_SERVER_5XX: (True, True, 5, 60, 6, False, False),
    BrokerErrorClass.RATE_LIMITED: (True, True, 15, 120, 4, False, False),
    BrokerErrorClass.MAINTENANCE: (True, True, 60, 300, 10, False, False),
    BrokerErrorClass.NETWORK_TIMEOUT: (True, True, 5, 60, 6, False, False),
    BrokerErrorClass.CONNECTION_RESET: (True, True, 10, 120, 5, True, False),
    BrokerErrorClass.SDK_SESSION_CLOSED: (False, False, 30, 60, 3, True, False),
    BrokerErrorClass.MALFORMED_RESPONSE: (True, True, 5, 30, 3, False, False),
    BrokerErrorClass.UNKNOWN_BROKER_ERROR: (True, False, 15, 60, 3, False, False),
}


def classify_broker_error(exc: BaseException) -> BrokerErrorClass:
    """Classify an exception from Shioaji list_positions / order calls.

    Uses class-name + message heuristics (deliberately NOT isinstance against
    shioaji classes — module identity breaks under fakes and import order).
    Never raises.
    """
    name = type(exc).__name__
    msg = str(exc)
    mod = type(exc).__module__ or ""

    if name in ("TokenError", "AuthError", "AccountNotSignError", "AccountNotProvideError"):
        return BrokerErrorClass.AUTHENTICATION_FAILURE
    if name in ("SystemMaintenance",):
        return BrokerErrorClass.MAINTENANCE
    if name in ("ServerError",):
        # HTTP 5xx -> transient server; 429 rate limit
        if "429" in msg or "rate" in msg.lower():
            return BrokerErrorClass.RATE_LIMITED
        return BrokerErrorClass.TRANSIENT_SERVER_5XX
    if name in ("BadRequestError", "ValidationError"):
        return BrokerErrorClass.REQUEST_VALIDATION_FAILURE
    if "Timeout" in name:
        return BrokerErrorClass.NETWORK_TIMEOUT
    if "ConnectionError" in name or "ConnectionReset" in name or isinstance(exc, OSError):
        return BrokerErrorClass.CONNECTION_RESET
    if "session" in msg.lower() and ("closed" in msg.lower() or "invalid" in msg.lower()):
        return BrokerErrorClass.SDK_SESSION_CLOSED
    if "DecodeError" in name or "ResponseError" in name or "decode" in msg.lower():
        return BrokerErrorClass.MALFORMED_RESPONSE
    return BrokerErrorClass.UNKNOWN_BROKER_ERROR


def policy_for(cls: BrokerErrorClass):
    return _CLASS_POLICY.get(cls, _CLASS_POLICY[BrokerErrorClass.UNKNOWN_BROKER_ERROR])


@dataclass
class BrokerHealthTracker:
    """Thread-safe broker health state machine.

    Invariants:
    - transient failure != session death
    - position query unavailable != flat
    - recovery failure never clears strategy position state
    - one recovery worker at a time (generation-guarded)
    - stale recovery result never overwrites newer healthy state
    """
    max_consecutive_degraded: int = 2
    backoff_base: float = 5.0
    backoff_max: float = 60.0
    jitter: float = 0.2
    relogin_max_attempts: int = 3

    state: HealthState = HealthState.HEALTHY
    consecutive_failures: int = 0
    last_error_class: BrokerErrorClass | None = None
    last_error_msg: str | None = None
    generation: int = 0          # incremented per recovery cycle
    recovery_active: bool = False
    recovered_generation: int = -1
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    entry_blocked: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ── reporting ────────────────────────────────────────────────────────
    def record_failure(self, exc: BaseException, has_position: bool = False) -> HealthState:
        cls = classify_broker_error(exc)
        with self._lock:
            self.last_error_class = cls
            self.last_error_msg = str(exc)[:200]
            self.last_failure_at = time.time()
            self.consecutive_failures += 1
            retryable, safe, *_ = policy_for(cls)
            if not retryable:
                if cls in (BrokerErrorClass.AUTHENTICATION_FAILURE,
                           BrokerErrorClass.AUTHORIZATION_FAILURE,
                           BrokerErrorClass.SDK_SESSION_CLOSED):
                    self.state = HealthState.SESSION_INVALID
                else:
                    self.state = HealthState.DEGRADED
            elif self.consecutive_failures >= self.max_consecutive_degraded:
                self.state = HealthState.DEGRADED
            else:
                self.state = HealthState.TRANSIENT_FAILURE
            if self.state != HealthState.HEALTHY:
                self.entry_blocked = True
            return self.state

    def record_success(self) -> HealthState:
        with self._lock:
            self.consecutive_failures = 0
            self.last_success_at = time.time()
            self.state = HealthState.HEALTHY
            self.entry_blocked = False
            self.last_error_class = None
            return self.state

    # ── retry scheduling ─────────────────────────────────────────────────
    def next_backoff_s(self) -> float:
        """Bounded exponential backoff + jitter for the CURRENT failure count."""
        with self._lock:
            cls = self.last_error_class or BrokerErrorClass.TRANSIENT_SERVER_5XX
            _, _, init_bo, max_bo, max_consec, _, _ = policy_for(cls)
            n = min(self.consecutive_failures, max_consec)
            base = min(init_bo * (2 ** (n - 1)), max_bo) if n > 0 else init_bo
            j = 1.0 + random.uniform(-self.jitter, self.jitter)
            return max(1.0, base * j)

    # ── recovery worker guard (Phase 4) ──────────────────────────────────
    def begin_recovery(self) -> int | None:
        """Claim the single recovery worker. Returns generation id or None."""
        with self._lock:
            if self.recovery_active:
                return None
            self.recovery_active = True
            self.generation += 1
            self.state = HealthState.RECOVERING
            return self.generation

    def finish_recovery(self, generation: int, ok: bool) -> None:
        with self._lock:
            self.recovery_active = False
            if generation <= self.recovered_generation:
                return  # stale/repeat — never overwrite newer state
            if ok:
                self.recovered_generation = generation
                self.consecutive_failures = 0
                self.state = HealthState.RECOVERED
                self.entry_blocked = False
            else:
                self.state = HealthState.DEGRADED
                self.entry_blocked = True

    def needs_relogin(self) -> bool:
        with self._lock:
            if self.last_error_class is None:
                return False
            _, _, _, _, _, needs_relogin, _ = policy_for(self.last_error_class)
            return bool(needs_relogin)

    def should_restart_process(self) -> bool:
        """PROCESS_RESTART_REQUIRED only after relogin exhaustion."""
        with self._lock:
            return self.state == HealthState.PROCESS_RESTART_REQUIRED

    def escalate_to_restart(self) -> None:
        with self._lock:
            self.state = HealthState.PROCESS_RESTART_REQUIRED
            self.entry_blocked = True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state.value,
                "consecutive_failures": self.consecutive_failures,
                "last_error_class": self.last_error_class.value if self.last_error_class else None,
                "generation": self.generation,
                "recovery_active": self.recovery_active,
                "recovered_generation": self.recovered_generation,
                "entry_blocked": self.entry_blocked,
                "last_success_at": self.last_success_at,
                "last_failure_at": self.last_failure_at,
            }
