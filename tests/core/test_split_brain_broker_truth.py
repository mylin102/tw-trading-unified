"""RED: the split-brain reset must consult BROKER truth, never local fills alone.

Live 2026-09-24: the app reset its position to FLAT (MTS_SPLIT_BRAIN_RESET)
because the local fills ledger said closed — the fills were closed only because a
negative spread price had been rejected — while the broker held the spread
(TMFJ6 Sell 1 / TMFK6 Buy 1). The system then showed FLAT against broker truth
and quarantined on the next restart.
"""
from types import SimpleNamespace

import pytest

from strategies.futures.monitor import FuturesMonitor


def _auth(status):
    return SimpleNamespace(status=SimpleNamespace(value=status))


@pytest.mark.parametrize("status,expected", [
    ("FLAT", True),          # the only status that may erase a local position
    ("OPEN", False),
    ("SINGLE_LEG", False),
    ("SPREAD", False),
    ("UNKNOWN", False),
    (None, False),
])
def test_only_a_broker_flat_proof_allows_the_reset(status, expected):
    assert FuturesMonitor._broker_truth_is_flat(_auth(status)) is expected


def test_missing_broker_truth_is_not_a_flat_proof():
    for value in (None, SimpleNamespace(), SimpleNamespace(status=None),
                  SimpleNamespace(status=SimpleNamespace(value=None))):
        assert FuturesMonitor._broker_truth_is_flat(value) is False
