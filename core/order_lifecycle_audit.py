from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


OPTION_EXIT_KEYWORDS = ("EXIT", "THETA_EXIT", "TP1", "TRAIL", "TIME", "REVERSAL", "TRAP", "EOD", "FILL")


def count_option_ledger_order_events(ledger_df: pd.DataFrame | None) -> int:
    if ledger_df is None or ledger_df.empty or "Action" not in ledger_df.columns:
        return 0
    actions = ledger_df["Action"].fillna("").astype(str)
    return int(actions.apply(_is_option_order_event).sum())


def read_orders_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    return data if isinstance(data, list) else []


def write_orders_file(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2))


def rebuild_options_orders_from_ledger(ledger_df: pd.DataFrame | None) -> list[dict]:
    if ledger_df is None or ledger_df.empty:
        return []

    rows: list[dict] = []
    for idx, (_, row) in enumerate(ledger_df.iterrows(), start=1):
        action = str(row.get("Action", ""))
        if not _is_option_order_event(action):
            continue

        side_label = str(row.get("Side", "")).upper()
        is_short_entry = "THETA" in action.upper() or side_label in {"THETA", "SHORT"}
        is_entry = "ENTRY" in action.upper() and "EXIT" not in action.upper()
        side = "sell" if (is_short_entry and is_entry) else "buy" if is_short_entry else "buy" if is_entry else "sell"
        quantity = int(row.get("Quantity", 1) or 1)
        price = float(row.get("Price", 0) or 0)
        note = str(row.get("Note", ""))
        strategy = _extract_strategy(note) or side_label.lower() or "options"
        timestamp = str(row.get("Timestamp", ""))

        rows.append(
            {
                "order_id": f"LEDGER-{idx:06d}",
                "symbol": "TXO",
                "side": side,
                "order_type": "market",
                "quantity": quantity,
                "filled_quantity": quantity,
                "remaining_quantity": 0,
                "price": price,
                "stop_price": None,
                "avg_fill_price": price,
                "status": "filled",
                "strategy": strategy,
                "account": "",
                "comment": note,
                "commission": 0.0,
                "tax": 0.0,
                "total_fee": 0.0,
                "slippage": 0.0,
                "fill_time_ms": 0,
                "exchange_order_id": f"LEDGER-{idx:06d}",
                "reject_reason": None,
                "cancel_reason": None,
                "parent_order_id": None,
                "created_at": timestamp,
                "submitted_at": timestamp,
                "filled_at": timestamp,
                "cancelled_at": None,
                "rejected_at": None,
                "expired_at": None,
                "updated_at": timestamp,
                "unrealized_pnl": None,
                "unrealized_pnl_pts": None,
                "current_price": None,
            }
        )
    return rows


def _is_option_order_event(action: str) -> bool:
    upper = str(action).upper()
    if "RETRY" in upper or "SUBMITTED" in upper or "CLEARED" in upper:
        return False
    return ("ENTRY" in upper and "EXIT" not in upper) or any(keyword in upper for keyword in OPTION_EXIT_KEYWORDS)


def _extract_strategy(note: str) -> str:
    if "strategy=" not in note:
        return ""
    tail = note.split("strategy=", 1)[1]
    return tail.split()[0].strip("[]")
