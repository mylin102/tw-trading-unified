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
    )

    assert estimate == {
        "unrealized_pnl": 2000.0,
        "current_price": 1210.0,
        "pricing_label": "option_premium",
    }


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
