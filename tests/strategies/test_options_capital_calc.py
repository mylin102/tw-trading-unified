"""
Unit tests for capital calculation utilities.

Tests _calc_required_margin and _calc_available_capital in isolation,
then verifies _paper_margin_check delegates to them (behavior preserved).
"""
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def monitor(tmp_path):
    """Create a minimal monitor with controlled config and a real temp ledger file."""
    with patch('strategies.options.live_options_squeeze_monitor.ShioajiOptionsSmartMonitor.load_config') as mock_load:
        mock_config = {
            'active_mode': 'V2',
            'modes': {'V2': {'tp1_pct': 0.3}},
            'exit_optimization': {'eod_panic_time': '13:30', 'eod_passive_window_mins': 20},
            'strategy': {'entry_score': 15, 'cooldown_bars': 3},
            'execution': {'broker_fee_per_side': 20, 'exchange_fee_per_side': 5},
            'pricing': {
                'point_value': 99,
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
        # Use real temp file for ledger so pd.read_csv can actually read it
        m.ledger_path = tmp_path / "options_trade_ledger_test.csv"
        m._record_paper_order = MagicMock()
        return m


# ── _calc_required_margin ──

class TestCalcRequiredMargin:

    def test_normal_entry(self, monitor):
        """entry_price=800, lots=1, point_value=99 → 800*99*1 = 79200"""
        monitor.pricing_cfg = {'point_value': 99}
        assert monitor._calc_required_margin(800, lots=1) == 79200

    def test_multiple_lots(self, monitor):
        """entry_price=800, lots=3, point_value=99 → 800*99*3 = 237600"""
        monitor.pricing_cfg = {'point_value': 99}
        assert monitor._calc_required_margin(800, lots=3) == 237600

    def test_zero_price_fallback(self, monitor):
        """entry_price=0 → fallback 10000 * lots"""
        monitor.pricing_cfg = {'point_value': 99}
        assert monitor._calc_required_margin(0, lots=1) == 10000
        assert monitor._calc_required_margin(0, lots=3) == 30000

    def test_config_point_value_used(self, monitor):
        """With point_value=50 (standard), entry_price=1450, lots=1 → 72500"""
        monitor.pricing_cfg = {'point_value': 50}
        assert monitor._calc_required_margin(1450, lots=1) == 72500

    def test_default_point_value(self, monitor):
        """Empty pricing_cfg → fallback to 50"""
        monitor.pricing_cfg = {}
        assert monitor._calc_required_margin(800, lots=1) == 40000  # 800 * 50 * 1


# ── _calc_available_capital ──

class TestCalcAvailableCapital:

    def test_no_ledger_default(self, monitor):
        """ledger_path doesn't exist → available = 100000 * 0.8 = 80000"""
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        # Temp file doesn't exist yet
        assert not monitor.ledger_path.exists()
        assert monitor._calc_available_capital() == 80000

    def _write_ledger(self, monitor, csv_content):
        """Helper: write CSV content to the real ledger temp file."""
        monitor.ledger_path.write_text(csv_content)

    def test_with_realized_pnl(self, monitor):
        """Ledger has PnL=+5000 → available = (100000+5000)*0.8 = 84000"""
        self._write_ledger(monitor, "PnL\n1000\n2000\n2000\n")
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        assert monitor._calc_available_capital() == 84000

    def test_with_negative_pnl(self, monitor):
        """Ledger has PnL=-30000 → available = (100000-30000)*0.8 = 56000"""
        self._write_ledger(monitor, "PnL\n-10000\n-20000\n")
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        assert monitor._calc_available_capital() == 56000

    def test_ledger_read_error_safe(self, monitor):
        """Ledger read raises exception → fallback to initial capital * 0.8"""
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        monitor.ledger_path.write_text("NOT VALID CSV!!!\ncorrupt,data\n")
        assert monitor._calc_available_capital() == 80000


# ── _paper_margin_check delegates correctly (integration) ──

class TestPaperMarginCheckDelegates:

    def _write_ledger(self, monitor, csv_content):
        monitor.ledger_path.write_text(csv_content)

    def test_allow_when_sufficient_capital(self, monitor):
        """available=80000, required=79200 (800*99) → allow"""
        monitor.pricing_cfg = {'point_value': 99}
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        monitor.base_lots = 1
        # No ledger file → available = 80000
        assert monitor._paper_margin_check(800, lots=1) is True

    def test_block_when_insufficient_capital(self, monitor):
        """available=80000, required=81081 (819*99) → block"""
        monitor.pricing_cfg = {'point_value': 99}
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        monitor.base_lots = 1
        assert monitor._paper_margin_check(819, lots=1) is False

    def test_allow_with_positive_pnl(self, monitor):
        """available=84000 (100K+5K realized), required=80000 → allow"""
        self._write_ledger(monitor, "PnL\n5000\n")
        monitor.pricing_cfg = {'point_value': 50}
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        monitor.base_lots = 1
        assert monitor._paper_margin_check(1600, lots=1) is True

    def test_block_with_negative_pnl_high_entry(self, monitor):
        """available=56000 (100K-30K), required=1125*50=56250 > 56000 → block"""
        self._write_ledger(monitor, "PnL\n-10000\n-20000\n")
        monitor.pricing_cfg = {'point_value': 50}
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        monitor.base_lots = 1
        assert monitor._paper_margin_check(1125, lots=1) is False

    # ── Cumulative position tests ──
    # 2026-05-25 Hermes Agent: verify existing position capital consumption

    def test_block_when_already_has_position(self, monitor):
        """已持倉 1 張 (entry_price=1450)，再加 1 張 (price=1450)
        existing=1450*50*1=72500, new=1450*50*1=72500, total=145000 > 80000 → block
        """
        monitor.pricing_cfg = {'point_value': 50}
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        monitor.base_lots = 1
        monitor.position = 1
        monitor.entry_price = 1450
        assert monitor._paper_margin_check(1450, lots=1) is False

    def test_allow_first_entry_reasonable(self, monitor):
        """空倉，第一張 entry_price=1450 → required=1450*50=72500 < 80000 → allow"""
        monitor.pricing_cfg = {'point_value': 50}
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        monitor.base_lots = 1
        monitor.position = 0
        monitor.entry_price = 0
        assert monitor._paper_margin_check(1450, lots=1) is True

    def test_block_second_addon_exceeds_capital(self, monitor):
        """已持倉 1 張 (entry_price=1400)，再加 1 張 (price=1400) → total=140K > 80K → block"""
        monitor.pricing_cfg = {'point_value': 50}
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        monitor.base_lots = 1
        monitor.position = 1
        monitor.entry_price = 1400
        assert monitor._paper_margin_check(1400, lots=1) is False

    def test_allow_small_addon_within_capital(self, monitor):
        """已持倉 1 張 (entry_price=500)，再加 1 張 (price=500) → total=50K < 80K → allow"""
        monitor.pricing_cfg = {'point_value': 50}
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        monitor.base_lots = 1
        monitor.position = 1
        monitor.entry_price = 500
        assert monitor._paper_margin_check(500, lots=1) is True


# ── Recovery capital check tests ──
# 2026-05-25 Hermes Agent: verify recovery force-closes positions
# that exceed available capital

class TestRecoveryCapitalCheck:

    def _write_ledger(self, monitor, csv_content):
        monitor.ledger_path.write_text(csv_content)

    def test_recovery_allows_when_sufficient_capital(self, monkeypatch, monitor):
        """Ledger has position (1x C @ 500) and sufficient capital → position restored"""
        self._write_ledger(monitor,
            "trade_id,Timestamp,Mode,Action,Side,Price,Quantity,PnL,Balance,Note\n"
            "t1,2026-05-25 09:00:00,V2,PAPER_ENTRY,C,500.0,1,0,0,score=50.0\n"
        )
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        monitor.pricing_cfg = {'point_value': 50}

        monkeypatch.setattr(monitor, 'order_mgr', None)
        monkeypatch.setattr(monitor, 'active_contracts', {})
        monkeypatch.setattr(monitor, '_theta_gang', None)

        monitor._recover_position_from_api()

        assert monitor.position == 1
        assert monitor.active_side == 'C'
        assert monitor.entry_price == 500.0

    def test_recovery_force_closes_when_insufficient_capital(self, monkeypatch, monitor):
        """Ledger has position (1x C @ 2000) requiring 100K but only 80K available → force close"""
        self._write_ledger(monitor,
            "trade_id,Timestamp,Mode,Action,Side,Price,Quantity,PnL,Balance,Note\n"
            "t1,2026-05-25 09:00:00,V2,PAPER_ENTRY,C,2000.0,1,0,0,score=50.0\n"
        )
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        monitor.pricing_cfg = {'point_value': 50}

        monkeypatch.setattr(monitor, 'order_mgr', None)
        monkeypatch.setattr(monitor, 'active_contracts', {})
        monkeypatch.setattr(monitor, '_theta_gang', None)

        # 2000 * 50 * 1 = 100000 > 80000 → should force close
        monitor._recover_position_from_api()

        assert monitor.position == 0, f"expected 0, got {monitor.position}"
        assert monitor.active_side is None

        # Verify ledger has the force close record
        import csv
        with open(monitor.ledger_path) as f:
            rows = list(csv.DictReader(f))
        actions = [r['Action'] for r in rows]
        assert 'RECOVERY_FORCE_CLOSE_EXIT' in actions, \
            f"expected RECOVERY_FORCE_CLOSE_EXIT in ledger, got {actions}"

    def test_recovery_force_close_when_negative_pnl(self, monkeypatch, monitor):
        """Ledger has realized loss -30K, position (1x C @ 1450) requires 72.5K but only 56K available → force close"""
        self._write_ledger(monitor,
            "trade_id,Timestamp,Mode,Action,Side,Price,Quantity,PnL,Balance,Note\n"
            "t1,2026-05-25 09:00:00,V2,PAPER_ENTRY,C,1450.0,1,0,0,score=50.0\n"
            "t2,2026-05-25 10:00:00,V2,PAPER_EXIT,C,1000.0,1,-22500,-22500,stop_loss\n"
            "t3,2026-05-25 11:00:00,V2,PAPER_ENTRY,C,1450.0,1,0,-22500,score=50.0\n"
        )
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        monitor.pricing_cfg = {'point_value': 50}

        monkeypatch.setattr(monitor, 'order_mgr', None)
        monkeypatch.setattr(monitor, 'active_contracts', {})
        monkeypatch.setattr(monitor, '_theta_gang', None)

        # available = (100000 - 22500) * 0.8 = 62000
        # required = 1450 * 50 * 1 = 72500
        # 62000 < 72500 → force close
        monitor._recover_position_from_api()

        assert monitor.position == 0, f"expected 0, got {monitor.position}"

    def test_recovery_allows_with_small_loss(self, monkeypatch, monitor):
        """Ledger has realized loss -10K, position (1x C @ 1000) requires 50K, available = (100K-10K)*0.8 = 72K → allow"""
        self._write_ledger(monitor,
            "trade_id,Timestamp,Mode,Action,Side,Price,Quantity,PnL,Balance,Note\n"
            "t1,2026-05-25 09:00:00,V2,PAPER_ENTRY,C,1000.0,1,0,0,score=50.0\n"
            "t2,2026-05-25 10:00:00,V2,PAPER_EXIT,C,800.0,1,-11000,-11000,stop_loss\n"
            "t3,2026-05-25 11:00:00,V2,PAPER_ENTRY,C,1000.0,1,0,-11000,score=50.0\n"
        )
        monitor.full_cfg = {'risk_mgmt': {'initial_capital': 100000}}
        monitor.pricing_cfg = {'point_value': 50}

        monkeypatch.setattr(monitor, 'order_mgr', None)
        monkeypatch.setattr(monitor, 'active_contracts', {})
        monkeypatch.setattr(monitor, '_theta_gang', None)

        monitor._recover_position_from_api()

        assert monitor.position == 1, f"expected 1, got {monitor.position}"
        assert monitor.active_side == 'C'
        assert monitor.entry_price == 1000.0
