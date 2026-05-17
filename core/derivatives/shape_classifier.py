"""
IV Curve Shape Classifier + VolatilityContext — ref_250516.md Section 2.

Classifies the IV curve shape into two independent dimensions:

1. directional_skew: LEFT / RIGHT / SYMMETRIC
   - Put wing vs call wing steepness (bounded [-1,1] slope_ratio)

2. tension: LOW / MEDIUM / HIGH
   - Parallel shift magnitude (atm_iv_change, the "how much did all IVs lift")

Combined with vol_state (from VolatilityContext) and iv_percentile (from
separate engine), this forms the complete Volatility Surface Regime.

ALGORITHM
---------
put_slope = otm_put_iv - atm_iv
call_slope = otm_call_iv - atm_iv

slope_ratio = (call_slope - put_slope) / (abs(call_slope) + abs(put_slope) + EPS)

- slope_ratio < -SKEW_THRESHOLD  → directional_skew = LEFT
- slope_ratio > +SKEW_THRESHOLD  → directional_skew = RIGHT
- else → SYMMETRIC

Tension from atm_iv_change:
- atm_iv_change < TENSION_LOW   → LOW
- atm_iv_change > TENSION_HIGH  → HIGH
- else → MEDIUM

These are independent axes: LEFT + HIGH = crash hedging,
SYMMETRIC + HIGH = universal panic (not "neutral").
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Output model — the SSOT for all volatility surface downstream consumption
# ---------------------------------------------------------------------------

EPS = 1e-10


@dataclass
class VolatilityContext:
    """Volatility Surface Regime — SSOT consumed by all strategy layers.

    This is NOT a raw dump of indicators. It is a pre-interpreted policy
    context. Every field here has gone through:
      1. IV calculation (iv_calculator)
      2. Surface snapshot (surface_engine)
      3. Shape + tension classification (IVShapeClassifier)
      4. IV percentile, z-score (IVPercentileEngine) — optional, populated
         when the percentile engine is attached and ready.

    Fields
    ------
    directional_skew: "LEFT" | "RIGHT" | "SYMMETRIC" | "UNKNOWN"
    tension: "LOW" | "MEDIUM" | "HIGH" | "UNKNOWN"
    slope_ratio: float, bounded [-1, 1]
    atm_iv_change: float
    delta_slope_ratio: float
    confidence: float [0, 1]

    iv_percentile: [0, 1] — rolling percentile of ATM IV (optional)
    iv_zscore: float — standard deviations from rolling mean (optional)

    timestamp / underlying_price / atm_iv / otm_put_iv / otm_call_iv

    --- Legacy fields ---
    shape / vol_regime
    """
    # New dimensions
    directional_skew: str = "UNKNOWN"
    tension: str = "UNKNOWN"

    # Continuous metrics
    slope_ratio: float = 0.0
    atm_iv_change: float = 0.0
    delta_slope_ratio: float = 0.0
    confidence: float = 0.0

    # IV percentile / z-score (populated by IVPercentileEngine)
    iv_percentile: float = 0.0
    iv_zscore: float = 0.0

    # Absolute IV values (raw data)
    timestamp: Optional[datetime.datetime] = None
    underlying_price: float = 0.0
    atm_iv: float = 0.0
    otm_put_iv: float = 0.0
    otm_call_iv: float = 0.0

    # --- Legacy compat ---
    shape: str = "UNKNOWN"
    vol_regime: str = "UNKNOWN"

    def to_dict(self) -> dict:
        """Serializable dict for shared_state injection.

        Strategies should consume directional_skew + tension, not shape.
        shape is kept for backward compat during migration.
        """
        return {
            "directional_skew": self.directional_skew,
            "tension": self.tension,
            "slope_ratio": self.slope_ratio,
            "atm_iv_change": self.atm_iv_change,
            "delta_slope_ratio": self.delta_slope_ratio,
            "confidence": self.confidence,
            "iv_percentile": self.iv_percentile,
            "iv_zscore": self.iv_zscore,
            "timestamp": str(self.timestamp) if self.timestamp else None,
            "underlying_price": self.underlying_price,
            "atm_iv": self.atm_iv,
            "otm_put_iv": self.otm_put_iv,
            "otm_call_iv": self.otm_call_iv,
            # Legacy
            "shape": self.shape,
            "vol_regime": self.vol_regime,
        }


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class IVShapeClassifier:
    """Classify IV curve shape from (otm_put_iv, atm_iv, otm_call_iv).

    Outputs VolatilityContext with two independent dimensions:
      directional_skew + tension
    plus velocity tracking via previous snapshot.

    Hysteresis (state transition stability) is NOT built-in.
    For production use, wrap with VolatilityStateMachine.
    """

    # Skew thresholds
    SKEW_THRESHOLD: float = 0.30       # |slope_ratio| beyond this → skew detected

    # Tension thresholds (absolute atm_iv_change)
    TENSION_LOW: float = 0.01           # below this → LOW tension
    TENSION_HIGH: float = 0.03          # above this → HIGH tension

    # Confidence
    MIN_CONFIDENCE: float = 0.10
    MAX_CONFIDENCE: float = 0.95

    def __init__(self):
        self._prev: Optional[dict] = None  # stores previous slope_ratio + atm_iv

    def classify(
        self,
        atm_iv: float,
        otm_put_iv: float,
        otm_call_iv: float,
        underlying_price: float,
        timestamp: Optional[datetime.datetime] = None,
    ) -> VolatilityContext:
        """Classify the IV curve shape from three IV observations.

        Parameters
        ----------
        atm_iv: IV of the ATM option (call or put, whichever is closer)
        otm_put_iv: IV of the OTM put (futures - ~300 pts)
        otm_call_iv: IV of the OTM call (futures + ~300 pts)
        underlying_price: current futures price
        timestamp: observation timestamp

        Returns
        -------
        VolatilityContext with directional_skew + tension + legacy shape.
        """
        ts = timestamp or datetime.datetime.utcnow()

        # Guard: insufficient data
        if any(v is None or v <= 0 for v in [atm_iv, otm_put_iv, otm_call_iv]):
            return VolatilityContext(
                directional_skew="UNKNOWN",
                tension="UNKNOWN",
                shape="UNKNOWN",
                timestamp=ts,
                underlying_price=underlying_price,
            )

        # Compute slopes
        put_slope = otm_put_iv - atm_iv
        call_slope = otm_call_iv - atm_iv

        # ---- Dimension 1: Directional Skew ----
        denominator = abs(call_slope) + abs(put_slope) + EPS
        slope_ratio = (call_slope - put_slope) / denominator

        if slope_ratio < -self.SKEW_THRESHOLD:
            directional_skew = "LEFT"
        elif slope_ratio > self.SKEW_THRESHOLD:
            directional_skew = "RIGHT"
        else:
            directional_skew = "SYMMETRIC"

        # ---- Dimension 2: Tension (Parallel Shift Magnitude) ----
        atm_iv_change = 0.0
        if self._prev is not None:
            atm_iv_change = abs(atm_iv - self._prev.get("atm_iv", atm_iv))

        if atm_iv_change < self.TENSION_LOW:
            tension = "LOW"
        elif atm_iv_change > self.TENSION_HIGH:
            tension = "HIGH"
        else:
            tension = "MEDIUM"

        # ---- Velocity ----
        delta_slope_ratio = 0.0
        if self._prev is not None:
            prev_slope = self._prev.get("slope_ratio", slope_ratio)
            delta_slope_ratio = slope_ratio - prev_slope

        # ---- Confidence ----
        # Confidence is decomposed: skew_confidence + tension_confidence
        # Overall confidence = max(skew_conf, tension_conf) but each has a floor
        if directional_skew in ("LEFT", "RIGHT"):
            skew_conf = abs(slope_ratio)
        else:
            skew_conf = 0.0

        if tension == "HIGH":
            tension_conf = min(atm_iv_change / (self.TENSION_HIGH * 2), 1.0)
        elif tension == "MEDIUM":
            tension_conf = min(atm_iv_change / self.TENSION_LOW, 0.6) if atm_iv_change > 0 else 0.3
        else:
            tension_conf = 0.0

        # Overall = max of the two, but if both are low → low overall
        confidence = max(skew_conf, tension_conf)
        if confidence < self.MIN_CONFIDENCE:
            confidence = 0.0
            # Force to unknown/defaults
            directional_skew = "SYMMETRIC"
            tension = "LOW"
        else:
            confidence = min(confidence, self.MAX_CONFIDENCE)

        # ---- Legacy shape mapping ----
        if directional_skew == "LEFT":
            shape = "LEFT_SKEW"
        elif directional_skew == "RIGHT":
            shape = "RIGHT_SKEW"
        elif tension == "HIGH":
            shape = "PARALLEL"
        else:
            shape = "NEUTRAL"

        # Legacy vol_regime (coarse)
        if tension == "HIGH":
            vol_regime = "EXPANDING"
        elif tension == "MEDIUM" and directional_skew != "SYMMETRIC":
            vol_regime = "EXPANDING"
        else:
            vol_regime = "COMPRESSING"

        # Store for next delta
        self._prev = {
            "slope_ratio": slope_ratio,
            "atm_iv": atm_iv,
        }

        ctx = VolatilityContext(
            directional_skew=directional_skew,
            tension=tension,
            slope_ratio=round(slope_ratio, 6),
            atm_iv_change=round(atm_iv_change, 6),
            delta_slope_ratio=round(delta_slope_ratio, 6),
            confidence=round(confidence, 4),
            timestamp=ts,
            underlying_price=underlying_price,
            atm_iv=round(atm_iv, 6),
            otm_put_iv=round(otm_put_iv, 6),
            otm_call_iv=round(otm_call_iv, 6),
            shape=shape,
            vol_regime=vol_regime,
        )

        return ctx

    def reset(self) -> None:
        """Clear velocity tracking history."""
        self._prev = None

# ---------------------------------------------------------------------------
# Backward compat alias
# ---------------------------------------------------------------------------

SkewRegime = VolatilityContext
