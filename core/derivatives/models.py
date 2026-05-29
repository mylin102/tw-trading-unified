"""
OptionQuoteEvent — generic quote event for option surface engine.
SkewSignal — output signal consumed by strategy layer.
SurfaceSnapshot — IV surface snapshot for shape classification.

Design: event-driven, not field-specific. Engine only reads symbol, strike,
option_type, and mid price — adding strikes doesn't change the event schema.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OptionQuoteEvent:
    """A single option quote observation from the bidask stream.

    This is the universal input to OptionSurfaceEngine.
    Adding strikes (e.g., ±100, ±200, ±300, ±500) does NOT change this schema.
    """
    timestamp: datetime.datetime
    symbol: str                              # e.g. TXO33800E6
    option_type: str                         # "CALL" | "PUT"
    strike: float                            # e.g. 33800
    bid: float
    ask: float
    mid: float                               # (bid + ask) / 2
    expiry: str = ""                         # e.g. "202606" or delivery date string


@dataclass
class SkewSignal:
    """Option skew signal — output of OptionSurfaceEngine.compute_if_ready().

    Consumed by strategy layer as a filter / weighting factor / regime modifier.
    When no calculation is possible, direction="UNKNOWN" and confidence=0.0.
    """
    direction: str = "UNKNOWN"               # "UP" | "DOWN" | "NEUTRAL" | "UNKNOWN"
    confidence: float = 0.0                  # 0.0 ~ 1.0
    skew_level: float = 0.0                  # put_price - call_price
    skew_change: float = 0.0                 # change from previous
    put_call_divergence: float = 0.0         # put_change - call_change
    downside_risk: float = 0.0               # OTM put premium
    upside_risk: float = 0.0                 # OTM call premium
    imbalance: float = 0.0                   # downside - upside
    vol_regime: str = "UNKNOWN"              # "EXPANDING" | "COMPRESSING" | "NEUTRAL" | "UNKNOWN"
    timestamp: Optional[datetime.datetime] = None
    underlying_price: float = 0.0

    def is_valid(self) -> bool:
        """True when engine has enough data to compute a signal."""
        return self.direction != "UNKNOWN" and self.confidence > 0

    def to_dict(self) -> dict:
        """Serializable dict for shared_state / StrategyContext injection."""
        return {
            "direction": self.direction,
            "confidence": self.confidence,
            "skew_level": self.skew_level,
            "skew_change": self.skew_change,
            "put_call_divergence": self.put_call_divergence,
            "downside_risk": self.downside_risk,
            "upside_risk": self.upside_risk,
            "imbalance": self.imbalance,
            "vol_regime": self.vol_regime,
            "timestamp": str(self.timestamp) if self.timestamp else None,
            "underlying_price": self.underlying_price,
        }


@dataclass
class SurfaceSnapshot:
    """IV surface snapshot — output of OptionSurfaceEngine.surface_snapshot().

    Contains structured IV data for shape classification and consumption
    by the SkewRegime pipeline.
    """
    atm_iv: float = 0.0
    otm_put_iv: float = 0.0
    otm_call_iv: float = 0.0
    atm_strike: float = 0.0
    otm_put_strike: float = 0.0
    otm_call_strike: float = 0.0
    underlying_price: float = 0.0
    dte: float = 0.0
    timestamp: Optional[datetime.datetime] = None

    # Invalid reason when !is_valid()
    invalid_reason: str = ""

    def is_valid(self) -> bool:
        """True when all three IV values are positive."""
        return self.atm_iv > 0 and self.otm_put_iv > 0 and self.otm_call_iv > 0
