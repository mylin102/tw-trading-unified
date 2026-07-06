"""Tests for ETF Regime Consumer."""

import pytest
from core.etf_regime_consumer import (
    REGIME_ADJUSTMENTS,
    DEFAULT_ADJUSTMENTS,
    get_regime_adjustments,
    REGIME_KEYS,
)


class TestGetRegimeAdjustments:
    def test_risk_on_adjustments(self):
        """RISK_ON should lower ORB threshold, increase size."""
        data = {"regime": "RISK_ON", "confidence": 0.8}
        adj = get_regime_adjustments(data)
        assert adj["orb_threshold_mult"] < 1.0
        assert adj["orb_size_mult"] > 1.0
        assert adj["size_mult"] > 1.0
        assert adj["allow_scale"] == 1.0
        assert adj["scout_only"] == 0.0

    def test_risk_off_reduces_size(self):
        """RISK_OFF should reduce size and disable scale."""
        data = {"regime": "RISK_OFF", "confidence": 0.7}
        adj = get_regime_adjustments(data)
        assert adj["orb_size_mult"] < 1.0
        assert adj["size_mult"] < 1.0
        assert adj["allow_scale"] == 0.0
        assert adj["scout_only"] == 1.0

    def test_defensive_reduces_orb(self):
        """DEFENSIVE should increase ORB threshold and reduce size."""
        data = {"regime": "DEFENSIVE", "confidence": 0.6}
        adj = get_regime_adjustments(data)
        assert adj["orb_threshold_mult"] > 1.0  # harder to trigger
        assert adj["orb_size_mult"] < 1.0
        assert adj["size_mult"] < 1.0

    def test_chop_defaults(self):
        """CHOP should use neutral adjustments."""
        data = {"regime": "CHOP", "confidence": 0.5}
        adj = get_regime_adjustments(data)
        assert adj["orb_threshold_mult"] == 1.0
        assert adj["size_mult"] == 1.0
        assert adj["scout_only"] == 1.0

    def test_low_confidence_degrades(self):
        """Confidence < 0.3 should halve adjustments toward neutral."""
        data = {"regime": "RISK_ON", "confidence": 0.2}
        adj = get_regime_adjustments(data)
        # Full RISK_ON: size_mult=1.2 → halved: 1.0 + (1.2-1.0)*0.5 = 1.1
        expected_size = 1.0 + (1.2 - 1.0) * 0.5
        assert adj["size_mult"] == pytest.approx(expected_size)
        assert adj["degraded"] is False  # degraded flag not set, but low confidence triggered halving

    def test_degraded_flag_halves(self):
        """Explicit degraded=True should halve adjustments."""
        data = {"regime": "RISK_ON", "confidence": 0.6, "degraded": True}
        adj = get_regime_adjustments(data)
        expected_size = 1.0 + (1.2 - 1.0) * 0.5
        assert adj["size_mult"] == pytest.approx(expected_size)
        assert adj["degraded"] is True

    def test_unknown_regime_defaults_to_chop(self):
        """Unknown regime should fall back to CHOP adjustments."""
        data = {"regime": "UNKNOWN", "confidence": 0.5}
        adj = get_regime_adjustments(data)
        assert adj["regime"] == "CHOP"

    def test_no_data_returns_defaults(self):
        """Empty data should return CHOP with neutral adjustments."""
        data = {}
        adj = get_regime_adjustments(data)
        assert adj["regime"] == "CHOP"

    def test_metadata_in_output(self):
        """Output should include regime, confidence, degraded."""
        data = {"regime": "RISK_ON", "confidence": 0.8, "degraded": False}
        adj = get_regime_adjustments(data)
        assert adj["regime"] == "RISK_ON"
        assert adj["confidence"] == 0.8
        assert adj["degraded"] is False

    def test_all_regimes_have_same_keys(self):
        """Every regime should have the same adjustment keys."""
        keys = set(REGIME_ADJUSTMENTS["RISK_ON"].keys())
        for regime in REGIME_KEYS:
            assert set(REGIME_ADJUSTMENTS[regime].keys()) == keys, f"{regime} keys mismatch"

    def test_default_vs_chop_agree_on_neutral_keys(self):
        """DEFAULT_ADJUSTMENTS should match CHOP on neutral keys only.

        CHOP intentionally tightens VWAP (vwap_threshold_mult=0.97),
        so not all keys match. Check only the truly neutral ones.
        """
        neutral_keys = {"orb_threshold_mult", "orb_size_mult", "size_mult"}
        for key in neutral_keys:
            assert DEFAULT_ADJUSTMENTS[key] == REGIME_ADJUSTMENTS["CHOP"][key], f"{key} mismatch"

    def test_regime_values_in_range(self):
        """All adjustment values should be in reasonable range."""
        for regime, adj in REGIME_ADJUSTMENTS.items():
            for key, value in adj.items():
                assert 0.0 <= value <= 2.0, f"{regime}.{key}={value} out of range [0, 2]"
