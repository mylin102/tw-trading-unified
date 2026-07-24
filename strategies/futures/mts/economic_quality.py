# 2026-07-24 Gemini CLI: Level 3 Economic Quality & Distribution Stability Metrics Engine
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence


@dataclass(frozen=True)
class EconomicStabilitySummary:
    """Statistical distribution metrics measuring economic stability across a dataset.
    
    Fields:
    - mean: Average metric value.
    - median: 50th percentile.
    - std: Standard deviation (volatility of performance).
    - p10: 10th percentile (lower tail).
    - p90: 90th percentile (upper tail).
    - iqr: Interquartile range (p75 - p25).
    """
    mean: float
    median: float
    std: float
    p10: float
    p90: float
    iqr: float

    @classmethod
    def compute(cls, values: Sequence[float]) -> "EconomicStabilitySummary":
        """Compute distribution summary for a sequence of floating point metric values."""
        if not values:
            return cls(mean=0.0, median=0.0, std=0.0, p10=0.0, p90=0.0, iqr=0.0)

        sorted_vals = sorted(values)
        n = len(sorted_vals)

        mean_val = sum(sorted_vals) / n

        # Median (50th percentile)
        if n % 2 == 1:
            median_val = sorted_vals[n // 2]
        else:
            median_val = (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0

        # Sample Standard Deviation
        if n > 1:
            variance = sum((x - mean_val) ** 2 for x in sorted_vals) / (n - 1)
            std_val = math.sqrt(variance)
        else:
            std_val = 0.0

        p10_val = quantile(sorted_vals, 0.10)
        p90_val = quantile(sorted_vals, 0.90)
        p25_val = quantile(sorted_vals, 0.25)
        p75_val = quantile(sorted_vals, 0.75)
        iqr_val = p75_val - p25_val

        return cls(
            mean=mean_val,
            median=median_val,
            std=std_val,
            p10=p10_val,
            p90=p90_val,
            iqr=iqr_val,
        )


@dataclass(frozen=True)
class TradeEconomicQuality:
    """Pure dataclass capturing Level 3 Economic Quality metrics for a completed trade."""
    trade_id: str
    ticker: str
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    mfe: Decimal
    mae: Decimal
    ped: Decimal
    capture_ratio: float
    release_efficiency: float
    exit_efficiency: float
    holding_duration_seconds: float

    @classmethod
    def calculate(
        cls,
        trade_id: str,
        ticker: str,
        entry_price: Decimal,
        exit_price: Decimal,
        net_pnl: Decimal,
        peak_favorable_price: Decimal,
        peak_adverse_price: Decimal,
        release_point_price: Decimal,
        duration_seconds: float,
        is_long: bool = True,
    ) -> "TradeEconomicQuality":
        """Compute pure Economic Quality metrics from trade prices and peak excursions."""
        multiplier = Decimal("1") if is_long else Decimal("-1")
        gross_pnl = (exit_price - entry_price) * multiplier

        if is_long:
            mfe = max(Decimal("0"), peak_favorable_price - entry_price)
            mae = max(Decimal("0"), entry_price - peak_adverse_price)
            release_mfe = max(Decimal("0"), release_point_price - entry_price)
        else:
            mfe = max(Decimal("0"), entry_price - peak_favorable_price)
            mae = max(Decimal("0"), peak_adverse_price - entry_price)
            release_mfe = max(Decimal("0"), entry_price - release_point_price)

        ped = max(Decimal("0"), mfe - net_pnl)
        capture_ratio = float(net_pnl / mfe) if mfe > 0 else (1.0 if net_pnl >= 0 else 0.0)
        release_eff = float(release_mfe / mfe) if mfe > 0 else 1.0
        exit_eff = float(net_pnl / mfe) if mfe > 0 else 1.0

        return cls(
            trade_id=trade_id,
            ticker=ticker,
            entry_price=entry_price,
            exit_price=exit_price,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            mfe=mfe,
            mae=mae,
            ped=ped,
            capture_ratio=capture_ratio,
            release_efficiency=release_eff,
            exit_efficiency=exit_eff,
            holding_duration_seconds=duration_seconds,
        )


def quantile(sorted_data: Sequence[float], q: float) -> float:
    """Helper to compute quantile value using linear interpolation."""
    n = len(sorted_data)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_data[0]

    pos = q * (n - 1)
    idx = int(pos)
    frac = pos - idx

    if idx >= n - 1:
        return sorted_data[-1]

    return sorted_data[idx] + frac * (sorted_data[idx + 1] - sorted_data[idx])
