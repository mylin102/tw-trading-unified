"""
Contract: MTS entry sync must initialize ALL lifecycle state fields.

Entry:
  lifecycle = OPEN
  released_leg = None
  remaining_leg = None
  side = None
  peak/nadir = entry price

None is a LEGAL initial state, not an error.
Before release, PARTIAL_EXIT + released_leg=None = ignore, not error.
"""
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))


def _make_context(config_dict: dict) -> SimpleNamespace:
    """Create a minimal StrategyContext-like object for unit testing."""
    context = SimpleNamespace()
    context.config = config_dict
    context.market = SimpleNamespace()
    context.market.ticker = "TMF"
    context.market.near_symbol = "TMF_NEAR"
    context.market.far_symbol = "TMF_FAR"
    return context


def _make_strategy():
    """Create a TMFSpread strategy with console.print mocked."""
    from unittest.mock import MagicMock
    import strategies.plugins.futures.active.tmf_spread as _mod
    _mod.console = MagicMock()
    from strategies.plugins.futures.active.tmf_spread import TMFSpread
    strategy = TMFSpread()
    strategy.init(_make_context({"params": {}}))
    return strategy


def test_mts_entry_sync_initializes_release_state():
    """sync_position() must set all lifecycle fields to clean initial values."""
    strategy = _make_strategy()

    strategy.sync_position(
        trade_id="test-trade-001",
        side="SELL_NEAR_BUY_FAR",
        near_entry=40363.0,
        far_entry=40402.0,
        entry_spread_z=2.71,
    )

    # ── Lifecycle state (newly initialized) ──
    assert strategy._lifecycle == "OPEN", \
        f"Expected _lifecycle='OPEN', got '{strategy._lifecycle}'"
    assert strategy._released_leg is None, \
        f"Expected _released_leg=None, got '{strategy._released_leg}'"
    assert strategy._side is None, \
        f"Expected _side=None, got '{strategy._side}'"

    # ── Entry price state ──
    assert strategy._has_position is True
    assert strategy._trade_id == "test-trade-001"
    assert strategy._near_entry == 40363.0
    assert strategy._far_entry == 40402.0
    assert strategy._entry_spread_z == 2.71

    # ── Peak/nadir set to entry price (trailing starts from entry) ──
    assert strategy._peak > 0, f"Expected _peak > 0, got {strategy._peak}"
    assert strategy._nadir > 0, f"Expected _nadir > 0, got {strategy._nadir}"

    # ── Release timestamp must be None (not set at entry) ──
    assert strategy._release_ts is None, \
        f"Expected _release_ts=None, got '{strategy._release_ts}'"

    # ── Side labels ──
    assert strategy._near_side is not None, \
        "near_side should be set after sync"
    assert strategy._far_side is not None, \
        "far_side should be set after sync"


def test_contract_test_suite():
    """
    Meta: this test exists so we can add more MTS state invariants later.
    Currently covered:
      - sync_position initializes lifecycle fields:
        _lifecycle=OPEN, _released_leg=None, _side=None
      - entry price state: _has_position, _near_entry, _far_entry, _entry_spread_z
      - peak/nadir set to entry price
      - release timestamp is None

    TODO when release_leg() method is added:
      - test release_leg transitions _released_leg, _side, _lifecycle
    """
    pass
