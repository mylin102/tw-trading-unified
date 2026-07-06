"""
IV Percentile / Z-score Engine — in-memory rolling volatility percentile.

PURPOSE
-------
Absolute IV has no universal meaning (TSLA 25% = calm, SPX 25% = panic,
TXO 25% = depends on regime). This engine tracks the rolling distribution
of ATM IV so decisions are based on relative positioning, not absolute level.

OUTPUT
------
{
    "atm_iv": 0.23,          # current ATM IV
    "iv_percentile": 0.78,   # [0, 1] — rank within window
    "iv_zscore": 1.35,       # standard deviations from mean
    "sample_count": 240,     # how many samples in window
    "window_sec": 7200,      # configured window size
    "ready": True,            # False until min_samples reached
}

DESIGN
------
- In-memory deque-based rolling window. No DB.
- Non-positive / None / stale samples rejected.
- When std is near zero, zscore returns 0.
- Thread-safe via lock (called from monitor main loop and surface engine).
"""

from __future__ import annotations

import datetime
import statistics
from collections import deque
from threading import Lock
from typing import Optional


class IVPercentileEngine:
    """Rolling IV percentile and z-score tracker.

    Usage:
        engine = IVPercentileEngine(window_sec=7200, min_samples=60)
        engine.record(atm_iv=0.23, timestamp=now)
        result = engine.get_percentile(atm_iv=0.23)
        # → {"iv_percentile": 0.78, "iv_zscore": 1.35, "ready": True, ...}
    """

    def __init__(
        self,
        window_sec: int = 7200,      # 2-hour rolling window by default
        min_samples: int = 30,       # minimum samples before ready=True
        max_age_sec: int = 300,      # samples older than this are stale (5 min)
    ):
        self.window_sec = window_sec
        self.min_samples = min_samples
        self.max_age_sec = max_age_sec

        # (timestamp, atm_iv) pairs, time-ordered
        self._samples: deque[tuple[datetime.datetime, float]] = deque()
        self._lock = Lock()

    # ------------------------------------------------------------------
    # Record
    # ------------------------------------------------------------------

    def record(self, atm_iv: float, timestamp: Optional[datetime.datetime] = None) -> None:
        """Record an ATM IV observation.

        Rejects:
        - Non-positive IV values
        - None timestamp (uses utcnow as fallback)
        """
        if atm_iv is None or atm_iv <= 0:
            return

        ts = timestamp or datetime.datetime.utcnow()

        with self._lock:
            self._samples.append((ts, atm_iv))
            # Prune against whatever is most recent (could be the one we just added)
            latest_ts = self._samples[-1][0]
            self._prune(latest_ts)

    def record_batch(self, values: list[tuple[datetime.datetime, float]]) -> None:
        """Record multiple IV observations at once (e.g., on startup)."""
        with self._lock:
            for ts, iv in values:
                if iv is not None and iv > 0:
                    self._samples.append((ts, iv))
            latest_ts = max((ts for ts, _ in self._samples), default=datetime.datetime.utcnow())
            self._prune(latest_ts)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_percentile(
        self,
        atm_iv: float,
        timestamp: Optional[datetime.datetime] = None,
    ) -> dict:
        """Get percentile + zscore for the given IV value.

        Returns a dict with keys:
            atm_iv: current input value
            iv_percentile: [0, 1] rank within window
            iv_zscore: std devs from mean (0 if std too small)
            sample_count: number of samples in window
            window_sec: configured window
            ready: True if sample_count >= min_samples
        """
        ts = timestamp or datetime.datetime.utcnow()

        with self._lock:
            self._prune(ts)
            samples = [iv for _, iv in self._samples]

        count = len(samples)
        ready = count >= self.min_samples

        iv_pct = 0.0
        iv_zscore = 0.0

        if ready and count > 0:
            # Percentile: rank(atm_iv) / count
            # Uses < to match standard percentile semantics
            rank = sum(1 for x in samples if x < atm_iv)
            iv_pct = rank / count

            # Z-score
            mean = statistics.mean(samples)
            if count >= 2:
                std = statistics.stdev(samples)
                # Guard against degenerate std (all values identical)
                if std > 1e-8:
                    iv_zscore = (atm_iv - mean) / std
                # else iv_zscore stays 0

        return {
            "atm_iv": round(atm_iv, 6),
            "iv_percentile": round(iv_pct, 4),
            "iv_zscore": round(iv_zscore, 4),
            "sample_count": count,
            "window_sec": self.window_sec,
            "ready": ready,
        }

    def get_stats(self) -> dict:
        """Get summary stats of the current window (no IV input needed)."""
        with self._lock:
            samples = list(self._samples)

        if len(samples) < 2:
            return {"ready": False, "sample_count": len(samples)}

        values = [iv for _, iv in samples]
        mean = statistics.mean(values)
        std = statistics.stdev(values)

        return {
            "ready": len(samples) >= self.min_samples,
            "sample_count": len(samples),
            "mean_iv": round(mean, 6),
            "std_iv": round(std, 6),
            "min_iv": round(min(values), 6),
            "max_iv": round(max(values), 6),
            "current_iv": round(values[-1], 6),
            "window_sec": self.window_sec,
        }

    def reset(self) -> None:
        """Clear all samples."""
        with self._lock:
            self._samples.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _prune(self, now: datetime.datetime) -> None:
        """Remove samples outside the rolling window.

        Handles out-of-order timestamps by filtering all stale samples.
        """
        cutoff = now - datetime.timedelta(seconds=self.window_sec)
        # Remove front-stale samples (common case: time-ordered)
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        # Clean remaining stale from non-front positions (rare: out-of-order)
        kept = deque()
        while self._samples:
            ts, iv = self._samples.popleft()
            if ts >= cutoff:
                kept.append((ts, iv))
        self._samples = kept

    @property
    def sample_count(self) -> int:
        with self._lock:
            return len(self._samples)
