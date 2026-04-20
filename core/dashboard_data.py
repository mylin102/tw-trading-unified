from __future__ import annotations

import pandas as pd


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
