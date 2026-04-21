from pathlib import Path

from core.dashboard_positions import describe_options_order_truth


def test_dashboard_labels_combo_truth_sources():
    broker_combo = describe_options_order_truth(
        {"truth_source": "broker_combo", "combo_legs": [{"action": "SELL", "side": "P", "strike": 21800}]},
        orders_rebuilt_from_ledger=False,
    )
    paper_strategy = describe_options_order_truth(
        {"strategy": "theta_gang"},
        orders_rebuilt_from_ledger=False,
    )
    ledger_rebuilt = describe_options_order_truth(
        {"strategy": "theta_gang"},
        orders_rebuilt_from_ledger=True,
    )

    assert broker_combo["badge"] == "✅ broker_combo"
    assert paper_strategy["badge"] == "📝 paper_strategy"
    assert ledger_rebuilt["badge"] == "⚠️ ledger_rebuilt"


def test_dashboard_source_shows_truth_column_combo_summary_and_degraded_caption():
    src = Path("ui/dashboard.py").read_text()

    assert "真實來源" in src
    assert "combo_legs" in src
    assert "組合腿摘要" in src
    assert "degraded_caption" in src
    assert "ledger_rebuilt" in src
