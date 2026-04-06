"""
Strategy parameter schema for squeeze pattern integration.
Pydantic model with validation for strategy preset parameters.
"""
from typing import Optional, List
from pydantic import BaseModel, ConfigDict, field_validator


class StrategyParams(BaseModel):
    """Validated strategy parameters for squeeze pattern filtering."""
    model_config = ConfigDict(extra="forbid")

    # Signal filters
    min_momentum: Optional[float] = None
    max_momentum: Optional[float] = None
    min_energy_level: Optional[int] = None
    require_squeeze_on: bool = False
    require_fired: bool = False
    min_value_score: Optional[float] = None

    # Pattern selection
    patterns: List[str] = []
    allowed_regimes: Optional[List[str]] = None

    # Exit / holding
    holding_days: int = 14
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None

    @field_validator("patterns")
    @classmethod
    def validate_patterns(cls, v: List[str]) -> List[str]:
        valid = {"squeeze", "houyi", "whale"}
        for p in v:
            if p not in valid:
                raise ValueError(f"Invalid pattern: {p}. Must be one of {valid}")
        return v

    @field_validator("allowed_regimes")
    @classmethod
    def validate_regimes(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        valid = {"bull_trend", "bear_trend", "range_bound"}
        for r in v:
            if r not in valid:
                raise ValueError(f"Invalid regime: {r}. Must be one of {valid}")
        return v

    @field_validator("holding_days")
    @classmethod
    def validate_holding_days(cls, v: int) -> int:
        if v < 1:
            raise ValueError("holding_days must be >= 1")
        return v


# TW-optimized strategy presets
TW_STRATEGY_PRESETS = {
    "baseline": StrategyParams(
        patterns=["squeeze", "houyi", "whale"],
        holding_days=14,
    ),
    "squeeze_only": StrategyParams(
        patterns=["squeeze"],
        require_squeeze_on=True,
        holding_days=14,
    ),
    "whale_alignment": StrategyParams(
        patterns=["whale"],
        holding_days=10,
    ),
    "conservative": StrategyParams(
        patterns=["squeeze", "whale"],
        min_momentum=0.02,
        require_squeeze_on=True,
        holding_days=14,
    ),
    "scalping": StrategyParams(
        patterns=["squeeze"],
        min_momentum=0.15,
        require_fired=True,
        holding_days=3,
    ),
    "houyi_specialist": StrategyParams(
        patterns=["houyi"],
        holding_days=5,
    ),
    "custom": StrategyParams(),
}
