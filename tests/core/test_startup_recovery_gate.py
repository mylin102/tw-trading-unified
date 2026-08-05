"""Fail-closed startup reconciliation tests."""

from types import SimpleNamespace

from core.channel_safety import get_safety_state, reset_safety_state
from main import verify_startup_reconciliation


class FakeBroker:
    def __init__(self, positions=(), trades=(), failure=None):
        self.futopt_account = object()
        self.positions = list(positions)
        self.trades = list(trades)
        self.failure = failure
        self.update_calls = 0

    def update_status(self, account):
        self.update_calls += 1
        if self.failure:
            raise self.failure

    def list_positions(self, account):
        if self.failure:
            raise self.failure
        return self.positions

    def list_trades(self):
        if self.failure:
            raise self.failure
        return self.trades


def trade(status):
    return SimpleNamespace(status=SimpleNamespace(status=status))


def setup_function():
    reset_safety_state()


def test_flat_broker_state_unlocks_after_read_only_reconciliation():
    broker = FakeBroker(trades=[trade("Filled"), trade("Cancelled")])

    assert verify_startup_reconciliation(broker) is True
    safety = get_safety_state()
    assert broker.update_calls == 1
    assert safety.reconciled is True
    assert safety.entry_allowed() is True


def test_open_broker_position_keeps_entry_blocked():
    broker = FakeBroker(positions=[SimpleNamespace(code="TMF", quantity=1)])

    assert verify_startup_reconciliation(broker) is False
    safety = get_safety_state()
    assert safety.reconciled is False
    assert safety.entry_allowed() is False
    assert "RECONCILIATION_PENDING" in (safety.entry_blocked_reason or "")


def test_open_order_keeps_entry_blocked():
    broker = FakeBroker(trades=[trade("Submitted")])

    assert verify_startup_reconciliation(broker) is False
    assert get_safety_state().entry_allowed() is False


def test_broker_query_failure_keeps_entry_blocked():
    broker = FakeBroker(failure=RuntimeError("broker unavailable"))

    assert verify_startup_reconciliation(broker) is False
    safety = get_safety_state()
    assert safety.reconciled is False
    assert safety.entry_allowed() is False
