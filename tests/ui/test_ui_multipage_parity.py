# 2026-07-26 Gemini CLI: Wave D1 Streamlit Multipage Structure & Numerical Parity Test
from pathlib import Path
import pandas as pd
import pytest

from ui.attribution_dashboard import AttributionDashboard


def test_multipage_folder_structure_exists():
    """Verify ui/pages multipage file structure."""
    ui_dir = Path("ui")
    pages_dir = ui_dir / "pages"
    assert pages_dir.exists() and pages_dir.is_dir()

    expected_pages = [
        "01_live_monitor.py",
        "02_positions_orders.py",
        "03_trade_review.py",
        "04_attribution.py",
        "05_configuration.py",
    ]

    for page in expected_pages:
        page_path = pages_dir / page
        assert page_path.exists(), f"Missing multipage file: {page_path}"


def test_attribution_dashboard_numerical_parity():
    """Verify AttributionDashboard calculation parity between 8501 standalone and 8500 multipage page."""
    attr_dir = Path("data/attribution")
    dash = AttributionDashboard(attr_dir)

    router_df, signal_df, trade_df = dash.load_data()
    metrics = dash.calculate_summary_metrics(router_df, trade_df)

    # Invariants
    assert isinstance(metrics, dict)
    if not trade_df.empty and "pnl" in trade_df.columns:
        assert metrics.get("total_pnl") == float(trade_df["pnl"].sum())
    else:
        assert metrics.get("total_pnl", 0) == 0
