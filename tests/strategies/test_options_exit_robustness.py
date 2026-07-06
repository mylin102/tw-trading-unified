import pytest
from unittest.mock import MagicMock, patch
from strategies.options.live_options_squeeze_monitor import ShioajiOptionsSmartMonitor

class MockContract:
    def __init__(self, code):
        self.code = code
        self.delivery_date = "2026-06-17"

@pytest.fixture
def monitor():
    with patch('strategies.options.live_options_squeeze_monitor.ShioajiOptionsSmartMonitor.load_config') as mock_load:
        mock_config = {
            'active_mode': 'PRO',
            'modes': {'PRO': {'tp1_pct': 0.5}},
            'exit_optimization': {'eod_panic_time': '13:30', 'eod_passive_window_mins': 20},
            'strategy': {'entry_score': 15, 'cooldown_bars': 5},
            'execution': {'broker_fee_per_side': 20, 'exchange_fee_per_side': 5},
            'pricing': {'tax_rate': 0.001, 'pricing_model': 'black_scholes'}
        }
        mock_load.return_value = mock_config
        m = ShioajiOptionsSmartMonitor(dry_run=True)
        m.ledger_path = MagicMock()
        m._record_paper_order = MagicMock()
        return m

def test_exit_is_idempotent_under_double_tick(monitor, monkeypatch):
    """
    Test that even if a second exit call happens during log_trade (IO), 
    the position is already zeroed and reentrancy guard is active, 
    preventing double exit.
    """
    monitor.position = 1
    monitor.active_side = "C"
    monitor.entry_price = 100.0
    
    log_calls = []

    def fake_log_trade(*args, **kwargs):
        log_calls.append(kwargs)
        # Simulate a second tick triggering exit while the first one is still logging
        # This is where the reentrancy guard or the immediate position=0 should save us
        monitor.exit_paper_position("SECOND_TICK_EXIT", price=110.0, note="second tick")

    monkeypatch.setattr(monitor, "log_trade", fake_log_trade)

    # First trigger
    monitor.exit_paper_position("FIRST_TICK_EXIT", price=110.0, note="first tick")

    # Verification
    assert monitor.position == 0
    # Should only have 1 log_trade call because:
    # 1. The first call set _exit_in_progress = True
    # 2. The second call returned early because _exit_in_progress is True
    # Even without the guard, the second call would return because monitor.position was set to 0 FIRST.
    assert len(log_calls) == 1
    assert log_calls[0]["quantity"] == 1

def test_apply_paper_tp1_partial_reduction(monitor, monkeypatch):
    """
    Test that apply_paper_tp1 reduces position by 1 and snapshot captures correct values.
    """
    monitor.position = 2
    monitor.active_side = "C"
    monitor.entry_price = 100.0
    
    log_calls = []
    monkeypatch.setattr(monitor, "log_trade", lambda *args, **kwargs: log_calls.append(kwargs))

    monitor.apply_paper_tp1(price=120.0, note="tp1 hit")

    assert monitor.position == 1
    assert monitor.has_tp1_hit is True
    assert len(log_calls) == 1
    assert log_calls[0]["quantity"] == 1

def test_apply_paper_tp1_full_closure_clears_state(monitor, monkeypatch):
    """
    Test that if apply_paper_tp1 reduces position to 0, it clears all metadata.
    """
    monitor.position = 1
    monitor.active_side = "P"
    monitor.entry_price = 50.0
    
    monkeypatch.setattr(monitor, "log_trade", MagicMock())

    monitor.apply_paper_tp1(price=60.0, note="final exit")

    assert monitor.position == 0
    assert monitor.active_side is None
    assert monitor.entry_price == 0.0
    assert monitor.has_tp1_hit is False

def test_exit_spread_idempotency(monitor, monkeypatch):
    """
    Test vertical spread exit idempotency.
    """
    monitor.position = 1
    monitor.active_side = "C"
    monitor._current_spread = {"net_debit": 30.0, "option_type": "Call", "max_profit": 70.0}
    
    log_calls = []
    def fake_log_trade(*args, **kwargs):
        log_calls.append(kwargs)
        monitor._exit_spread_paper_position("SECOND_TRIGGER", exit_value=40.0)

    monkeypatch.setattr(monitor, "log_trade", fake_log_trade)

    monitor._exit_spread_paper_position("FIRST_TRIGGER", exit_value=40.0)

    assert monitor.position == 0
    assert monitor._current_spread is None
    assert len(log_calls) == 1
    assert log_calls[0]["quantity"] == 1
    assert log_calls[0]["entry_price_override"] == 30.0

def test_exit_order_rejected_keeps_position(monitor):
    """
    Test that if the order intent (paper_order) fails, 
    the position is kept for retry on the next tick.
    """
    monitor.position = 1
    monitor.active_side = "C"
    # Force _record_paper_order to fail
    monitor._record_paper_order = MagicMock(return_value=None)
    
    monitor.exit_paper_position("REJECTED_EXIT", price=110.0)

    # Position should still be 1
    assert monitor.position == 1
    assert monitor.active_side == "C"

def test_exit_order_accepted_clears_position_before_log_trade(monitor, monkeypatch):
    """
    Test that once the order intent is accepted, the position is zeroed 
    strictly BEFORE log_trade is called.
    """
    monitor.position = 1
    monitor.active_side = "C"
    monitor._record_paper_order = MagicMock(return_value={"order_id": "MOCK-123"})
    
    def fake_log_trade(*args, **kwargs):
        # The core assertion: position must be 0 when logging starts
        assert monitor.position == 0

    monkeypatch.setattr(monitor, "log_trade", fake_log_trade)

    monitor.exit_paper_position("ACCEPTED_EXIT", price=110.0)

    assert monitor.position == 0
