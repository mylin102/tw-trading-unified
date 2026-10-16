"""
mts_vol_adjuster.py — Volatility-Adjusted Volume 成交量異常度評估

識別每根 5 分鐘 K 線的成交量異常度 (Z-score)，區分：
- 正常成交 (z ≤ 1.0) → 信號可靠
- 高異常成交 (1.0 < z ≤ 2.0) → 可疑
- 極異常成交 (z > 2.0) → 警告信號
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


# Z-score 校準參數（根據 PoC 驗證）
ZSCORE_PARAMS = {
    "morning": {"mean": 467, "std": 121},      # 09:15-11:30
    "afternoon": {"mean": 396, "std": 96},     # 13:30-15:30
    "night": {"mean": 210, "std": 60},         # 18:00-05:00
}

TIME_SEGMENTS = {
    "morning": [(9, 15), (11, 30)],
    "afternoon": [(13, 30), (15, 30)],
    "night": [(18, 0), (5, 0)],
}


@dataclass
class VolAdjustment:
    """成交量異常度評估結果"""
    ts: float  # epoch seconds (bar close time)
    raw_volume: int  # 原始成交量
    z_score: float  # Z-score
    anomaly_level: str  # "NORMAL" | "HIGH" | "EXTREME" | "UNKNOWN"
    quality_rating: float  # [0, 100]
    segment: Optional[str] = None  # "morning" | "afternoon" | "night"

    def to_dict(self) -> dict:
        """轉換為字典"""
        d = asdict(self)
        d.update({
            "z_score": round(self.z_score, 2),
            "quality_rating": round(self.quality_rating, 1),
        })
        return d


class VolAdjuster:
    """成交量異常度計算"""

    @staticmethod
    def get_time_segment(ts: float) -> Optional[str]:
        """
        根據時間戳確定時段

        Args:
            ts: epoch seconds

        Returns:
            "morning" | "afternoon" | "night" | None
        """
        dt = datetime.fromtimestamp(ts)
        hour, minute = dt.hour, dt.minute

        # Morning: 09:15-11:30
        if (hour == 9 and minute >= 15) or (9 < hour < 11) or (hour == 11 and minute < 30):
            return "morning"

        # Afternoon: 13:30-15:30
        elif (hour == 13 and minute >= 30) or (13 < hour < 15) or (hour == 15 and minute <= 30):
            return "afternoon"

        # Night: 18:00-05:00 (next day)
        elif hour >= 18 or hour < 5:
            return "night"

        return None

    @staticmethod
    def compute_zscore(volume: int, segment: str) -> Optional[float]:
        """
        計算 Z-score

        Args:
            volume: 成交量
            segment: 時段 ("morning" | "afternoon" | "night")

        Returns:
            Z-score 或 None (無效時段)
        """
        if segment not in ZSCORE_PARAMS:
            return None

        params = ZSCORE_PARAMS[segment]
        if params["std"] == 0:
            return 0.0

        z = (volume - params["mean"]) / params["std"]
        return z

    @staticmethod
    def classify_anomaly(z_score: float) -> str:
        """
        異常分類

        Args:
            z_score: Z-score 值

        Returns:
            "NORMAL" | "HIGH" | "EXTREME"
        """
        if z_score > 2.0:
            return "EXTREME"
        elif z_score > 1.0:
            return "HIGH"
        else:
            return "NORMAL"

    @staticmethod
    def compute_quality(z_score: float) -> float:
        """
        成交品質評分 (0-100)

        公式: quality = 100 * exp(-|z_score|^2 / 2)

        Args:
            z_score: Z-score 值

        Returns:
            品質評分 [0, 100]
        """
        try:
            return 100 * math.exp(-z_score**2 / 2)
        except (ValueError, OverflowError):
            return 0.0

    @staticmethod
    def adjust(ts: float, volume: int) -> VolAdjustment:
        """
        主計算函數：計算成交量異常度

        Args:
            ts: epoch seconds (bar close time)
            volume: 5m K 線成交量

        Returns:
            VolAdjustment 物件
        """
        segment = VolAdjuster.get_time_segment(ts)

        if segment is None:
            # 非交易時段
            return VolAdjustment(
                ts=ts,
                raw_volume=volume,
                z_score=0.0,
                anomaly_level="UNKNOWN",
                quality_rating=0.0,
                segment=None
            )

        z_score = VolAdjuster.compute_zscore(volume, segment)
        if z_score is None:
            return VolAdjustment(
                ts=ts,
                raw_volume=volume,
                z_score=0.0,
                anomaly_level="UNKNOWN",
                quality_rating=0.0,
                segment=segment
            )

        anomaly = VolAdjuster.classify_anomaly(z_score)
        quality = VolAdjuster.compute_quality(z_score)

        return VolAdjustment(
            ts=ts,
            raw_volume=volume,
            z_score=z_score,
            anomaly_level=anomaly,
            quality_rating=quality,
            segment=segment
        )
