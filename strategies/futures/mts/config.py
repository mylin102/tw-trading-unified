# 2026-07-24 Gemini CLI: Wave 0 Strongly Typed Config Models & Profile Resolver
from dataclasses import dataclass
from typing import Any
from .contracts import ExitFamily


@dataclass(frozen=True)
class NormalReleaseConfig:
    """Configuration parameters for NormalReleasePolicy."""
    family: ExitFamily = ExitFamily.NORMAL_RELEASE
    release_threshold_mode: str = "ATR_DYNAMIC"
    release_atr_ratio: float = 1.0
    warmup_ms: int = 800
    warmup_ticks: int = 2


@dataclass(frozen=True)
class SpreadPnlTrailConfig:
    """Configuration parameters for SpreadPnlTrailPolicy."""
    family: ExitFamily = ExitFamily.SPREAD_PNL_TRAIL
    arm_profit_twd: int = 800
    arm_atr_ratio: float = 0.8
    trail_atr_ratio: float = 0.8
    fixed_trail_floor_twd: int = 200
    retain_ratio: float = 0.4
    hard_stop_twd: int = -1500


@dataclass(frozen=True)
class ReverseHarvestConfig:
    """Configuration parameters for ReverseHarvestPolicy."""
    family: ExitFamily = ExitFamily.REVERSE_HARVEST
    harvest_atr_ratio: float = 1.0
    harvest_confirm_ticks: int = 2
    harvest_confirm_ms: int = 500
    loser_hard_stop_twd: int = -1800
    recovery_timeout_seconds: int = 900
    retain_ratio: float = 0.25


class ProfileResolver:
    """Resolves base configuration + session overrides into a resolved config object."""

    @staticmethod
    def resolve_spread_pnl_trail(profile_dict: dict[str, Any], session: str) -> SpreadPnlTrailConfig:
        """Resolve SpreadPnlTrailConfig applying session overrides (DAY/NIGHT)."""
        base = profile_dict.get("base", profile_dict)
        overrides = profile_dict.get("overrides", {}).get(session.upper(), {})
        
        merged = dict(base)
        merged.update(overrides)
        
        return SpreadPnlTrailConfig(
            family=ExitFamily.SPREAD_PNL_TRAIL,
            arm_profit_twd=int(merged.get("arm_profit_twd", 800)),
            arm_atr_ratio=float(merged.get("arm_atr_ratio", 0.8)),
            trail_atr_ratio=float(merged.get("trail_atr_ratio", 0.8)),
            fixed_trail_floor_twd=int(merged.get("fixed_trail_floor_twd", 200)),
            retain_ratio=float(merged.get("retain_ratio", 0.4)),
            hard_stop_twd=int(merged.get("hard_stop_twd", -1500)),
        )
