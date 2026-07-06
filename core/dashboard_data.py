from __future__ import annotations

import pandas as pd
from pathlib import Path


def _stable_string_identifier(value) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def merge_indicator_frames(frames: list[pd.DataFrame], timestamp_col: str = "timestamp") -> pd.DataFrame:
    """Merge overlapping indicator frames and prefer the newest, most complete row per timestamp."""
    prepared: list[pd.DataFrame] = []
    for priority, frame in enumerate(frames):
        if frame is None or frame.empty or timestamp_col not in frame.columns:
            continue
        current = frame.copy()
        current["__source_priority"] = current.get("__source_priority", priority)
        current["__row_completeness"] = current.drop(
            columns=[timestamp_col, "__source_priority"],
            errors="ignore",
        ).notna().sum(axis=1)
        prepared.append(current)

    if not prepared:
        return pd.DataFrame()

    merged = pd.concat(prepared, ignore_index=True, sort=False)
    merged = merged.sort_values(
        [timestamp_col, "__source_priority", "__row_completeness"],
        kind="stable",
    )
    merged = merged.drop_duplicates(subset=[timestamp_col], keep="last")
    merged = merged.sort_values(timestamp_col, kind="stable").reset_index(drop=True)
    return merged.drop(columns=["__source_priority", "__row_completeness"], errors="ignore")


def extend_taifex_recess_continuity(
    frame: pd.DataFrame,
    *,
    now: pd.Timestamp | None = None,
    timestamp_col: str = "timestamp",
    max_gap_hours: float = 6.0,
) -> pd.DataFrame:
    """Extend the last valid futures row through scheduled recess windows for dashboard continuity.

    This is display-only continuity. It must not hide real in-session feed stalls, so it only
    applies during the normal TAIFEX recess windows (05:00-08:45, 13:45-15:00) and only when
    the last real row is recent enough to plausibly belong to the immediately preceding session.
    """
    if frame is None or frame.empty or timestamp_col not in frame.columns:
        return frame

    current = frame.copy()
    ts = pd.to_datetime(current[timestamp_col], errors="coerce")
    valid_mask = ts.notna()
    if not valid_mask.any():
        return current

    current = current.loc[valid_mask].copy()
    current[timestamp_col] = ts.loc[valid_mask]
    current = current.sort_values(timestamp_col, kind="stable").reset_index(drop=True)

    now_ts = pd.Timestamp(now or pd.Timestamp.now()).tz_localize(None)
    hhmm = int(now_ts.strftime("%H%M"))
    weekday = now_ts.weekday()
    in_morning_recess = weekday < 5 and 500 <= hhmm < 845
    in_lunch_recess = weekday < 5 and 1345 <= hhmm < 1500
    if not (in_morning_recess or in_lunch_recess):
        return current

    last_row = current.iloc[-1].copy()
    last_ts = pd.Timestamp(last_row[timestamp_col]).tz_localize(None)
    target_ts = now_ts.floor("5min")
    if target_ts <= last_ts:
        return current

    gap_hours = (target_ts - last_ts).total_seconds() / 3600.0
    if gap_hours <= 0 or gap_hours > max_gap_hours:
        return current

    if "close" in current.columns and pd.notna(last_row.get("close")):
        carry_price = float(last_row["close"])
        for col in ("open", "high", "low", "close"):
            if col in current.columns:
                last_row[col] = carry_price
    if "volume" in current.columns:
        last_row["volume"] = 0.0

    last_row[timestamp_col] = target_ts
    last_row["__synthetic_continuity"] = True
    if "__synthetic_continuity" not in current.columns:
        current["__synthetic_continuity"] = False

    extended = pd.concat([current, pd.DataFrame([last_row])], ignore_index=True, sort=False)
    extended = extended.drop_duplicates(subset=[timestamp_col], keep="last")
    return extended.sort_values(timestamp_col, kind="stable").reset_index(drop=True)


def resolve_preferred_or_latest_file(
    directory: Path,
    preferred_name: str,
    fallback_pattern: str,
) -> Path | None:
    """Prefer the expected session file, but fall back to the newest matching artifact."""
    preferred = directory / preferred_name
    if preferred.exists():
        return preferred

    candidates = list(directory.glob(fallback_pattern))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def resolve_stock_orders_file(
    directory: Path,
    session_date_str: str,
    mode: str,
) -> Path | None:
    """Resolve stock order exports, preferring mode-scoped files and falling back to legacy names."""
    mode_scoped = resolve_preferred_or_latest_file(
        directory,
        f"STOCK_{session_date_str}_{mode}_orders.json",
        f"STOCK_*_{mode}_orders.json",
    )
    if mode_scoped is not None:
        return mode_scoped

    return resolve_preferred_or_latest_file(
        directory,
        f"STOCK_{session_date_str}_orders.json",
        "STOCK_*_orders.json",
    )


def build_stock_orders_from_trades(
    trades_df: pd.DataFrame | None,
    *,
    default_strategy: str = "",
    mode: str = "PAPER",
) -> list[dict]:
    """Build filled stock-order records from the stock trade ledger."""
    if trades_df is None or trades_df.empty:
        return []

    orders_data: list[dict] = []
    for index, trade in trades_df.iterrows():
        action = str(trade.get("action", "")).upper()
        if action not in {"BUY", "SELL"}:
            continue

        ticker = _stable_string_identifier(trade.get("ticker", ""))
        timestamp = _stable_string_identifier(trade.get("timestamp", ""))
        if not ticker or not timestamp:
            continue

        qty = int(pd.to_numeric(trade.get("qty", 0), errors="coerce") or 0)
        price = float(pd.to_numeric(trade.get("price", 0.0), errors="coerce") or 0.0)
        if qty <= 0 or price <= 0:
            continue

        order_suffix = timestamp.replace("-", "").replace(":", "").replace(" ", "T")
        orders_data.append(
            {
                "order_id": f"{mode}-{action}-{ticker}-{order_suffix}-{index}",
                "ticker": ticker,
                "side": action,
                "order_type": "LMT",
                "qty": qty,
                "filled_qty": qty,
                "price": price,
                "filled_price": price,
                "status": "FILLED",
                "timestamp": timestamp,
                "strategy": _stable_string_identifier(trade.get("strategy", default_strategy)) or default_strategy,
                "mode": mode,
            }
        )

    return orders_data
