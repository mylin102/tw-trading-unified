"""
OptionSurfaceEngine — real-time option surface builder and skew calculator.

Designed as P1.5 layer between raw tick stream (P1) and strategy layer (P3).

Input:  OptionQuoteEvent (from bidask callback)
Output: SkewSignal (via compute_if_ready)

Architecture
------------
- quote_store holds the latest bid/ask/mid for each (option_type, strike) pair
- compute_if_ready is called on futures bar / tick, checks if enough strikes
  are available, then calculates skew
- No knowledge of Shioaji contracts — pure model-based
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from core.derivatives.models import OptionQuoteEvent, SkewSignal

logger = logging.getLogger(__name__)


class OptionSurfaceEngine:
    """Real-time option surface engine.

    Usage:
        engine = OptionSurfaceEngine(otm_points=300)
        engine.on_quote(event)          # on every bidask callback
        signal = engine.compute_if_ready(futures_price=34000, timestamp=now)
        shared_state["skew_signal"] = signal.to_dict()
    """

    def __init__(
        self,
        otm_points: int = 300,
        neutral_threshold: float = 0.15,
        vol_expand_threshold: float = 0.25,
    ):
        logger.setLevel(logging.INFO)

        # quote_store[(option_type, strike)] = {mid, bid, ask, timestamp, expiry}
        self.quote_store: dict[tuple[str, float], dict] = {}

        # Previous snapshot for delta calculation
        self._prev_signal: Optional[dict] = None

        # Config
        self.otm_points = otm_points
        self.neutral_threshold = neutral_threshold
        self.vol_expand_threshold = vol_expand_threshold

        # Track last compute time to avoid recomputing every tick
        self._last_compute_ts: Optional[datetime.datetime] = None
        self._cooldown_seconds: float = 5.0

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def on_quote(self, event: OptionQuoteEvent) -> None:
        """Feed a quote event into the surface engine.

        This is called on every bidask callback for any option contract.
        quote_store is updated by (option_type, strike) — same strike
        overwrites previous quote automatically.
        """
        key = (event.option_type.upper(), event.strike)
        self.quote_store[key] = {
            "mid": event.mid,
            "bid": event.bid,
            "ask": event.ask,
            "timestamp": event.timestamp,
            "expiry": event.expiry,
            "symbol": event.symbol,
        }
        print(
            "[SurfaceEngine] quote updated: %s %.0f bid=%.1f ask=%.1f mid=%.2f"
            % (event.option_type, event.strike, event.bid, event.ask, event.mid),
            flush=True,
        )

    # ------------------------------------------------------------------
    # Computation
    # ------------------------------------------------------------------

    def compute_if_ready(
        self,
        futures_price: float,
        timestamp: Optional[datetime.datetime] = None,
        force: bool = False,
    ) -> SkewSignal:
        """Compute skew signal if enough quotes are available.

        Args:
            futures_price: Current underlying futures price.
            timestamp: Current timestamp (defaults to UTC now).
            force: Bypass cooldown for forced recomputation.

        Returns:
            SkewSignal — when data is insufficient, returns direction="UNKNOWN".
        """
        # Cooldown check
        if not force and self._last_compute_ts is not None:
            now = timestamp or datetime.datetime.utcnow()
            elapsed = (now - self._last_compute_ts).total_seconds()
            if elapsed < self._cooldown_seconds:
                return self._build_unknown(timestamp)

        # Find OTM put and call nearest to futures ± otm_points
        put_strike, put_record = self._nearest_quote("PUT", futures_price - self.otm_points)
        call_strike, call_record = self._nearest_quote("CALL", futures_price + self.otm_points)

        # [Skew Integration / Phase 1] When quote store has strikes but they are
        # far from the futures anchor (e.g. ATM strike resolution mismatch), use
        # the actual ATM (nearest to futures) as a fallback so we get a valid
        # signal instead of _unknown.  An asymmetric pair (call put strikes far
        # apart) will produce a NEUTRAL signal downstream so it won't trigger
        # false filters.
        if put_record is None or call_record is None:
            fallback_put_strike, fallback_put_record = self._nearest_quote("PUT", futures_price)
            fallback_call_strike, fallback_call_record = self._nearest_quote("CALL", futures_price)
            if fallback_put_record is not None and fallback_call_record is not None:
                put_strike, put_record = fallback_put_strike, fallback_put_record
                call_strike, call_record = fallback_call_strike, fallback_call_record

        if put_record is None or call_record is None:
            return self._build_unknown(timestamp)

        # Phase 1 guard: if call/put strikes are asymmetric (miles apart) we
        # can't compute a valid directional skew.  Return NEUTRAL so the rest
        # of the pipeline sees a valid signal but confidence=0.
        if put_strike is not None and call_strike is not None:
            strike_gap = abs(put_strike - call_strike)
            if strike_gap > 1000:
                print(
                    "[SurfaceEngine] asymmetric strikes put=%.0f call=%.0f "
                    "gap=%.0f pts > 1000 → NEUTRAL fallthrough"
                    % (put_strike, call_strike, strike_gap),
                    flush=True,
                )
                return SkewSignal(
                    direction="NEUTRAL",
                    confidence=0.0,
                    timestamp=timestamp or datetime.datetime.utcnow(),
                    underlying_price=futures_price,
                )

        put_price = put_record["mid"]
        call_price = call_record["mid"]
        ts = timestamp or put_record.get("timestamp") or datetime.datetime.utcnow()

        # Compute signal
        skew_level = float(put_price - call_price)
        underlying_price = futures_price

        # Build result dict for delta calculation
        current = {
            "put_price": put_price,
            "call_price": call_price,
            "skew_level": skew_level,
        }

        # Delta from previous snapshot
        put_change = 0.0
        call_change = 0.0
        put_call_divergence = 0.0
        skew_change = 0.0

        if self._prev_signal is not None:
            put_change = put_price - self._prev_signal["put_price"]
            call_change = call_price - self._prev_signal["call_price"]
            put_call_divergence = put_change - call_change
            skew_change = skew_level - self._prev_signal["skew_level"]

        self._prev_signal = current

        # Confidence: divergence magnitude relative to price level
        avg_price = (put_price + call_price) / 2.0
        normalized_div = 0.0
        if avg_price > 0.1:
            confidence = min(abs(put_call_divergence) / (avg_price * 2.0), 1.0)
        else:
            confidence = 0.0

        # Direction
        if confidence < self.neutral_threshold:
            direction = "NEUTRAL"
        elif put_call_divergence > 0:
            direction = "DOWN"  # Puts getting more expensive → fear
        else:
            direction = "UP"    # Calls getting more expensive → greed

        # Vol regime: rate of change in skew
        skew_abs_change = abs(skew_change)
        vol_impulse = skew_abs_change / (avg_price + 1e-9)
        if confidence < self.neutral_threshold:
            vol_regime = "NEUTRAL"
        elif vol_impulse >= self.vol_expand_threshold:
            vol_regime = "EXPANDING"
        else:
            vol_regime = "COMPRESSING"

        self._last_compute_ts = ts if isinstance(ts, datetime.datetime) else datetime.datetime.utcnow()

        signal = SkewSignal(
            direction=direction,
            confidence=round(confidence, 4),
            skew_level=round(skew_level, 2),
            skew_change=round(skew_change, 2),
            put_call_divergence=round(put_call_divergence, 2),
            downside_risk=round(put_price, 2),
            upside_risk=round(call_price, 2),
            imbalance=round(put_price - call_price, 2),
            vol_regime=vol_regime,
            timestamp=ts if isinstance(ts, datetime.datetime) else datetime.datetime.utcnow(),
            underlying_price=underlying_price,
        )

        print(
            "[SurfaceEngine] skew_signal direction=%s confidence=%.4f "
            "skew_level=%.2f put=%.2f call=%.2f divergence=%.2f"
            % (signal.direction, signal.confidence,
               signal.skew_level, signal.downside_risk, signal.upside_risk,
               signal.put_call_divergence),
            flush=True,
        )

        return signal

    def reset(self) -> None:
        """Clear quote store and history. Used on session recovery."""
        self.quote_store.clear()
        self._prev_signal = None
        self._last_compute_ts = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _nearest_quote(self, option_type: str, target_strike: float) -> tuple[Optional[float], Optional[dict]]:
        """Find the quote nearest to target_strike for given option_type."""
        candidates = [
            (strike, record)
            for (otype, strike), record in self.quote_store.items()
            if otype == option_type.upper()
        ]
        if not candidates:
            return None, None

        best_strike, best_record = min(
            candidates,
            key=lambda x: abs(x[0] - target_strike),
        )
        return best_strike, best_record

    def _build_unknown(self, timestamp=None) -> SkewSignal:
        """Return a neutral/unknown signal when data is insufficient."""
        ts = timestamp or datetime.datetime.utcnow()
        return SkewSignal(
            direction="UNKNOWN",
            timestamp=ts if isinstance(ts, datetime.datetime) else datetime.datetime.utcnow(),
        )
