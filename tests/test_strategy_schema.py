"""Unit tests for StrategyParams schema validation."""
import pytest
from core.strategy_schema import StrategyParams, TW_STRATEGY_PRESETS


class TestStrategyParamsValid:
    """Valid parameter combinations should be accepted."""

    def test_defaults(self):
        p = StrategyParams()
        assert p.patterns == []
        assert p.holding_days == 14
        assert p.require_squeeze_on is False
        assert p.require_fired is False
        assert p.min_momentum is None
        assert p.stop_loss_pct is None

    def test_all_presets_are_valid(self):
        """All TW_STRATEGY_PRESETS should instantiate without error."""
        for name, params in TW_STRATEGY_PRESETS.items():
            assert isinstance(params, StrategyParams), f"{name} is not StrategyParams"
            assert params.holding_days >= 1, f"{name} has invalid holding_days"

    def test_custom_params(self):
        p = StrategyParams(
            patterns=["squeeze", "houyi"],
            min_momentum=0.05,
            require_fired=True,
            holding_days=7,
            stop_loss_pct=0.05,
        )
        assert p.patterns == ["squeeze", "houyi"]
        assert p.min_momentum == 0.05
        assert p.require_fired is True
        assert p.holding_days == 7
        assert p.stop_loss_pct == 0.05


class TestStrategyParamsInvalid:
    """Invalid parameter combinations should be rejected."""

    def test_invalid_pattern_raises(self):
        with pytest.raises(ValueError, match="Invalid pattern"):
            StrategyParams(patterns=["invalid_pattern"])

    def test_invalid_regime_raises(self):
        with pytest.raises(ValueError, match="Invalid regime"):
            StrategyParams(allowed_regimes=["moon_phase"])

    def test_zero_holding_days_raises(self):
        with pytest.raises(ValueError, match="holding_days must be >= 1"):
            StrategyParams(holding_days=0)

    def test_extra_field_raises(self):
        with pytest.raises(Exception):  # Pydantic raises ValidationError
            StrategyParams(unknown_field="should_fail")


class TestStrategyPresets:
    """Verify TW strategy preset configurations."""

    def test_squeeze_only_requires_squeeze_on(self):
        p = TW_STRATEGY_PRESETS["squeeze_only"]
        assert "squeeze" in p.patterns
        assert p.require_squeeze_on is True

    def test_whale_alignment_has_whale_pattern(self):
        p = TW_STRATEGY_PRESETS["whale_alignment"]
        assert p.patterns == ["whale"]

    def test_conservative_has_momentum_filter(self):
        p = TW_STRATEGY_PRESETS["conservative"]
        assert p.min_momentum is not None
        assert p.min_momentum > 0

    def test_scalping_has_short_holding(self):
        p = TW_STRATEGY_PRESETS["scalping"]
        assert p.holding_days == 3
        assert p.require_fired is True

    def test_baseline_accepts_all_patterns(self):
        p = TW_STRATEGY_PRESETS["baseline"]
        assert set(p.patterns) == {"squeeze", "houyi", "whale"}
