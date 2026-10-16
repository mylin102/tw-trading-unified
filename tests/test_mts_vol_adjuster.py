# 2026-10-15 Hermes Agent: integrate supplied tests using package imports; expectations unchanged.
"""
test_mts_vol_adjuster.py — VolAdjuster 完整測試套件

覆蓋：時段判斷、Z-score 計算、異常分類、品質評分、邊界情況
"""

from datetime import datetime
import pytest

from strategies.plugins.futures.active.mts_vol_adjuster import VolAdjuster, VolAdjustment, ZSCORE_PARAMS


class TestVolAdjusterTimeSegment:
    """時段判斷測試"""

    def test_get_time_segment_morning_start(self):
        """09:15 → morning"""
        ts = datetime(2026, 10, 15, 9, 15).timestamp()
        segment = VolAdjuster.get_time_segment(ts)
        assert segment == "morning"

    def test_get_time_segment_morning_end(self):
        """11:29 → morning, 11:30 → NOT morning"""
        ts_morning = datetime(2026, 10, 15, 11, 29).timestamp()
        ts_after = datetime(2026, 10, 15, 11, 30).timestamp()

        assert VolAdjuster.get_time_segment(ts_morning) == "morning"
        assert VolAdjuster.get_time_segment(ts_after) != "morning"

    def test_get_time_segment_afternoon_start(self):
        """13:30 → afternoon"""
        ts = datetime(2026, 10, 15, 13, 30).timestamp()
        segment = VolAdjuster.get_time_segment(ts)
        assert segment == "afternoon"

    def test_get_time_segment_afternoon_end(self):
        """15:30 → afternoon, 15:31 → NOT afternoon"""
        ts_afternoon = datetime(2026, 10, 15, 15, 30).timestamp()
        ts_after = datetime(2026, 10, 15, 15, 31).timestamp()

        assert VolAdjuster.get_time_segment(ts_afternoon) == "afternoon"
        assert VolAdjuster.get_time_segment(ts_after) != "afternoon"

    def test_get_time_segment_night_start(self):
        """18:00 → night"""
        ts = datetime(2026, 10, 15, 18, 0).timestamp()
        segment = VolAdjuster.get_time_segment(ts)
        assert segment == "night"

    def test_get_time_segment_night_end(self):
        """04:59 → night, 05:00 → NOT night"""
        ts_night = datetime(2026, 10, 16, 4, 59).timestamp()
        ts_after = datetime(2026, 10, 16, 5, 0).timestamp()

        assert VolAdjuster.get_time_segment(ts_night) == "night"
        assert VolAdjuster.get_time_segment(ts_after) != "night"

    def test_get_time_segment_non_trading_hours(self):
        """12:00 (午休) → None"""
        ts = datetime(2026, 10, 15, 12, 0).timestamp()
        segment = VolAdjuster.get_time_segment(ts)
        assert segment is None

    def test_get_time_segment_night_wraps_midnight(self):
        """23:30 → night (跨日)"""
        ts = datetime(2026, 10, 15, 23, 30).timestamp()
        segment = VolAdjuster.get_time_segment(ts)
        assert segment == "night"

    # ── Z-score 計算測試 ────────────────────────────────────────

    def test_zscore_normal_morning(self):
        """早盤正常成交量 (467 = μ) → z ≈ 0"""
        z = VolAdjuster.compute_zscore(467, "morning")
        assert abs(z - 0) < 0.1

    def test_zscore_high_morning(self):
        """早盤高異常成交量 (588 = μ + σ) → z ≈ 1.0"""
        z = VolAdjuster.compute_zscore(588, "morning")
        assert 0.9 < z < 1.1

    def test_zscore_extreme_morning(self):
        """早盤極異常成交量 (708 = μ + 2σ) → z ≈ 2.0"""
        z = VolAdjuster.compute_zscore(708, "morning")
        assert 1.9 < z < 2.1

    def test_zscore_afternoon(self):
        """午盤 Z-score 計算"""
        # μ = 396, σ = 96
        z_normal = VolAdjuster.compute_zscore(396, "afternoon")
        z_high = VolAdjuster.compute_zscore(492, "afternoon")  # μ + σ

        assert abs(z_normal - 0) < 0.1
        assert 0.9 < z_high < 1.1

    def test_zscore_night(self):
        """夜盤 Z-score 計算"""
        # μ = 210, σ = 60
        z_normal = VolAdjuster.compute_zscore(210, "night")
        z_high = VolAdjuster.compute_zscore(270, "night")  # μ + σ

        assert abs(z_normal - 0) < 0.1
        assert 0.9 < z_high < 1.1

    def test_zscore_invalid_segment(self):
        """無效時段 → None"""
        z = VolAdjuster.compute_zscore(500, "invalid_segment")
        assert z is None

    def test_zscore_zero_std(self):
        """σ = 0 → z = 0"""
        # 這種情況不應該發生，但要能處理
        z = VolAdjuster.compute_zscore(500, "morning")
        assert isinstance(z, (int, float))

    # ── 異常分類測試 ────────────────────────────────────────────

    def test_classify_normal(self):
        """z ≤ 1.0 → NORMAL"""
        assert VolAdjuster.classify_anomaly(0.0) == "NORMAL"
        assert VolAdjuster.classify_anomaly(0.5) == "NORMAL"
        assert VolAdjuster.classify_anomaly(1.0) == "NORMAL"

    def test_classify_high(self):
        """1.0 < z ≤ 2.0 → HIGH"""
        assert VolAdjuster.classify_anomaly(1.5) == "HIGH"
        assert VolAdjuster.classify_anomaly(2.0) == "HIGH"

    def test_classify_extreme(self):
        """z > 2.0 → EXTREME"""
        assert VolAdjuster.classify_anomaly(2.5) == "EXTREME"
        assert VolAdjuster.classify_anomaly(3.0) == "EXTREME"

    def test_classify_negative_zscore(self):
        """負的 Z-score → NORMAL"""
        assert VolAdjuster.classify_anomaly(-1.5) == "NORMAL"

    # ── 品質評分測試 ────────────────────────────────────────────

    def test_quality_at_zero_zscore(self):
        """z = 0 → quality ≈ 100"""
        q = VolAdjuster.compute_quality(0.0)
        assert 95 < q <= 100

    def test_quality_at_1sigma(self):
        """z = 1.0 → quality ≈ 60"""
        q = VolAdjuster.compute_quality(1.0)
        assert 55 < q < 65

    def test_quality_at_2sigma(self):
        """z = 2.0 → quality ≈ 14"""
        q = VolAdjuster.compute_quality(2.0)
        assert 10 < q < 20

    def test_quality_at_3sigma(self):
        """z = 3.0 → quality 極低"""
        q = VolAdjuster.compute_quality(3.0)
        assert q < 5

    def test_quality_bounds(self):
        """品質評分應在 [0, 100] 範圍"""
        for z in [-3, -1, 0, 1, 2, 3]:
            q = VolAdjuster.compute_quality(float(z))
            assert 0 <= q <= 100

    # ── 完整 adjust() 函數測試 ────────────────────────────────────

    def test_adjust_normal_morning(self):
        """正常時段、正常成交量"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        vol_adj = VolAdjuster.adjust(ts, 450)

        assert vol_adj.segment == "morning"
        assert vol_adj.raw_volume == 450
        assert vol_adj.anomaly_level == "NORMAL"
        assert vol_adj.quality_rating > 80

    def test_adjust_high_anomaly_morning(self):
        """正常時段、高異常成交量"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        vol_adj = VolAdjuster.adjust(ts, 588)

        assert vol_adj.segment == "morning"
        assert vol_adj.anomaly_level == "HIGH"
        assert 50 < vol_adj.quality_rating < 70

    def test_adjust_extreme_anomaly_morning(self):
        """正常時段、極異常成交量"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        vol_adj = VolAdjuster.adjust(ts, 750)

        assert vol_adj.segment == "morning"
        assert vol_adj.anomaly_level == "EXTREME"
        assert vol_adj.quality_rating < 20

    def test_adjust_non_trading_hours(self):
        """非交易時段"""
        ts = datetime(2026, 10, 15, 12, 0).timestamp()
        vol_adj = VolAdjuster.adjust(ts, 100)

        assert vol_adj.segment is None
        assert vol_adj.anomaly_level == "UNKNOWN"
        assert vol_adj.quality_rating == 0.0

    def test_adjust_different_segments(self):
        """不同時段的正常成交量應該有不同的異常度"""
        vol = 450

        ts_morning = datetime(2026, 10, 15, 10, 0).timestamp()
        ts_afternoon = datetime(2026, 10, 15, 14, 0).timestamp()
        ts_night = datetime(2026, 10, 15, 20, 0).timestamp()

        adj_m = VolAdjuster.adjust(ts_morning, vol)
        adj_a = VolAdjuster.adjust(ts_afternoon, vol)
        adj_n = VolAdjuster.adjust(ts_night, vol)

        # 450 在早盤是正常，在午盤是偏高，在夜盤是極異常
        assert adj_m.anomaly_level == "NORMAL"
        assert adj_a.anomaly_level == "HIGH"
        assert adj_n.anomaly_level == "EXTREME"

    # ── VolAdjustment 物件測試 ────────────────────────────────────

    def test_vol_adjustment_to_dict(self):
        """VolAdjustment 轉換為字典"""
        vol_adj = VolAdjustment(
            ts=1729048500,
            raw_volume=450,
            z_score=0.123456,
            anomaly_level="NORMAL",
            quality_rating=99.876543,
            segment="morning"
        )

        d = vol_adj.to_dict()
        assert d["ts"] == 1729048500
        assert d["raw_volume"] == 450
        assert d["z_score"] == 0.12  # 四捨五入
        assert d["quality_rating"] == 99.9  # 四捨五入
        assert d["segment"] == "morning"

    # ── 邊界與邊緣情況 ────────────────────────────────────────────

    def test_zscore_zero_volume(self):
        """零成交量"""
        z = VolAdjuster.compute_zscore(0, "morning")
        assert z < 0  # 遠低於均值

    def test_zscore_extreme_high_volume(self):
        """極高成交量"""
        z = VolAdjuster.compute_zscore(2000, "morning")
        assert z > 5

    def test_adjust_timestamp_precision(self):
        """時間戳精度測試"""
        # 09:15:00 vs 09:15:01
        ts_exact = datetime(2026, 10, 15, 9, 15, 0).timestamp()
        ts_plus1s = datetime(2026, 10, 15, 9, 15, 1).timestamp()

        adj_exact = VolAdjuster.adjust(ts_exact, 450)
        adj_plus1s = VolAdjuster.adjust(ts_plus1s, 450)

        assert adj_exact.segment == "morning"
        assert adj_plus1s.segment == "morning"


class TestVolAdjusterPerformance:
    """性能測試"""

    def test_performance_1000_calculations(self):
        """1000 次計算 < 100ms"""
        import time

        start = time.time()
        for i in range(1000):
            ts = datetime(2026, 10, 15, 10, i % 60).timestamp()
            VolAdjuster.adjust(ts, 400 + i % 100)
        elapsed = time.time() - start

        assert elapsed < 0.1, f"Performance too slow: {elapsed:.3f}s"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
