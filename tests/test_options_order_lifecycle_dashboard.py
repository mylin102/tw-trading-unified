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


def test_dashboard_renders_truth_source_labels_and_combo_valuation_columns():
    src = Path("ui/dashboard.py").read_text()

    assert "broker_combo" in src
    assert "paper_strategy" in src
    assert "ledger_rebuilt" in src
    assert "真實來源" in src
    assert "組合腿摘要" in src
    assert "目前組合價值" in src


def test_dashboard_only_shows_paper_theta_disclaimer_for_non_broker_truth():
    src = Path("ui/dashboard.py").read_text()

    assert "show_paper_disclaimer" in src
    assert "broker_combo" in src
    assert "紙上生命週期紀錄" in src


def test_dashboard_surfaces_ledger_rebuild_degraded_caption():
    src = Path("ui/dashboard.py").read_text()

    assert "委託單檔案為空" in src
    assert "degraded_caption" in src
