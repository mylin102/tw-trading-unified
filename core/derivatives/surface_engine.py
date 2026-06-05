"""
OptionSurfaceEngine — real-time option surface builder and skew calculator.

Designed as P1.5 layer between raw tick stream (P1) and strategy layer (P3).

Input:  OptionQuoteEvent (from bidask callback)
Output: SkewSignal (via compute_if_ready), SurfaceSnapshot (via surface_snapshot)

Architecture
------------
- quote_store holds the latest bid/ask/mid for each (option_type, strike) pair
- compute_if_ready is called on futures bar / tick, checks if enough strikes
  are available, then calculates skew
- surface_snapshot computes implied volatility for all stored quotes, enabling
  IV curve shape classification (see shape_classifier.py)
- No knowledge of Shioaji contracts — pure model-based
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

from core.derivatives.iv_calculator import iv_from_price
from core.derivatives.models import OptionQuoteEvent, SkewSignal, SurfaceSnapshot

logger = logging.getLogger(__name__)


class OptionSurfaceEngine:
    """Real-time option surface engine.

    Usage:
        engine = OptionSurfaceEngine(otm_points=300)
        engine.on_quote(event)          # on every bidask callback
        signal = engine.compute_if_ready(futures_price=34000, timestamp=now)
        snapshot = engine.surface_snapshot(futures_price=34000, timestamp=now)
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
        # 💡 GSD: Discard contaminated prices (e.g. MTX price leakage)
        # 2026-06-04 Gemini CLI: Added contamination check and silenced logs
        if event.mid >= 10000:
            return

        key = (event.option_type.upper(), event.strike)
        self.quote_store[key] = {
            "mid": event.mid,
            "bid": event.bid,
            "ask": event.ask,
            "timestamp": event.timestamp,
            "expiry": event.expiry,
            "symbol": event.symbol,
        }
        # 💡 GSD: Silenced log to save CPU/IO resources
        # print(
        #     "[SurfaceEngine] quote updated: %s %.0f bid=%.1f ask=%.1f mid=%.2f"
        #     % (event.option_type, event.strike, event.bid, event.ask, event.mid),
        #     flush=True,
        # )

    # ------------------------------------------------------------------
    # Surface Snapshot (IV-based — for shape classification)
    # ------------------------------------------------------------------

    def surface_snapshot(
        self,
        futures_price: float,
        timestamp: Optional[datetime.datetime] = None,
    ) -> SurfaceSnapshot:
        """Compute IV surface snapshot from current quote store.

        Returns a SurfaceSnapshot with ATM IV, OTM put IV, and OTM call IV.
        When insufficient data is available, returns a snapshot with is_valid()=False.
        The invalid_reason field explains why.
        """
        ts = timestamp or datetime.datetime.utcnow()

        # Find ATM (nearest to futures, not exact match)
        all_calls = [(strike, rec) for (otype, strike), rec in self.quote_store.items() if otype == "CALL"]
        all_puts = [(strike, rec) for (otype, strike), rec in self.quote_store.items() if otype == "PUT"]

        # ATM: nearest strike overall
        atm_strike = None
        atm_record = None
        atm_is_call = True
        all_strikes = []
        for strike, rec in all_calls:
            all_strikes.append((strike, rec, "CALL"))
        for strike, rec in all_puts:
            all_strikes.append((strike, rec, "PUT"))

        if all_strikes:
            best_strike, best_rec, best_type = min(
                all_strikes, key=lambda x: abs(x[0] - futures_price)
            )
            atm_strike = best_strike
            atm_record = best_rec
            atm_is_call = best_type == "CALL"

        # OTM put: nearest below futures (or nearest overall if none below)
        otm_put_strike = None
        otm_put_record = None
        if all_puts:
            below = [(s, r) for s, r in all_puts if s < futures_price]
            if below:
                otm_put_strike, otm_put_record = max(below, key=lambda x: x[0])
            else:
                otm_put_strike, otm_put_record = min(all_puts, key=lambda x: x[0])

        # OTM call: nearest above futures (or nearest overall if none above)
        otm_call_strike = None
        otm_call_record = None
        if all_calls:
            above = [(s, r) for s, r in all_calls if s > futures_price]
            if above:
                otm_call_strike, otm_call_record = min(above, key=lambda x: x[0])
            else:
                otm_call_strike, otm_call_record = max(all_calls, key=lambda x: x[0])

        # Compute DTE from the first available expiry
        dte = 0.0
        for record in [atm_record, otm_put_record, otm_call_record]:
            if record and record.get("expiry"):
                try:
                    expiry = record["expiry"]
                    # expiry is a delivery date string like "202606" or "2026-06-17"
                    if "20" in expiry and len(expiry) >= 6:
                        if "-" in expiry:
                            expiry_dt = datetime.datetime.strptime(expiry[:10], "%Y-%m-%d")
                        elif len(expiry) == 6:
                            # YYYYMM format → use third Wednesday heuristic or last day
                            year = int(expiry[:4])
                            month = int(expiry[4:6])
                            # Approximate: use 15th of month for DTE
                            expiry_dt = datetime.datetime(year, month, min(15, 28))
                        else:
                            continue
                        dte = max((expiry_dt - ts).total_seconds() / 86400.0, 0.1)
                        break
                except (ValueError, OSError):
                    continue

        # Compute IV for each
        atm_iv = 0.0
        if atm_record is not None:
            iv = iv_from_price(
                option_type="CALL" if atm_is_call else "PUT",
                strike=float(atm_strike),
                premium=float(atm_record["mid"]),
                underlying_price=futures_price,
                dte=dte,
            )
            if iv is not None:
                atm_iv = iv

        otm_put_iv = 0.0
        if otm_put_record is not None:
            iv = iv_from_price(
                option_type="PUT",
                strike=float(otm_put_strike),
                premium=float(otm_put_record["mid"]),
                underlying_price=futures_price,
                dte=dte,
            )
            if iv is not None:
                otm_put_iv = iv

        otm_call_iv = 0.0
        if otm_call_record is not None:
            iv = iv_from_price(
                option_type="CALL",
                strike=float(otm_call_strike),
                premium=float(otm_call_record["mid"]),
                underlying_price=futures_price,
                dte=dte,
            )
            if iv is not None:
                otm_call_iv = iv

        # Determine invalid_reason
        invalid_reason = ""
        if atm_record is None:
            invalid_reason = "NO_ATM_QUOTE"
        elif otm_put_record is None:
            invalid_reason = "NO_OTM_PUT"
        elif otm_call_record is None:
            invalid_reason = "NO_OTM_CALL"
        elif atm_iv <= 0:
            invalid_reason = "INVALID_ATM_IV"
        elif otm_put_iv <= 0:
            invalid_reason = "INVALID_OTM_PUT_IV"
        elif otm_call_iv <= 0:
            invalid_reason = "INVALID_OTM_CALL_IV"

        snapshot = SurfaceSnapshot(
            atm_iv=round(atm_iv, 6),
            otm_put_iv=round(otm_put_iv, 6),
            otm_call_iv=round(otm_call_iv, 6),
            atm_strike=float(atm_strike or 0),
            otm_put_strike=float(otm_put_strike or 0),
            otm_call_strike=float(otm_call_strike or 0),
            underlying_price=futures_price,
            dte=round(dte, 2),
            timestamp=ts,
            invalid_reason=invalid_reason,
        )

        # 💡 GSD: Silenced verbose logs to save resources
        # 2026-06-04 Gemini CLI: Silenced logs
        # print(
        #     "[SurfaceEngine] snapshot: atm_iv=%.4f otm_put_iv=%.4f otm_call_iv=%.4f "
        #     "dte=%.1f valid=%s reason=%s "
        #     "atm_strike=%.0f otm_put_strike=%.0f otm_call_strike=%.0f "
        #     "underlying=%.0f"
        #     % (snapshot.atm_iv, snapshot.otm_put_iv, snapshot.otm_call_iv,
        #        snapshot.dte, snapshot.is_valid(), snapshot.invalid_reason,
        #        snapshot.atm_strike, snapshot.otm_put_strike, snapshot.otm_call_strike,
        #        snapshot.underlying_price),
        #     flush=True,
        # )

        return snapshot

    # ------------------------------------------------------------------
    # Skew Computation (legacy premium-based)
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
                # 💡 GSD: Silenced verbose logs to save resources
                # 2026-06-04 Gemini CLI: Silenced logs
                # print(
                #     "[SurfaceEngine] asymmetric strikes put=%.0f call=%.0f "
                #     "gap=%.0f pts > 1000 → NEUTRAL fallthrough"
                #     % (put_strike, call_strike, strike_gap),
                #     flush=True,
                # )
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

        # 💡 GSD: Silenced verbose logs to save resources
        # 2026-06-04 Gemini CLI: Silenced logs
        # print(
        #     "[SurfaceEngine] skew_signal direction=%s confidence=%.4f "
        #     "skew_level=%.2f put=%.2f call=%.2f divergence=%.2f"
        #     % (signal.direction, signal.confidence,
        #        signal.skew_level, signal.downside_risk, signal.upside_risk,
        #        signal.put_call_divergence),
        #     flush=True,
        # )

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
