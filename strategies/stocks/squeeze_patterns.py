"""
Squeeze pattern classification for Taiwan stocks.
Classifies houyi and whale patterns from existing 5min indicator columns.

Patterns:
- squeeze: BB inside KC compression (already computed by calculate_futures_squeeze)
- houyi (後羿射日): Strong momentum burst after extended squeeze — "shooting sun"
- whale: Multi-timeframe EMA alignment with volume confirmation — institutional flow
"""
import pandas as pd
import numpy as np


def classify_houyi(df: pd.DataFrame) -> pd.Series:
    """
    後羿射日 pattern: Strong momentum burst after squeeze compression.

    Conditions:
    1. Squeeze just FIRED (transition from compression to expansion)
    2. Momentum state >= 2 (acceleration phase)
    3. Momentum velocity > 0 (directional force)

    Returns boolean Series: True where houyi pattern detected.
    """
    fired = df.get("fired", pd.Series(False, index=df.index))
    mom_state = df.get("mom_state", pd.Series(0, index=df.index))
    mom_velo = df.get("mom_velo", pd.Series(0.0, index=df.index))

    # Houyi: fired + strong momentum + positive velocity
    is_houyi = (
        fired
        & (mom_state >= 2)
        & (mom_velo > 0)
    )

    return is_houyi


def classify_whale(df: pd.DataFrame) -> pd.Series:
    """
    鯨魚 pattern: Multi-timeframe EMA alignment with institutional flow.

    Conditions:
    1. Bullish alignment (EMA fast > EMA slow > EMA macro)
    2. OR Bearish alignment (EMA fast < EMA slow < EMA macro)
    3. Volume above 20-bar average (institutional participation)
    4. ADX > 15 (trend strength, if available)

    Returns boolean Series: True where whale pattern detected.
    """
    bullish = df.get("bullish_align", pd.Series(False, index=df.index))
    bearish = df.get("bearish_align", pd.Series(False, index=df.index))
    volume = df.get("Volume", pd.Series(0, index=df.index))

    # Volume confirmation: current volume > 20-bar SMA
    vol_sma = volume.rolling(20, min_periods=20).mean()
    vol_confirm = volume > vol_sma

    # Whale: aligned trend + volume confirmation
    is_whale = (bullish | bearish) & vol_confirm

    # ADX filter: only apply if column exists and has non-NaN values
    if "adx" in df.columns:
        adx = df["adx"]
        has_adx = adx.notna()
        is_whale = is_whale & (~has_adx | (adx > 15))

    return is_whale


def apply_squeeze_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add 'pattern' column to DataFrame with squeeze pattern classifications.

    Priority: houyi > whale > squeeze (if multiple match, take highest priority)

    Returns DataFrame with additional 'pattern' column:
        'houyi' | 'whale' | 'squeeze' | None
    """
    df = df.copy()
    sqz_on = df.get("sqz_on", pd.Series(False, index=df.index))
    fired = df.get("fired", pd.Series(False, index=df.index))

    is_houyi = classify_houyi(df)
    is_whale = classify_whale(df)
    is_squeeze = sqz_on | fired  # Any squeeze activity

    # Priority assignment
    df["pattern"] = None
    df.loc[is_squeeze, "pattern"] = "squeeze"
    df.loc[is_whale, "pattern"] = "whale"
    df.loc[is_houyi, "pattern"] = "houyi"

    return df
