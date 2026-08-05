# Phase 7 tests: broker health classification + state machine (fault injection).
# 2026-08-05. Pure unit tests — no broker, no I/O, no runtime touch.
import sys
import os
import types
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# build a fake shioaji module with the exception hierarchy
_sj = types.ModuleType("shioaji")


class ShioajiError(Exception):
    pass


class AuthError(ShioajiError):
    pass


class TokenError(AuthError):
    pass


class AccountNotSignError(ShioajiError):
    pass


class SystemMaintenance(ShioajiError):
    pass


class ServerError(ShioajiError):
    pass


class BadRequestError(ShioajiError):
    pass


class ValidationError(ShioajiError):
    pass


class ShioajiTimeoutError(ShioajiError):
    pass


class DecodeError(ShioajiError):
    pass


for _n in ("ShioajiError", "AuthError", "TokenError", "AccountNotSignError",
           "SystemMaintenance", "ServerError", "BadRequestError",
           "ValidationError", "ShioajiTimeoutError", "DecodeError"):
    setattr(_sj, _n, globals()[_n])
sys.modules["shioaji"] = _sj

from core.broker_health import (
    BrokerHealthTracker, classify_broker_error, BrokerErrorClass, HealthState,
)


# ── classification ───────────────────────────────────────────────────────
def test_classify_server_500_is_transient():
    assert classify_broker_error(ServerError("list_positions: code: 500")) \
        == BrokerErrorClass.TRANSIENT_SERVER_5XX


def test_classify_auth_is_auth():
    assert classify_broker_error(TokenError("token expired")) \
        == BrokerErrorClass.AUTHENTICATION_FAILURE


def test_classify_timeout_is_network_timeout():
    assert classify_broker_error(ShioajiTimeoutError("timeout")) \
        == BrokerErrorClass.NETWORK_TIMEOUT


def test_classify_connection_error():
    assert classify_broker_error(ConnectionError("reset")) \
        == BrokerErrorClass.CONNECTION_RESET


def test_classify_session_closed():
    e = ShioajiError("session closed")
    assert classify_broker_error(e) == BrokerErrorClass.SDK_SESSION_CLOSED


def test_classify_unknown():
    assert classify_broker_error(RuntimeError("weird")) \
        == BrokerErrorClass.UNKNOWN_BROKER_ERROR


# ── state machine ────────────────────────────────────────────────────────
def test_single_500_does_not_exit_or_degrade():
    t = BrokerHealthTracker()
    st = t.record_failure(ServerError("500"))
    assert st == HealthState.TRANSIENT_FAILURE
    assert t.state != HealthState.SESSION_INVALID
    assert not t.should_restart_process()


def test_two_500s_degrade_but_not_session_dead():
    t = BrokerHealthTracker()
    t.record_failure(ServerError("500"))
    st = t.record_failure(ServerError("500"))
    assert st == HealthState.DEGRADED
    assert not t.should_restart_process()


def test_500_then_success_recovers_healthy():
    t = BrokerHealthTracker()
    t.record_failure(ServerError("500"))
    t.record_failure(ServerError("500"))
    st = t.record_success()
    assert st == HealthState.HEALTHY
    assert t.consecutive_failures == 0
    assert not t.entry_blocked


def test_timeout_then_success_resets_counter():
    t = BrokerHealthTracker()
    t.record_failure(ShioajiTimeoutError("t"))
    t.record_failure(ShioajiTimeoutError("t"))
    t.record_success()
    assert t.consecutive_failures == 0
    assert t.state == HealthState.HEALTHY


def test_auth_failure_enters_session_invalid():
    t = BrokerHealthTracker()
    st = t.record_failure(TokenError("expired"))
    assert st == HealthState.SESSION_INVALID
    assert t.entry_blocked
    assert t.needs_relogin()


def test_malformed_response_never_assumed_flat():
    # decode error on list_positions -> MALFORMED; position query unavailable
    # must NOT be treated as flat. Tracker only blocks entry — the "not flat"
    # invariant is enforced by callers not calling list_positions success path.
    t = BrokerHealthTracker()
    st = t.record_failure(DecodeError("decode"))
    assert st == HealthState.TRANSIENT_FAILURE or st == HealthState.DEGRADED
    assert t.entry_blocked


def test_backoff_bounded_and_grows():
    t = BrokerHealthTracker()
    t.record_failure(ServerError("500"))
    b1 = t.next_backoff_s()
    t.record_failure(ServerError("500"))
    b2 = t.next_backoff_s()
    t.record_failure(ServerError("500"))
    b3 = t.next_backoff_s()
    assert b1 <= b2 <= b3
    assert b3 <= 60.0 * 1.2  # max_backoff + jitter


def test_recovery_worker_single_flight():
    t = BrokerHealthTracker()
    g1 = t.begin_recovery()
    g2 = t.begin_recovery()
    assert g1 is not None
    assert g2 is None  # single worker
    assert t.recovery_active


def test_stale_recovery_does_not_overwrite():
    t = BrokerHealthTracker()
    g1 = t.begin_recovery()
    t.finish_recovery(g1, ok=True)   # recovered at gen 1
    g2 = t.begin_recovery()
    t.finish_recovery(g2, ok=False)  # failed at gen 2
    assert t.state == HealthState.DEGRADED
    # now a stale success from gen 1 must not overwrite DEGRADED
    t.finish_recovery(1, ok=True)
    assert t.state == HealthState.DEGRADED


def test_restart_only_after_relogin_exhaustion():
    t = BrokerHealthTracker()
    t.record_failure(TokenError("expired"))   # SESSION_INVALID
    assert not t.should_restart_process()
    t.escalate_to_restart()                    # relogin exhausted
    assert t.should_restart_process()


def test_2_generic_failures_never_restart():
    t = BrokerHealthTracker()
    t.record_failure(ServerError("500"))
    t.record_failure(ServerError("500"))
    assert not t.should_restart_process()


def test_recovery_failure_keeps_entry_blocked():
    t = BrokerHealthTracker()
    t.record_failure(ServerError("500"))
    t.record_failure(ServerError("500"))
    g = t.begin_recovery()
    t.finish_recovery(g, ok=False)
    assert t.entry_blocked
    assert t.state == HealthState.DEGRADED
