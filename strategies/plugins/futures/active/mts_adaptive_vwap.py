"""2026-10-15 Hermes Agent: pure, completed-bar Adaptive VWAP-TWAP fusion."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


TW = timezone(timedelta(hours=8))


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def parse_ts(value: Any) -> Optional[float]:
    """Epoch s/ms or ISO/datetime; naive wall times mean Asia/Taipei."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            # 2026-10-16 Hermes Agent: candidate telemetry stringifies numeric epochs.
            ts = _number(value)
            return ts / 1000 if ts is not None and ts > 1e12 else ts
    else:
        ts = _number(value)
        return ts / 1000 if ts is not None and ts > 1e12 else ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TW)
    try:
        return dt.timestamp()
    except (OverflowError, ValueError, OSError):
        return None


@dataclass(frozen=True)
class AdaptiveVWAPConfig:
    window_secs: float = 300.0
    bar_secs: float = 300.0
    intensity_full_scale: float = 10.0
    weights: tuple[float, float, float, float] = (0.25, 0.25, 0.30, 0.20)
    alpha_midpoint: float = 50.0
    alpha_scale: float = 15.0
    shadow_enabled: bool = True
    # 2026-10-16 Hermes Agent: supplement defines one week as five trading dates.
    minimum_trading_days: float = 5.0
    minimum_win_rate_gain_pp: float = 2.0
    minimum_mdd_reduction_pct: float = 5.0

    def __post_init__(self) -> None:
        for name in ("window_secs", "bar_secs", "intensity_full_scale", "alpha_scale", "minimum_trading_days"):
            value = _number(getattr(self, name))
            if value is None or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        for name in ("alpha_midpoint", "minimum_win_rate_gain_pp", "minimum_mdd_reduction_pct"):
            value = _number(getattr(self, name))
            if value is None or not 0 <= value <= 100:
                raise ValueError(f"{name} must be in [0, 100]")
            object.__setattr__(self, name, value)
        if not isinstance(self.weights, (tuple, list)) or len(self.weights) != 4:
            raise ValueError("weights must contain four non-negative finite numbers")
        weights = tuple(_number(w) for w in self.weights)
        if any(w is None or w < 0 for w in weights):
            raise ValueError("weights must contain four non-negative finite numbers")
        if not math.isclose(sum(weights), 1.0, abs_tol=1e-9):
            raise ValueError("weights must sum to 1")
        object.__setattr__(self, "weights", weights)
        if not isinstance(self.shadow_enabled, bool):
            raise ValueError("shadow_enabled must be a boolean")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> AdaptiveVWAPConfig:
        if not isinstance(values, Mapping):
            raise ValueError("adaptive configuration must be a mapping")
        args = dict(values)
        try:
            return cls(**args)
        except TypeError as exc:
            raise ValueError(f"invalid adaptive configuration: {exc}") from exc

    @classmethod
    def load(cls, path: str | Path) -> AdaptiveVWAPConfig:
        import yaml
        with Path(path).open() as stream:
            values = yaml.safe_load(stream)
        return cls.from_mapping(values)


@dataclass(frozen=True)
class AdaptiveVWAPSnapshot:
    ts: float
    chips_strength: float
    alpha: float
    vwap: float
    twap: float
    adaptive_price: float
    decision_confidence: float

    def to_dict(self) -> dict[str, float]:
        result = asdict(self)
        for name, places in (("chips_strength", 1), ("alpha", 3), ("vwap", 2),
                             ("twap", 2), ("adaptive_price", 2), ("decision_confidence", 1)):
            result[name] = round(result[name], places)
        return result


class AdaptiveVWAP:
    """No IO, orders, wall-clock decisions, or mutation of caller inputs."""

    @staticmethod
    def _chips_strength(oi_int, oi_dir, vol_qual, vol_z, config=None) -> float:
        cfg = config or AdaptiveVWAPConfig()
        intensity, direction, quality, zscore = map(_number, (oi_int, oi_dir, vol_qual, vol_z))
        if (intensity is None or intensity < 0 or direction is None or not 0 <= direction <= 1
                or quality is None or not 0 <= quality <= 100 or zscore is None):
            raise ValueError("invalid chips inputs")
        intensity_score = min(100.0, intensity / cfg.intensity_full_scale * 100)
        # Specification prose: balanced direction is weak, extremes are strong.
        direction_score = 2 * abs(direction - 0.5) * 100
        if zscore > 2:
            anomaly_score = min(100.0, (zscore - 2) * 20 + 80)
        elif zscore < -1:
            anomaly_score = max(0.0, (zscore + 1) * 33)
        else:
            anomaly_score = 50 + zscore / 3 * 50
        return min(100.0, max(0.0, sum(w * s for w, s in zip(
            cfg.weights, (intensity_score, direction_score, quality, anomaly_score)))))

    @staticmethod
    def _compute_alpha(chips_strength, config=None) -> float:
        cfg = config or AdaptiveVWAPConfig()
        chips = _number(chips_strength)
        if chips is None or not 0 <= chips <= 100:
            raise ValueError("chips_strength must be in [0, 100]")
        x = (chips - cfg.alpha_midpoint) / cfg.alpha_scale
        if x >= 0:
            return 1 / (1 + math.exp(-x))
        exp_x = math.exp(x)
        return exp_x / (1 + exp_x)

    @staticmethod
    def _twap_stats(bars, decision_ts, window_secs, bar_secs=300.0):
        end = parse_ts(decision_ts)
        window, period = _number(window_secs), _number(bar_secs)
        if end is None or window is None or window <= 0 or period is None or period <= 0:
            raise ValueError("invalid TWAP time/window")
        if bars is None:
            return None, 0.0
        start = end - window
        samples = {}
        for bar in bars:
            if not isinstance(bar, Mapping):
                continue
            close_ts, price = parse_ts(bar.get("ts")), _number(bar.get("close"))
            if close_ts is None or not start < close_ts <= end or price is None or price <= 0:
                continue
            if "start_ts" in bar:
                open_ts = parse_ts(bar["start_ts"])
                duration = close_ts - open_ts if open_ts is not None else None
            else:
                duration = _number(bar.get("duration_secs", period))
            if duration is None or duration <= 0:
                continue
            # Last valid duplicate wins; sort without mutating caller bars.
            samples[close_ts] = (price, duration)
        price_time, covered = 0.0, 0.0
        covered_end = start
        for close_ts, (price, duration) in sorted(samples.items()):
            open_ts = max(start, close_ts - duration, covered_end)
            seconds = close_ts - open_ts
            if seconds > 0:
                price_time += price * seconds
                covered += seconds
                covered_end = close_ts
        return (price_time / covered, covered) if covered > 0 else (None, 0.0)

    @staticmethod
    def _compute_twap(bars, decision_ts, window_secs=300, bar_secs=300):
        return AdaptiveVWAP._twap_stats(bars, decision_ts, window_secs, bar_secs)[0]

    @staticmethod
    def compute_adaptive_price(vwap, twap, alpha) -> Optional[float]:
        v, t, a = map(_number, (vwap, twap, alpha))
        if v is None or v <= 0 or t is None or t <= 0 or a is None or not 0 <= a <= 1:
            return None
        return a * v + (1 - a) * t

    @staticmethod
    def compute(ts, oi_intensity, oi_direction, vol_quality, vol_zscore, vwap,
                bars: Sequence[Mapping[str, Any]], window_secs=None,
                config: Optional[AdaptiveVWAPConfig] = None) -> Optional[AdaptiveVWAPSnapshot]:
        cfg = config or AdaptiveVWAPConfig()
        end, volume_price = parse_ts(ts), _number(vwap)
        if end is None or volume_price is None or volume_price <= 0:
            return None
        window = cfg.window_secs if window_secs is None else window_secs
        try:
            chips = AdaptiveVWAP._chips_strength(oi_intensity, oi_direction, vol_quality, vol_zscore, cfg)
            alpha = AdaptiveVWAP._compute_alpha(chips, cfg)
            twap, covered = AdaptiveVWAP._twap_stats(bars, end, window, cfg.bar_secs)
        except ValueError:
            return None
        price = AdaptiveVWAP.compute_adaptive_price(volume_price, twap, alpha)
        if price is None:
            return None
        # Conservative quality/coverage proxy, not a calibrated probability.
        confidence = float(vol_quality) * min(1.0, covered / float(window))
        return AdaptiveVWAPSnapshot(end, chips, alpha, volume_price, twap, price, confidence)
