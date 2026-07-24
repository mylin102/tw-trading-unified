# 2026-07-24 Gemini CLI: Wave 0 Strongly Typed Immutable Policy State Models
from dataclasses import dataclass
from typing import Union
from .contracts import Leg


@dataclass(frozen=True)
class NormalReleaseState:
    """Immutable state holder for NormalReleasePolicy."""
    released_leg: Leg | None = None
    warmup_started_at_ns: int | None = None
    release_triggered_at_ns: int | None = None
    single_leg_active: bool = False


@dataclass(frozen=True)
class SpreadPnlTrailState:
    """Immutable state holder for SpreadPnlTrailPolicy."""
    armed: bool = False
    combined_peak_pnl_twd: int = 0
    armed_at_ns: int | None = None


@dataclass(frozen=True)
class ReverseHarvestState:
    """Immutable state holder for ReverseHarvestPolicy."""
    winner_leg: Leg | None = None
    winner_peak_upl_twd: int = 0
    winner_harvested: bool = False
    loser_recovery_armed: bool = False
    loser_recovery_peak_twd: int = 0
    recovery_started_at_ns: int | None = None


PolicyState = Union[NormalReleaseState, SpreadPnlTrailState, ReverseHarvestState]
