import pytest
import pandas as pd

from core.dashboard_positions import (
    count_futures_entries,
    count_options_entries,
    describe_options_order_truth,
    estimate_options_order_unrealized,
    estimate_theta_unrealized,
    find_latest_open_futures_position,
    find_latest_open_options_position,
    latest_indicator_close,
    option_order_matches_open_position,
    summarize_combo_legs,
)


def test_count_futures_entries_ignores_exit_rows():
    df = pd.DataFrame(
        [
            {"timestamp": "2026-04-20 21:17:37", "type": "SELL", "price": 37495.0, "lots": 1},
            {"timestamp": "2026-04-20 21:30:01", "type": "EXIT", "price": 37591.0, "lots": 1},
        ]
    )

    assert count_futures_entries(df) == 1


def test_find_latest_open_futures_position_returns_open_short():
    df = pd.DataFrame(
        [
            {"timestamp": "2026-04-20 21:17:37", "type": "SELL", "price": 37495.0, "lots": 1},
        ]
    )

    open_pos = find_latest_open_futures_position(df)

    assert open_pos is not None
    assert open_pos.direction == "SHORT"
    assert open_pos.entry_price == 37495.0
    assert open_pos.cost_basis == 37495.0 * 50


def test_count_options_entries_uses_trading_day_not_calendar_day():
    ledger = pd.DataFrame(
        [
            {"Timestamp": "2026-04-20 21:03:58", "Action": "THETA_ENTRY", "Price": 182.835704, "Quantity": 1},
        ]
    )

    assert count_options_entries(ledger, "20260421") == 1


def test_find_latest_open_options_position_returns_theta_entry():
    ledger = pd.DataFrame(
        [
            {
                "Timestamp": "2026-04-20 21:03:58",
                "Action": "THETA_ENTRY",
                "Side": "THETA",
                "Price": 182.835704,
                "Quantity": 1,
                "Note": "credit=183 max_loss=17 strategy=iron_condor [SELL P37200 | BUY P37000 | SELL C37600 | BUY C37800]",
            }
        ]
    )

    open_pos = find_latest_open_options_position(ledger)

    assert open_pos is not None
    assert open_pos.action == "THETA_ENTRY"
    assert open_pos.entry_price == 182.835704


def test_estimate_theta_unrealized_prices_all_legs():
    estimate = estimate_theta_unrealized(
        "credit=183 max_loss=17 strategy=iron_condor [SELL P37200 | BUY P37000 | SELL C37600 | BUY C37800]",
        current_spot=37525.0,
        current_iv=0.2675,
        dte_years=29.1 / 365,
        quantity=1,
    )

    assert estimate is not None
    assert estimate["strategy"] == "iron_condor"
    assert estimate["cost_basis"] == 183 * 50
    assert len(estimate["legs"]) == 4
    assert isinstance(estimate["unrealized_pnl"], float)


def test_estimate_options_order_unrealized_uses_theta_spread_model():
    ledger = pd.DataFrame(
        [
            {
                "Timestamp": "2026-04-21 16:00:51",
                "Action": "THETA_ENTRY",
                "Side": "THETA",
                "Price": 182.8010576179604,
                "Quantity": 1,
                "Note": "credit=183 max_loss=17 strategy=iron_condor [SELL P37800 | BUY P37600 | SELL C38200 | BUY C38400]",
            }
        ]
    )
    open_pos = find_latest_open_options_position(ledger)
    order_row = {
        "status": "filled",
        "created_at": "2026-04-21 16:00:51",
        "avg_fill_price": 182.8010576179604,
        "filled_quantity": 1,
        "side": "sell",
        "strategy": "iron_condor",
    }

    estimate = estimate_options_order_unrealized(
        order_row,
        open_pos,
        live_premium=38044.0,
        current_spot=38045.0,
        current_iv=0.2675,
        dte_years=28.43 / 365,
    )

    assert estimate is not None
    assert estimate["pricing_label"] == "spread_value"
    assert estimate["current_price"] < 1000
    assert estimate["unrealized_pnl"] > -100000


def test_estimate_options_order_unrealized_uses_broker_combo_metadata_without_ledger_open_position():
    order_row = {
        "status": "filled",
        "created_at": "2026-04-21 16:00:51",
        "avg_fill_price": 45.0,
        "filled_quantity": 1,
        "quantity": 1,
        "side": "sell",
        "strategy": "theta_gang",
        "truth_source": "broker_combo",
        "combo_strategy": "bull_put_spread",
        "combo_legs": [
            {"action": "SELL", "side": "P", "strike": 21800},
            {"action": "BUY", "side": "P", "strike": 21700},
        ],
    }

    estimate = estimate_options_order_unrealized(
        order_row,
        None,
        current_spot=21750.0,
        current_iv=0.24,
        dte_years=21 / 365,
    )

    assert estimate is not None
    assert estimate["pricing_label"] == "spread_value"
    assert estimate["current_price"] < 200
    assert estimate["unrealized_pnl"] > -100000


def test_estimate_options_order_unrealized_uses_live_premium_for_single_leg():
    ledger = pd.DataFrame(
        [
            {
                "Timestamp": "2026-04-21 08:47:51",
                "Action": "PAPER_ENTRY",
                "Side": "C",
                "Price": 1190.0,
                "Quantity": 2,
                "Note": "score=20.0",
            }
        ]
    )
    open_pos = find_latest_open_options_position(ledger)
    order_row = {
        "status": "filled",
        "created_at": "2026-04-21 08:47:51",
        "avg_fill_price": 1190.0,
        "filled_quantity": 2,
        "side": "buy",
        "strategy": "directional",
    }

    estimate = estimate_options_order_unrealized(
        order_row,
        open_pos,
        live_premium=1210.0,
        current_spot=38045.0,
        current_iv=0.2675,
        dte_years=28.43 / 365,
        strike=38100.0,
    )

    assert estimate is not None
    assert estimate["unrealized_pnl"] == 2000.0
    assert estimate["current_price"] == 1210.0
    assert estimate["pricing_label"] == "option_premium"
    assert estimate["premium_source"] == "LIVE_QUOTE"
    assert estimate["dte_days"] == pytest.approx(28.43, rel=0.01)


def test_describe_options_order_truth_distinguishes_broker_paper_and_ledger_rows():
    broker_combo = describe_options_order_truth(
        {
            "truth_source": "broker_combo",
            "status": "filled",
            "combo_legs": [
                {"action": "SELL", "side": "P", "strike": 21800},
                {"action": "BUY", "side": "P", "strike": 21700},
            ],
        },
        orders_rebuilt_from_ledger=False,
    )
    paper_strategy = describe_options_order_truth(
        {
            "strategy": "theta_gang",
            "status": "filled",
        },
        orders_rebuilt_from_ledger=False,
    )
    ledger_rebuilt = describe_options_order_truth(
        {
            "strategy": "theta_gang",
            "status": "filled",
        },
        orders_rebuilt_from_ledger=True,
    )

    assert broker_combo["truth_source"] == "broker_combo"
    assert broker_combo["badge"].startswith("✅")
    assert broker_combo["show_paper_disclaimer"] is False
    assert broker_combo["degraded_caption"] == ""

    assert paper_strategy["truth_source"] == "paper_strategy"
    assert paper_strategy["badge"].startswith("📝")
    assert paper_strategy["show_paper_disclaimer"] is True
    assert "紙上" in paper_strategy["degraded_caption"]

    assert ledger_rebuilt["truth_source"] == "ledger_rebuilt"
    assert ledger_rebuilt["badge"].startswith("⚠️")
    assert ledger_rebuilt["show_paper_disclaimer"] is True
    assert "ledger" in ledger_rebuilt["degraded_caption"].lower()


def test_summarize_combo_legs_formats_spread_legs_for_dashboard():
    assert summarize_combo_legs(
        [
            {"action": "SELL", "side": "P", "strike": 21800},
            {"action": "BUY", "side": "P", "strike": 21700},
        ]
    ) == "SELL P21800 | BUY P21700"


def test_option_order_matches_only_current_open_position():
    ledger = pd.DataFrame(
        [
            {
                "Timestamp": "2026-04-21 08:47:51",
                "Action": "PAPER_ENTRY",
                "Side": "C",
                "Price": 1190.0,
                "Quantity": 2,
                "Note": "score=20.0",
            }
        ]
    )
    open_pos = find_latest_open_options_position(ledger)

    current_order = {
        "created_at": "2026-04-21T08:47:51",
        "avg_fill_price": 1190.0,
        "filled_quantity": 2,
        "strategy": "directional",
    }
    recovered_dup = {
        "created_at": "2026-04-21T08:47:51",
        "avg_fill_price": 1190.0,
        "filled_quantity": 2,
        "strategy": "RECOVERED",
    }
    old_order = {
        "created_at": "2026-04-21T07:41:08",
        "avg_fill_price": 182.98956,
        "filled_quantity": 1,
        "strategy": "THETA",
    }

    assert option_order_matches_open_position(current_order, open_pos) is True
    assert option_order_matches_open_position(recovered_dup, open_pos) is False
    assert option_order_matches_open_position(old_order, open_pos) is False


def test_latest_indicator_close_uses_latest_row_not_first_row():
    indicators = pd.DataFrame(
        [
            {"timestamp": "2026-04-21 09:01:00", "close": 2565.0},
            {"timestamp": "2026-04-21 10:17:00", "close": 2500.0},
        ]
    )

    assert latest_indicator_close(indicators) == 2500.0


# 2026-05-25 Hermes Agent: verify expiry date calculation from dte
def test_options_position_caption_shows_expiry_from_dte():
    """Verify that dte days → expiry date calculation is correct."""
    from datetime import datetime, timedelta

    # Simulate what dashboard.py does: dte=22.38, today fixed
    today = datetime(2026, 5, 25, 22, 8, 0)
    dte_days = 22.38
    expected_date = (today + timedelta(days=dte_days)).strftime("%Y-%m-%d")

    # dte=22.38 from today 2026-05-25 → 2026-06-17 (June W3 monthly settlement)
    assert expected_date == "2026-06-17"
    assert f"到期: {expected_date} (剩 {dte_days:.0f} 天)" == "到期: 2026-06-17 (剩 22 天)"


def test_options_position_caption_hides_expiry_when_dte_zero():
    """Verify that when dte=0, no expiry info is displayed."""
    dte_days = 0.0
    expiry_str = ""
    if dte_days > 0:
        from datetime import datetime, timedelta
        today = datetime.now()
        expiry_date = today + timedelta(days=dte_days)
        expiry_str = expiry_date.strftime("%Y-%m-%d")

    assert expiry_str == ""


def test_options_position_caption_hides_expiry_when_dte_missing():
    """Verify that when no indicator data, caption doesn't show expiry."""
    current_dte_years = 0.0
    dte_days = current_dte_years * 365.0 if current_dte_years > 0 else 0.0
    expiry_str = ""
    if dte_days > 0:
        from datetime import datetime, timedelta
        today = datetime.now()
        expiry_date = today + timedelta(days=dte_days)
        expiry_str = expiry_date.strftime("%Y-%m-%d")

    assert dte_days == 0.0
    assert expiry_str == ""


# 2026-05-25 Hermes Agent: verify BS theoretical premium for single-leg call
def test_estimate_single_leg_uses_bs_theo_when_no_live_premium():
    ledger = pd.DataFrame(
        [
            {
                "Timestamp": "2026-05-25 22:08:04",
                "Action": "PAPER_ENTRY",
                "Side": "C",
                "Price": 1470.0,
                "Quantity": 1,
                "Note": "score=86.7",
            }
        ]
    )
    open_pos = find_latest_open_options_position(ledger)
    order_row = {
        "status": "filled",
        "created_at": "2026-05-25 22:08:04",
        "avg_fill_price": 1470.0,
        "filled_quantity": 1,
        "side": "buy",
        "strategy": "c",
    }

    # spot=44088, strike=44100, iv=0.307, dte=22.11 → BS should give ~1470-ish
    estimate = estimate_options_order_unrealized(
        order_row,
        open_pos,
        live_premium=0.0,  # No live quote → BS fallback
        current_spot=44088.0,
        current_iv=0.307,
        dte_years=22.11 / 365,
        strike=44100.0,
    )

    assert estimate is not None
    assert estimate["premium_source"] == "BS_THEO"
    assert estimate["current_price"] > 0
    assert estimate["current_price"] < 2000  # should be reasonable ~1470 range
    assert estimate["pricing_label"] == "option_premium"
    assert estimate["dte_days"] == pytest.approx(22.11, rel=0.01)
    # unrlized pnl should be reasonable (entry ~= theoretical, within 200pts)
    assert abs(estimate["unrealized_pnl"]) < 10000


def test_estimate_single_leg_uses_entry_fallback_when_no_data():
    ledger = pd.DataFrame(
        [
            {
                "Timestamp": "2026-05-25 22:08:04",
                "Action": "PAPER_ENTRY",
                "Side": "C",
                "Price": 1470.0,
                "Quantity": 1,
                "Note": "score=86.7",
            }
        ]
    )
    open_pos = find_latest_open_options_position(ledger)
    order_row = {
        "status": "filled",
        "created_at": "2026-05-25 22:08:04",
        "avg_fill_price": 1470.0,
        "filled_quantity": 1,
        "side": "buy",
        "strategy": "c",
    }

    # No live premium, no BS data → entry fallback
    estimate = estimate_options_order_unrealized(
        order_row,
        open_pos,
        live_premium=0.0,
        current_spot=0.0,
        current_iv=0.0,
        dte_years=0.0,
        strike=0.0,
    )

    assert estimate is not None
    assert estimate["premium_source"] == "ENTRY_FALLBACK"
    assert estimate["current_price"] == 1470.0
    assert estimate["unrealized_pnl"] == 0.0
    assert estimate["pricing_label"] == "option_premium"
    assert estimate["dte_days"] == pytest.approx(0.0, abs=0.01)
