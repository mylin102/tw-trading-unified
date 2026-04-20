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
