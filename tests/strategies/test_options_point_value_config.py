"""
Unit tests for point_value config sourcing.
Verifies that all PnL and margin calculations read point_value from config
instead of using hardcoded literals.
"""
import pytest
from unittest.mock import MagicMock, patch


# ── Helper: create a monitor with controlled config ──

@pytest.fixture
def monitor_with_point_value():
    """Create monitor with a distinctive point_value for verification."""
    with patch('strategies.options.live_options_squeeze_monitor.ShioajiOptionsSmartMonitor.load_config') as mock_load:
        mock_config = {
            'active_mode': 'V2',
            'modes': {'V2': {'tp1_pct': 0.3}},
            'exit_optimization': {'eod_panic_time': '13:30', 'eod_passive_window_mins': 20},
            'strategy': {'entry_score': 15, 'cooldown_bars': 3},
            'execution': {'broker_fee_per_side': 20, 'exchange_fee_per_side': 5},
            'pricing': {
                'point_value': 99,     # ← distinctive value, not 50
                'tax_rate': 0.001,
                'pricing_model': 'black_scholes',
            },
            'risk_mgmt': {
                'initial_capital': 100000,
                'lots_per_trade': 1,
                'max_positions': 3,
                'stop_loss_pct': 0.1,
            },
        }
        mock_load.return_value = mock_config
        from strategies.options.live_options_squeeze_monitor import ShioajiOptionsSmartMonitor
        m = ShioajiOptionsSmartMonitor(dry_run=True)
        m.ledger_path = MagicMock()
        m._record_paper_order = MagicMock()
        return m


# ── Tests ──

class TestPointValueFromConfig:
    """Verify every code path that uses point_value reads from config."""

    def test_paper_margin_check_uses_config_point_value(self, monitor_with_point_value):
        """
        GIVEN pricing.point_value=99 in config
        WHEN _paper_margin_check calculates required
        THEN it uses 99, not 50
        """
        m = monitor_with_point_value
        m.pricing_cfg = m.full_cfg.get('pricing', {})

        # Mock ledger_path.exists() to return False so available = 100000 * 0.8 = 80000
        m.ledger_path.exists.return_value = False

        # entry_price=800, lots=1 → required = 800 * 99 * 1 = 79200
        # available = 80000, so this should pass
        result = m._paper_margin_check(entry_price=800, lots=1)
        assert result is True, "800*99*1=79200 should be < 80000"

        # entry_price=810, lots=1 → required = 810 * 99 * 1 = 80190
        # available = 80000, so this should fail
        result2 = m._paper_margin_check(entry_price=810, lots=1)
        assert result2 is False, "810*99*1=80190 should be > 80000"

    def test_log_trade_pnl_uses_config_point_value_long(self, monitor_with_point_value):
        """
        GIVEN pricing.point_value=99 in config
        WHEN log_trade calculates PnL for a long exit
        THEN it uses 99, not 50
        """
        from strategies.options.live_options_squeeze_monitor import ShioajiOptionsSmartMonitor

        m = monitor_with_point_value
        m.pricing_cfg = m.full_cfg.get('pricing', {})
        m.position = 0  # will be set by exit_paper_position
        m.active_side = None
        m.entry_price = 0.0
        m._entry_features = {}

        # Set up entry state then do a paper exit
        m.position = 1
        m.active_side = 'C'
        m.entry_price = 100.0

        # Mock log_trade to capture the PnL it computed
        captured = {}

        def capture_log_trade(action, side, price, note='', quantity=None, entry_price_override=None):
            nonlocal captured
            from strategies.options.live_options_squeeze_monitor import ShioajiOptionsSmartMonitor
            # Call real log_trade but capture after it finishes
            trade_id = ShioajiOptionsSmartMonitor.log_trade(
                m, action, side, price, note, quantity, entry_price_override
            )
            # Read the last row from the ledger to verify PnL
            import pandas as pd
            if m.ledger_path.exists():
                df = pd.read_csv(m.ledger_path)
                if len(df) > 0:
                    captured['pnl'] = df['PnL'].iloc[-1]
            return trade_id

        with patch.object(m, '_append_csv_row_durable', autospec=True) as mock_append:
            def fake_append(path, row):
                captured['pnl'] = row.get('PnL', 0)
                captured['action'] = row.get('Action', '')
            mock_append.side_effect = fake_append

            m.exit_paper_position('PAPER_EXIT', price=150.0, note='test exit')

        # Long entry at 100, exit at 150 → gross = (150-100)*99*1 = 4950
        # Minus fees: broker 20*2=40, exchange 5*2=10, tax ~(100+150)*99*0.001=24.75
        # Net ≈ 4950 - 40 - 10 - 24.75 ≈ 4875
        assert captured.get('action') == 'PAPER_EXIT'
        assert captured.get('pnl', 0) != 0, "PnL should be non-zero"
        # With point_value=99, PnL should be much larger than if 50 were used
        # (50 would give (150-100)*50 = 2500 gross → ~2425 net)
        assert captured['pnl'] > 3000, (
            f"PnL={captured['pnl']} should be >3000 with point_value=99, "
            f"indicating config was used instead of hardcoded 50"
        )


class TestHardcodedFiftyNotPresent:
    """Verify no new hardcoded * 50 multipliers were introduced."""

    SOURCE_FILE = 'strategies/options/live_options_squeeze_monitor.py'

    def test_no_hardcoded_txo_multiplier_comment(self):
        """
        Ensure 'TXO multiplier' comment (from the old hardcoded 50) is gone.
        """
        with open(self.SOURCE_FILE) as f:
            content = f.read()
        assert 'TXO multiplier' not in content, (
            "Old hardcoded comment 'TXO multiplier' should be removed"
        )

    @pytest.mark.parametrize('bad_pattern', [
        # These patterns should NOT appear — they're hardcoded multipliers
        ('* 50', 'Hardcoded * 50 multiplier'),
        ('* 50 ', 'Hardcoded * 50 with trailing space'),
    ])
    def test_no_hardcoded_multiplier_in_pnl_code(self, bad_pattern):
        """
        Verify that specific hardcoded multipliers are not present.
        Only check within PnL/margin code regions (not in comments or docstrings).
        """
        pattern, desc = bad_pattern
        with open(self.SOURCE_FILE) as f:
            lines = f.readlines()

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Skip comments and docstrings
            if stripped.startswith('#') or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            if pattern in stripped:
                # Only flag if it's in an executable context (not a string literal or default)
                # Check for common acceptable uses: default params, string formats
                if 'point_value' in stripped or 'point_value' in stripped:
                    continue
                if 'pricing_cfg' in stripped:
                    continue
                pytest.fail(
                    f"Hardcoded pattern '{pattern}' found at line {i}: {stripped}"
                )
