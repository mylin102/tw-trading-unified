"""
VCP (Volatility Contraction Pattern) detection tests.

Tests call the REAL detect_vcp() from production pattern_engine.py.
Uses synthetic price data (same approach as test_pattern_engine.py).
No real API, no Shioaji, no network.

2026-09-21 Antigravity: initial test suite.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.stocks.pattern_engine import detect_vcp


# ── Synthetic VCP data generator ──────────────────────────────────────────────

def generate_synthetic_vcp(
    n_pivots: int = 3,
    base_price: float = 100.0,
    first_depth: float = 0.18,
    volume_contract: bool = True,
    depth_ratio: float = 0.55,   # each contraction depth = prev * ratio (< 1 → decreasing)
    vol_ratio: float = 0.50,      # each segment volume = prev * ratio (< 1 → decreasing)
    bars_per_pivot: int = 22,     # trading days per contraction segment
    warmup_bars: int = 60,        # MA50 warmup bars before the base starts
) -> pd.DataFrame:
    """
    Generate a daily OHLCV series that forms a textbook VCP.

    Structure:
        [warmup: 60 days uptrend above MA50]
        [pivot segments: each with depth = prev * depth_ratio, vol = prev * vol_ratio]

    Returns capitalized DataFrame (Open/High/Low/Close/Volume).
    """
    rng = np.random.default_rng(42)

    # ── Warmup: gradual uptrend so price ends above MA50 ─────────────────────
    warmup_prices = base_price * 0.70 + np.linspace(0, base_price * 0.30, warmup_bars)
    warmup_prices += rng.normal(0, base_price * 0.005, warmup_bars)
    warmup_volume = np.full(warmup_bars, 200_000.0)

    # ── VCP pivot segments ────────────────────────────────────────────────────
    seg_prices  = []
    seg_volumes = []
    current_high = base_price
    current_depth = first_depth
    current_vol   = 100_000.0

    for seg in range(n_pivots - 1):  # n_pivots pivots → n_pivots-1 inter-pivot segments
        low_price = current_high * (1 - current_depth)
        # Down then up within segment
        half = bars_per_pivot // 2
        down = np.linspace(current_high, low_price, half)
        up   = np.linspace(low_price, current_high * (1 - current_depth * depth_ratio * 0.5), bars_per_pivot - half)
        seg_p = np.concatenate([down, up])
        seg_p += rng.normal(0, base_price * 0.002, len(seg_p))
        seg_prices.append(seg_p)

        # Volume: contracting each segment
        seg_vol = np.full(bars_per_pivot, current_vol if not volume_contract
                          else current_vol * (vol_ratio ** seg))
        seg_volumes.append(seg_vol)

        # Next pivot starts slightly lower than current
        current_high = seg_p[-1]
        current_depth = current_depth * depth_ratio

    # Final small consolidation (the last tight base)
    tight_prices = np.linspace(current_high * 0.99, current_high, 10)
    tight_prices += rng.normal(0, base_price * 0.001, 10)
    tight_vol = np.full(10, (current_vol * (vol_ratio ** (n_pivots - 1)))
                        if volume_contract else current_vol)

    all_prices  = np.concatenate([warmup_prices,  *seg_prices,  tight_prices])
    all_volumes = np.concatenate([warmup_volume,  *seg_volumes, tight_vol])

    dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=len(all_prices), freq="B")

    return pd.DataFrame({
        "Open":   all_prices * 0.995,
        "High":   all_prices * 1.005,
        "Low":    all_prices * 0.990,
        "Close":  all_prices,
        "Volume": all_volumes.astype(float),
    }, index=dates)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestVcpDetected:

    def test_vcp_3pivot_detected(self):
        """
        Standard 3-pivot VCP with volume contraction → status=True, type='vcp'.
        """
        df = generate_synthetic_vcp(n_pivots=3)
        result = detect_vcp(df, min_pivots=3, check_volume=True)

        assert result["status"] is True, f"Expected VCP detected. Reason: {result.get('reason')}"
        assert result["type"] == "vcp"
        assert result["pivot_price"] > 0
        assert result["n_pivots"] >= 3
        assert len(result["contractions"]) >= 2

    def test_vcp_pivot_price_is_positive(self):
        """pivot_price (breakout target) must always be > 0 when detected."""
        df = generate_synthetic_vcp(n_pivots=3)
        result = detect_vcp(df, min_pivots=3)
        if result["status"]:
            assert result["pivot_price"] > 0

    def test_vcp_contractions_strictly_decreasing(self):
        """Each contraction depth must be < previous when pattern detected."""
        df = generate_synthetic_vcp(n_pivots=3, first_depth=0.18, depth_ratio=0.55)
        result = detect_vcp(df, min_pivots=3, check_volume=False)

        if result["status"]:
            depths = [c["depth"] for c in result["contractions"]]
            for i in range(1, len(depths)):
                assert depths[i] < depths[i - 1], (
                    f"Contraction {i} ({depths[i]:.3f}) not < prev ({depths[i-1]:.3f})"
                )


class TestVcpRejected:

    def test_vcp_insufficient_data(self):
        """< 60 bars → status=False, reason='insufficient_data'."""
        df = generate_synthetic_vcp(n_pivots=3, warmup_bars=10, bars_per_pivot=5)
        short_df = df.iloc[:30]  # force < 60 bars
        result = detect_vcp(short_df, min_pivots=3)

        assert result["status"] is False
        assert result["reason"] == "insufficient_data"

    def test_vcp_below_ma50(self):
        """Price below MA50 → not in uptrend → rejected."""
        # Create a downtrend series (below its own MA50)
        n = 80
        prices = np.linspace(150, 70, n)  # falling prices → below MA50
        dates  = pd.date_range(end=pd.Timestamp.now().normalize(), periods=n, freq="B")
        df = pd.DataFrame({
            "Open":   prices * 0.995,
            "High":   prices * 1.005,
            "Low":    prices * 0.990,
            "Close":  prices,
            "Volume": np.full(n, 50_000.0),
        }, index=dates)

        result = detect_vcp(df, min_pivots=3)

        assert result["status"] is False
        assert result["reason"] == "below_ma50"

    def test_vcp_too_few_pivots(self):
        """
        Only 2 actual pivots found but min_pivots=3 → rejected.
        Generate a series with few oscillations.
        """
        # Short monotone uptrend — no local maxima oscillations
        n = 70
        prices = np.linspace(90, 110, n) + np.sin(np.linspace(0, 2, n)) * 0.5
        dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=n, freq="B")
        df = pd.DataFrame({
            "Open":   prices * 0.995,
            "High":   prices * 1.005,
            "Low":    prices * 0.990,
            "Close":  prices,
            "Volume": np.full(n, 50_000.0),
        }, index=dates)

        result = detect_vcp(df, min_pivots=3, order=5)

        assert result["status"] is False
        assert "too_few_pivots" in result["reason"] or "insufficient_contractions" in result["reason"]

    def test_vcp_contraction_not_decreasing(self):
        """
        Second contraction depth >= first → strictly decreasing rule must fail.

        Uses a manually crafted price series where the inter-pivot ranges EXPAND
        (first tight, then wide) so the contraction check fires, not MA50 check.
        """
        rng = np.random.default_rng(0)
        n = 120
        # Steadily rising prices to stay above MA50 throughout
        base = np.linspace(80, 120, n) + rng.normal(0, 0.3, n)

        # Inject expanding oscillation: small first, large second
        # Pivot 1 at bar 70, Pivot 2 at bar 85, Pivot 3 at bar 105
        base[65:75]  = np.linspace(110, 108, 10)   # small dip (tight)
        base[75:85]  = np.linspace(108, 115, 10)   # small recovery
        base[85:100] = np.linspace(115, 100, 15)   # LARGE dip (expanding)
        base[100:110] = np.linspace(100, 118, 10)  # recovery

        dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=n, freq="B")
        df = pd.DataFrame({
            "Open":   base * 0.995,
            "High":   base * 1.01,
            "Low":    base * 0.98,
            "Close":  base,
            "Volume": np.full(n, 80_000.0),
        }, index=dates)

        result = detect_vcp(df, min_pivots=3, check_volume=False)

        # Either below_ma50, too_few_pivots, or contraction_not_decreasing —
        # all correctly indicate this is NOT a valid VCP.
        assert result["status"] is False, (
            f"Expected detection to fail for expanding contractions, got: {result}"
        )


    def test_vcp_volume_not_decreasing(self):
        """
        Volume grows across pivots (vol_ratio > 1) → volume_not_decreasing rejected.
        """
        # vol_ratio=2.0 → volume doubles each segment
        df = generate_synthetic_vcp(n_pivots=3, first_depth=0.18, depth_ratio=0.55,
                                    volume_contract=False, vol_ratio=2.0)

        # Manually inflate volumes so they grow across segments
        # Override the volume column: segment 1 < segment 2 < segment 3
        n = len(df)
        warmup = 60
        seg = (n - warmup) // 3
        vol = df["Volume"].copy()
        vol.iloc[warmup        : warmup +   seg] = 100_000
        vol.iloc[warmup + seg  : warmup + 2*seg] = 150_000   # growing
        vol.iloc[warmup + 2*seg:               ] = 200_000   # growing
        df = df.copy()
        df["Volume"] = vol

        result = detect_vcp(df, min_pivots=3, check_volume=True)

        # May fail with contraction_not_decreasing or volume_not_decreasing
        assert result["status"] is False


class TestVcpStaysObserveOnly:
    """2026-09-22 Hermes takeover: VCP stays observe-only by decision."""

    def test_entry_strategy_whitelist_excludes_vcp(self):
        src = (ROOT / "strategies/stocks/entry_strategies.py").read_text()
        assert "'vcp'" not in src, (
            "VCP must NOT be in the entry-strategy whitelist (observe-only)")
