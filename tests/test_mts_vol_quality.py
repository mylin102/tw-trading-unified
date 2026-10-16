# 2026-10-15 Hermes Agent: package imports for supplied Task 1.3.2 tests; expectations unchanged.
"""
test_mts_vol_quality.py — VolQuality 完整測試套件

覆蓋：5 項詐欺指標、品質分級、邊界情況
"""

from datetime import datetime
import pytest

from strategies.plugins.futures.active.mts_vol_quality import VolQuality, QualityScore


class TestVolQualityFraudIndicators:
    """詐欺指標計算測試"""

    def test_order_stacking_no_repeat(self):
        """無重複訂單 → stacking_score = 0"""
        events = [
            {"order_id": "A"}, {"order_id": "B"}, {"order_id": "C"}
        ]
        score = VolQuality._compute_order_stacking(events)
        assert score == 0.0

    def test_order_stacking_high_repeat(self):
        """高重複率 (66%) → stacking_score > 90"""
        events = [
            {"order_id": "A"}, {"order_id": "A"},
            {"order_id": "B"}, {"order_id": "B"},
            {"order_id": "C"},
        ]
        score = VolQuality._compute_order_stacking(events)
        assert score > 90

    def test_event_density_normal(self):
        """20 events (正常) → density_score ≈ 0"""
        events = [{"id": i} for i in range(20)]
        score = VolQuality._compute_event_density(events)
        assert abs(score - 0) < 5

    def test_event_density_high(self):
        """50 events (密集) → density_score > 70"""
        events = [{"id": i} for i in range(50)]
        score = VolQuality._compute_event_density(events)
        assert score > 70

    def test_time_clustering_session_end_morning(self):
        """14:55 (早盤末) → time_cluster_score = 70"""
        ts = datetime(2026, 10, 15, 14, 55).timestamp()
        score = VolQuality._compute_time_clustering(ts)
        assert score == 70.0

    def test_time_clustering_session_end_night(self):
        """04:55 (夜盤末) → time_cluster_score = 85"""
        ts = datetime(2026, 10, 16, 4, 55).timestamp()
        score = VolQuality._compute_time_clustering(ts)
        assert score == 85.0

    def test_time_clustering_normal_hours(self):
        """10:00 (早盤中) → time_cluster_score = 0"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        score = VolQuality._compute_time_clustering(ts)
        assert score == 0.0

    def test_bid_ask_imbalance_balanced(self):
        """5 sells, 5 buys (平衡) → imbalance_score = 0"""
        events = [
            *[{"side": "sell"} for _ in range(5)],
            *[{"side": "buy"} for _ in range(5)],
        ]
        score = VolQuality._compute_bid_ask_imbalance(events)
        assert score == 0.0

    def test_bid_ask_imbalance_extreme(self):
        """10 sells, 1 buy (嚴重不平衡) → imbalance_score ≈ 81"""
        events = [
            *[{"side": "sell"} for _ in range(10)],
            {"side": "buy"},
        ]
        score = VolQuality._compute_bid_ask_imbalance(events)
        assert abs(score - 81) < 2

    def test_follow_through_high(self):
        """fill_ratio = 90% → follow_through_score ≈ 0"""
        score = VolQuality._compute_follow_through(10, 9)
        assert score < 5

    def test_follow_through_low(self):
        """fill_ratio = 20% → follow_through_score = 100"""
        score = VolQuality._compute_follow_through(10, 2)
        assert score == 100.0

    def test_follow_through_middle(self):
        """fill_ratio = 50% → follow_through_score ≈ 60"""
        score = VolQuality._compute_follow_through(10, 5)
        assert 55 < score < 65

    # ── 完整 compute() 函數測試 ────────────────────────────────────

    def test_quality_score_excellent(self):
        """所有指標都低 → quality ≥ 85"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        quality = VolQuality.compute(
            ts=ts,
            order_count=10,
            fill_count=9,  # 90% 成交率
            event_sequence=[
                {"order_id": "A"}, {"order_id": "B"},
                {"side": "sell"}, {"side": "buy"},
            ]
        )
        assert quality.quality_score >= 85
        assert quality.quality_level == "EXCELLENT"

    def test_quality_score_good(self):
        """中等指標 → 70 ≤ quality < 85"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        quality = VolQuality.compute(
            ts=ts,
            order_count=10,
            fill_count=7,  # 70% 成交率
            event_sequence=[
                {"order_id": f"O{i}"} for i in range(25)
            ]
        )
        assert 70 <= quality.quality_score < 85
        assert quality.quality_level == "GOOD"

    def test_quality_score_suspicious(self):
        """多項詐欺指標 → 50 ≤ quality < 70"""
        ts = datetime(2026, 10, 15, 14, 55).timestamp()  # 早盤末
        quality = VolQuality.compute(
            ts=ts,
            order_count=10,
            fill_count=4,  # 40% 成交率
            event_sequence=[
                *[{"order_id": "A"} for _ in range(5)],  # 重複
                *[{"side": "sell"} for _ in range(8)],   # 買賣不平衡
                *[{"id": i} for i in range(30)],         # 高密集度
            ]
        )
        assert 50 <= quality.quality_score < 70
        assert quality.quality_level == "SUSPICIOUS"

    def test_quality_score_fraudulent(self):
        """極度詐欺跡象 → quality < 50"""
        ts = datetime(2026, 10, 15, 4, 55).timestamp()  # 夜盤末
        quality = VolQuality.compute(
            ts=ts,
            order_count=10,
            fill_count=1,  # 10% 成交率
            event_sequence=[
                *[{"order_id": "A"} for _ in range(8)],  # 極度重複
                *[{"side": "sell"} for _ in range(20)],  # 極度不平衡
                *[{"id": i} for i in range(50)],         # 極度密集
            ]
        )
        assert quality.quality_score < 50
        assert quality.quality_level == "FRAUDULENT"

    # ── 邊界情況 ────────────────────────────────────────────────────

    def test_quality_score_empty_events(self):
        """空事件序列 → 應該優雅處理"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        quality = VolQuality.compute(
            ts=ts,
            order_count=0,
            fill_count=0,
            event_sequence=[]
        )
        assert isinstance(quality.quality_score, float)
        assert 0 <= quality.quality_score <= 100

    def test_quality_score_all_filled(self):
        """100% 成交率 + 好指標 → EXCELLENT"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        quality = VolQuality.compute(
            ts=ts,
            order_count=10,
            fill_count=10,  # 100% 成交
            event_sequence=[
                {"order_id": f"O{i}", "side": "buy" if i % 2 else "sell"}
                for i in range(20)
            ]
        )
        assert quality.quality_score > 90
        assert quality.quality_level == "EXCELLENT"

    def test_quality_score_no_fills(self):
        """0% 成交率 → FRAUDULENT"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        quality = VolQuality.compute(
            ts=ts,
            order_count=10,
            fill_count=0,  # 0% 成交
            event_sequence=[{"order_id": f"O{i}"} for i in range(10)]
        )
        assert quality.quality_score < 30
        assert quality.quality_level == "FRAUDULENT"

    # ── QualityScore 物件測試 ────────────────────────────────────────

    def test_quality_score_to_dict(self):
        """QualityScore 轉換為字典"""
        quality = QualityScore(
            ts=1729048500,
            quality_score=85.123456,
            quality_level="EXCELLENT",
            order_count=10,
            fill_count=9,
            fraud_indicators={
                "stacking": 15.5,
                "density": 20.3,
                "time_cluster": 0.0,
                "imbalance": 10.1,
                "follow_through": 5.7,
            }
        )
        d = quality.to_dict()

        assert d["ts"] == 1729048500
        assert d["quality_score"] == 85.1  # 四捨五入
        assert d["quality_level"] == "EXCELLENT"
        assert d["order_count"] == 10
        assert d["fraud_indicators"]["stacking"] == 15.5

    # ── 綜合場景測試 ────────────────────────────────────────────────

    def test_scenario_real_spike(self):
        """真實爆量：高成交率、但時間/密集度可疑"""
        ts = datetime(2026, 10, 15, 14, 58).timestamp()  # 早盤末
        quality = VolQuality.compute(
            ts=ts,
            order_count=20,
            fill_count=18,  # 90% 成交率
            event_sequence=[
                {"order_id": f"O{i}", "side": "buy"}
                for i in range(30)  # 高密集
            ]
        )
        # 雖然成交率高（降低詐欺分），但時間和密集度可疑
        assert 60 <= quality.quality_score <= 80
        assert quality.quality_level in ("GOOD", "SUSPICIOUS")

    def test_scenario_fake_spike(self):
        """虛假爆量：低成交率、買賣不平衡、時間可疑"""
        ts = datetime(2026, 10, 15, 17, 59).timestamp()  # 午盤末
        quality = VolQuality.compute(
            ts=ts,
            order_count=10,
            fill_count=2,  # 20% 成交率
            event_sequence=[
                *[{"order_id": f"O{i}", "side": "sell"} for i in range(25)],
                *[{"order_id": f"D{i}"} for i in range(25)]  # 重複掛單
            ]
        )
        assert quality.quality_score < 40
        assert quality.quality_level == "FRAUDULENT"


class TestVolQualityPerformance:
    """性能測試"""

    def test_performance_1000_calculations(self):
        """1000 次計算 < 500ms"""
        import time

        start = time.time()
        for i in range(1000):
            ts = datetime(2026, 10, 15, 10, (i % 60)).timestamp()
            VolQuality.compute(
                ts=ts,
                order_count=10,
                fill_count=7,
                event_sequence=[{"id": j} for j in range(20)]
            )
        elapsed = time.time() - start

        assert elapsed < 0.5, f"Performance too slow: {elapsed:.3f}s"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
