import pandas as pd

from core.order_lifecycle_audit import (
    count_option_ledger_order_events,
    rebuild_options_orders_from_ledger,
)


def test_count_option_ledger_order_events_counts_theta_entry_and_exit():
    ledger = pd.DataFrame(
        [
            {"Action": "THETA_ENTRY", "Price": 183, "Quantity": 1},
            {"Action": "THETA_EXIT", "Price": 95, "Quantity": 1},
            {"Action": "SUBMITTED", "Price": 0, "Quantity": 1},
        ]
    )

    assert count_option_ledger_order_events(ledger) == 2


def test_rebuild_options_orders_from_ledger_builds_filled_lifecycle_rows():
    ledger = pd.DataFrame(
        [
            {
                "Timestamp": "2026-04-20 21:03:58",
                "Action": "THETA_ENTRY",
                "Side": "THETA",
                "Price": 182.835704,
                "Quantity": 1,
                "Note": "credit=183 max_loss=17 strategy=iron_condor [SELL P37200 | BUY P37000 | SELL C37600 | BUY C37800]",
            },
            {
                "Timestamp": "2026-04-20 22:15:00",
                "Action": "THETA_EXIT",
                "Side": "iron_condor",
                "Price": 95.0,
                "Quantity": 1,
                "Note": "TP credit=183 pnl=80",
            },
        ]
    )

    rebuilt = rebuild_options_orders_from_ledger(ledger)

    assert len(rebuilt) == 2
    assert rebuilt[0]["side"] == "sell"
    assert rebuilt[0]["strategy"] == "iron_condor"
    assert rebuilt[1]["side"] == "buy"
    assert rebuilt[1]["status"] == "filled"
