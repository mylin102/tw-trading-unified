from pathlib import Path


def test_theta_entry_uses_pending_broker_combo_truth_for_dashboard():
    src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()

    assert 'if self.live_trading:' in src
    assert 'self._submit_live_theta_combo_entry(entry_info)' in src
    assert 'self.pending_theta_combo = {' in src
    assert 'truth_source="broker_combo"' in src
    assert 'symbol="TXO-COMBO"' in src


def test_theta_exit_uses_pending_broker_combo_truth_for_dashboard():
    src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()

    assert 'self._submit_live_theta_combo_exit(exit_info)' in src
    assert '"phase": "exit"' in src
    assert 'THETA_LIVE_EXIT_SUBMITTED' in src
