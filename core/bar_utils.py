from __future__ import annotations

from collections.abc import Callable

import pandas as pd
from core.date_utils import get_session, get_trading_day


OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def empty_ohlcv_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=OHLCV_COLUMNS)


def canonicalize_ohlcv(df: pd.DataFrame | None) -> pd.DataFrame:
    """Normalize an OHLCV dataframe to the shared canonical contract.

    Contract:
    - DatetimeIndex sorted ascending
    - Columns: Open/High/Low/Close/Volume only
    - No strategy-specific enrichment in this layer
    """
    if df is None or len(df) == 0:
        return empty_ohlcv_frame()

    missing = [col for col in OHLCV_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"Missing OHLCV columns: {missing}")

    out = df[OHLCV_COLUMNS].copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)
    return out.sort_index()


def resample_ohlcv(df: pd.DataFrame | None, rule: str) -> pd.DataFrame:
    """Resample canonical OHLCV data into a higher timeframe using shared semantics."""
    base = canonicalize_ohlcv(df)
    if base.empty:
        return base

    return base.resample(rule).agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna()


def attach_bar_metadata(df: pd.DataFrame | None) -> pd.DataFrame:
    """Attach shared session metadata to canonical or enriched bar dataframes."""
    if df is None or len(df) == 0:
        return df if df is not None else empty_ohlcv_frame()

    out = df.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)

    out["trading_day"] = get_trading_day(out.index)
    out["session"] = [get_session(ts) for ts in out.index]
    return out


def get_bar_freshness_minutes(df: pd.DataFrame | None, now: pd.Timestamp | None = None) -> float | None:
    """Return how old the latest bar is in minutes."""
    if df is None or len(df) == 0:
        return None

    latest_ts = pd.Timestamp(df.index[-1])
    current_ts = pd.Timestamp.now() if now is None else pd.Timestamp(now)
    age = (current_ts - latest_ts).total_seconds() / 60.0
    return max(0.0, age)


def fill_small_ohlcv_gaps(
    df: pd.DataFrame | None,
    *,
    expected_freq: str = "5min",
    max_gap_minutes: int = 15,
) -> pd.DataFrame:
    """Forward-fill small canonical OHLCV gaps without bridging session boundaries."""
    base = canonicalize_ohlcv(df)
    if len(base) < 2:
        return base

    step = pd.Timedelta(expected_freq)
    step_minutes = max(1, int(step.total_seconds() / 60))
    fill_limit = max(1, min(3, max_gap_minutes // step_minutes))

    rows: list[tuple[pd.Timestamp, pd.Series]] = []
    prev_ts = pd.Timestamp(base.index[0])
    prev_row = base.iloc[0].copy()
    rows.append((prev_ts, prev_row))

    for idx in range(1, len(base)):
        curr_ts = pd.Timestamp(base.index[idx])
        curr_row = base.iloc[idx].copy()
        gap_minutes = (curr_ts - prev_ts).total_seconds() / 60.0
        same_session = (
            get_session(prev_ts) == get_session(curr_ts)
            and get_trading_day(prev_ts) == get_trading_day(curr_ts)
        )
        if expected_freq and same_session and gap_minutes > step_minutes and gap_minutes <= max_gap_minutes:
            fill_index = pd.date_range(
                start=prev_ts + step,
                end=curr_ts - step,
                freq=expected_freq,
            )[:fill_limit]
            for fill_ts in fill_index:
                rows.append((pd.Timestamp(fill_ts), prev_row.copy()))
        rows.append((curr_ts, curr_row))
        prev_ts = curr_ts
        prev_row = curr_row

    out = pd.DataFrame([row for _, row in rows], index=[ts for ts, _ in rows])
    return canonicalize_ohlcv(out)


def validate_ohlcv_bars(
    df: pd.DataFrame | None,
    *,
    min_bars: int,
    expected_interval_minutes: int = 5,
    max_intraday_gap_minutes: int = 30,
    max_session_gap_minutes: int = 380,
) -> tuple[bool, str]:
    """Validate canonical OHLCV data with session-aware gap allowances."""
    base = canonicalize_ohlcv(df)
    if base.empty:
        return False, "資料為空"

    if len(base) < min_bars:
        return False, f"資料不足: {len(base)}根 < {min_bars}根"

    for col in OHLCV_COLUMNS:
        nan_count = base[col].isna().sum()
        if nan_count > 0:
            return False, f"欄位 {col} 有 {nan_count} 個NaN值"
        if col != "Volume" and (base[col] <= 0).any():
            return False, f"欄位 {col} 有非正數值"

    if len(base) > 1:
        for prev_ts, curr_ts in zip(base.index[:-1], base.index[1:]):
            gap_minutes = (pd.Timestamp(curr_ts) - pd.Timestamp(prev_ts)).total_seconds() / 60.0
            same_session = (
                get_session(prev_ts) == get_session(curr_ts)
                and get_trading_day(prev_ts) == get_trading_day(curr_ts)
            )
            allowed_gap = max_intraday_gap_minutes if same_session else max_session_gap_minutes
            if gap_minutes > allowed_gap:
                return False, f"資料缺口過大: {gap_minutes:.0f}分鐘"

            if same_session and expected_interval_minutes > 0 and gap_minutes < expected_interval_minutes:
                return False, f"資料間隔過短: {gap_minutes:.0f}分鐘"

    return True, "資料完整"


def build_canonical_bar_frames(
    df: pd.DataFrame | None,
    *,
    source_timeframe: str,
    max_gap_minutes: int = 15,
) -> dict[str, pd.DataFrame]:
    """Build shared 5m/15m/1h canonical frames from either 1m or 5m source data."""
    base = canonicalize_ohlcv(df)
    if base.empty:
        return {}

    if source_timeframe == "1min":
        base_5m = resample_ohlcv(base, "5min")
    elif source_timeframe == "5min":
        base_5m = base
    else:
        raise ValueError(f"Unsupported source timeframe: {source_timeframe}")

    base_5m = fill_small_ohlcv_gaps(base_5m, expected_freq="5min", max_gap_minutes=max_gap_minutes)
    if base_5m.empty:
        return {}

    frames = {"5m": attach_bar_metadata(base_5m)}
    for label, rule in (("15m", "15min"), ("1h", "1h")):
        resampled = resample_ohlcv(base_5m, rule)
        if not resampled.empty:
            frames[label] = attach_bar_metadata(resampled)
    return frames


def build_preferred_canonical_bar_frames(
    candidates: list[dict[str, object]],
    *,
    min_5m_bars: int = 2,
    now: pd.Timestamp | None = None,
    max_gap_minutes: int = 15,
    validator: Callable[[pd.DataFrame], tuple[bool, str]] | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, object]]:
    """Pick the first usable source and return shared canonical frames plus diagnostics."""
    rejected: list[str] = []
    for candidate in candidates:
        name = str(candidate.get("name", "unknown"))
        frame = candidate.get("frame")
        timeframe = str(candidate.get("source_timeframe", "5min"))
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            rejected.append(f"{name}:empty")
            continue

        frames = build_canonical_bar_frames(
            frame,
            source_timeframe=timeframe,
            max_gap_minutes=max_gap_minutes,
        )
        df_5m = frames.get("5m")
        if df_5m is None or len(df_5m) < min_5m_bars:
            rejected.append(f"{name}:insufficient_5m_bars")
            continue

        if validator is not None:
            is_valid, reason = validator(df_5m)
            if not is_valid:
                rejected.append(f"{name}:{reason}")
                continue

        diagnostics = {
            "source": name,
            "source_timeframe": timeframe,
            "freshness_minutes": get_bar_freshness_minutes(df_5m, now=now),
            "rejected": rejected,
        }
        return frames, diagnostics

    return {}, {"source": None, "rejected": rejected, "freshness_minutes": None}
