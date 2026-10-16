# 2026-10-15 Hermes Agent: integrate supplied tests using package imports; expectations unchanged.
"""
test_mts_oi_estimator.py — OI Estimator 完整測試套件

覆蓋：基礎計算、邊界情況、性能、去重邏輯
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from strategies.plugins.futures.active.mts_oi_estimator import OIEstimator, OISnapshot


class TestOIEstimator:
    """OI Estimator 測試類別"""

    @pytest.fixture
    def temp_log_file(self):
        """創建臨時日誌檔案"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            temp_path = f.name
        yield temp_path
        # cleanup
        if os.path.exists(temp_path):
            os.unlink(temp_path)

    @pytest.fixture
    def estimator(self, temp_log_file):
        """創建 OI 推估器"""
        return OIEstimator(temp_log_file)

    def write_events(self, path: str, events: list):
        """寫入測試事件"""
        with open(path, 'w') as f:
            for event in events:
                f.write(json.dumps(event) + '\n')

    # ── 基礎計算測試 ──────────────────────────────────────────

    def test_oi_direction_all_new_orders(self, estimator, temp_log_file):
        """全新倉 → oi_direction == 1.0"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        events = [
            {"event": "ENTRY", "order_id": "A", "ts": "2026-10-15T10:00:00"},
            {"event": "ENTRY", "order_id": "B", "ts": "2026-10-15T10:01:00"},
        ]
        self.write_events(temp_log_file, events)

        snapshot = estimator.estimate_oi(ts)
        assert snapshot.oi_direction == 1.0
        assert snapshot.new_orders == 2
        assert snapshot.close_orders == 0

    def test_oi_direction_all_close_orders(self, estimator, temp_log_file):
        """全平倉 → oi_direction == 0.0"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        events = [
            {"event": "EXIT_LOG", "order_id": "C", "ts": "2026-10-15T10:00:00"},
            {"event": "EXIT_LOG", "order_id": "D", "ts": "2026-10-15T10:01:00"},
        ]
        self.write_events(temp_log_file, events)

        snapshot = estimator.estimate_oi(ts)
        assert snapshot.oi_direction == 0.0
        assert snapshot.new_orders == 0
        assert snapshot.close_orders == 2

    def test_oi_direction_mixed(self, estimator, temp_log_file):
        """混合 2 新 + 1 平 → oi_direction == 2/3 ≈ 0.667"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        events = [
            {"event": "ENTRY", "order_id": "A", "ts": "2026-10-15T09:56:00"},
            {"event": "ENTRY", "order_id": "B", "ts": "2026-10-15T09:57:00"},
            {"event": "EXIT_LOG", "order_id": "C", "ts": "2026-10-15T09:58:00"},
        ]
        self.write_events(temp_log_file, events)

        snapshot = estimator.estimate_oi(ts)
        assert abs(snapshot.oi_direction - 2/3) < 0.01
        assert snapshot.new_orders == 2
        assert snapshot.close_orders == 1

    def test_filled_ratio_all_filled(self, estimator, temp_log_file):
        """所有訂單都成交 → filled_ratio == 1.0"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        events = [
            {"event": "ENTRY", "order_id": "A", "ts": "2026-10-15T09:56:00"},
            {"event": "ENTRY", "order_id": "B", "ts": "2026-10-15T09:57:00"},
            {"event": "LEG_FILLED", "order_id": "A", "ts": "2026-10-15T09:56:30"},
            {"event": "LEG_FILLED", "order_id": "B", "ts": "2026-10-15T09:57:30"},
        ]
        self.write_events(temp_log_file, events)

        snapshot = estimator.estimate_oi(ts)
        assert snapshot.filled_ratio == 1.0
        assert snapshot.total_fills == 2

    def test_filled_ratio_partial(self, estimator, temp_log_file):
        """部分成交 → filled_ratio == 0.5"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        events = [
            {"event": "ENTRY", "order_id": "A", "ts": "2026-10-15T09:56:00"},
            {"event": "ENTRY", "order_id": "B", "ts": "2026-10-15T09:57:00"},
            {"event": "LEG_FILLED", "order_id": "A", "ts": "2026-10-15T09:56:30"},
            # B 沒有成交
        ]
        self.write_events(temp_log_file, events)

        snapshot = estimator.estimate_oi(ts)
        assert snapshot.filled_ratio == 0.5
        assert snapshot.total_fills == 1

    # ── 邊界情況測試 ────────────────────────────────────────────

    def test_empty_events(self, estimator, temp_log_file):
        """空事件列表 → 返回 default OISnapshot"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        self.write_events(temp_log_file, [])

        snapshot = estimator.estimate_oi(ts)
        assert snapshot.oi_direction == 0.5  # 預設平衡
        assert snapshot.new_orders == 0
        assert snapshot.close_orders == 0
        assert snapshot.total_fills == 0

    def test_no_order_id(self, estimator, temp_log_file):
        """event 無 order_id → 忽略"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        events = [
            {"event": "ENTRY"},  # 無 order_id
            {"event": "ENTRY", "order_id": "A", "ts": "2026-10-15T09:56:00"},
        ]
        self.write_events(temp_log_file, events)

        snapshot = estimator.estimate_oi(ts)
        assert snapshot.new_orders == 1  # 只計算有 order_id 的

    def test_duplicate_order_ids(self, estimator, temp_log_file):
        """同一 order_id 多次出現 → 去重"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        events = [
            {"event": "ENTRY", "order_id": "A", "ts": "2026-10-15T09:56:00"},
            {"event": "ENTRY", "order_id": "A", "ts": "2026-10-15T09:56:10"},  # 重複
            {"event": "ENTRY_AUDIT", "order_id": "A", "ts": "2026-10-15T09:56:20"},  # 同樣去重
            {"event": "ENTRY", "order_id": "B", "ts": "2026-10-15T09:57:00"},
        ]
        self.write_events(temp_log_file, events)

        snapshot = estimator.estimate_oi(ts)
        assert snapshot.new_orders == 2  # 去重後只有 A 和 B

    def test_window_boundary_inclusive(self, estimator, temp_log_file):
        """時間窗口邊界應包含"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        window_secs = 300  # 5 分鐘

        events = [
            # 剛好在窗口開始邊界
            {"event": "ENTRY", "order_id": "A", "ts": "2026-10-15T09:55:00"},
            # 中間
            {"event": "ENTRY", "order_id": "B", "ts": "2026-10-15T09:57:30"},
            # 在窗口結束邊界
            {"event": "ENTRY", "order_id": "C", "ts": "2026-10-15T10:00:00"},
            # 超出窗口
            {"event": "ENTRY", "order_id": "D", "ts": "2026-10-15T10:00:01"},
        ]
        self.write_events(temp_log_file, events)

        snapshot = estimator.estimate_oi(ts, window_secs=window_secs)
        # 應該包含 A, B, C，不包含 D
        assert snapshot.new_orders == 3

    def test_invalid_timestamp(self, estimator, temp_log_file):
        """無效時間戳 → 忽略"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        events = [
            {"event": "ENTRY", "order_id": "A", "ts": "invalid_time"},
            {"event": "ENTRY", "order_id": "B", "ts": "2026-10-15T09:56:00"},
        ]
        self.write_events(temp_log_file, events)

        snapshot = estimator.estimate_oi(ts)
        assert snapshot.new_orders == 1  # 只計算 B

    # ── 品質評分測試 ────────────────────────────────────────────

    def test_quality_score_at_zero_filled_ratio(self, estimator, temp_log_file):
        """filled_ratio = 0 → quality < 10"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        events = [
            {"event": "ENTRY", "order_id": "A", "ts": "2026-10-15T09:56:00"},
            # 沒有成交
        ]
        self.write_events(temp_log_file, events)

        snapshot = estimator.estimate_oi(ts)
        assert snapshot.quality_score < 10

    def test_quality_score_at_perfect_filled_ratio(self, estimator, temp_log_file):
        """filled_ratio = 1.0 → quality > 95"""
        ts = datetime(2026, 10, 15, 10, 0).timestamp()
        events = [
            {"event": "ENTRY", "order_id": "A", "ts": "2026-10-15T09:56:00"},
            {"event": "LEG_FILLED", "order_id": "A", "ts": "2026-10-15T09:56:30"},
        ]
        self.write_events(temp_log_file, events)

        snapshot = estimator.estimate_oi(ts)
        assert snapshot.quality_score > 95

    # ── 靜態方法測試 ────────────────────────────────────────────

    def test_parse_ts_valid_iso_string(self):
        """解析有效的 ISO 時間戳"""
        ts_str = "2026-10-15T09:30:00"
        ts = OIEstimator._parse_ts(ts_str)
        assert ts is not None
        assert isinstance(ts, float)

    def test_parse_ts_invalid_string(self):
        """無效時間戳 → None"""
        ts = OIEstimator._parse_ts("not_a_time")
        assert ts is None

    def test_parse_ts_empty_string(self):
        """空字符串 → None"""
        ts = OIEstimator._parse_ts("")
        assert ts is None

    def test_deduplicate_orders_single_event_type(self):
        """去重單一事件類型"""
        events = [
            {"event": "ENTRY", "order_id": "A"},
            {"event": "ENTRY", "order_id": "B"},
            {"event": "ENTRY", "order_id": "A"},  # 重複
        ]
        result = OIEstimator._deduplicate_orders(events, ["ENTRY"])
        assert len(result) == 2
        assert "A" in result and "B" in result

    def test_deduplicate_orders_multiple_event_types(self):
        """去重多個事件類型"""
        events = [
            {"event": "ENTRY", "order_id": "A"},
            {"event": "ENTRY_AUDIT", "order_id": "A"},  # 同一訂單，不同事件
            {"event": "ENTRY", "order_id": "B"},
        ]
        result = OIEstimator._deduplicate_orders(
            events, ["ENTRY", "ENTRY_AUDIT"]
        )
        assert len(result) == 2  # A 和 B（A 去重）

    def test_calculate_quality_score_bounds(self):
        """品質評分應在 [0, 100] 範圍"""
        for ratio in [0.0, 0.25, 0.5, 0.75, 1.0]:
            score = OIEstimator._calculate_quality_score(ratio)
            assert 0 <= score <= 100

    # ── OISnapshot 測試 ────────────────────────────────────────

    def test_oisnapshot_to_dict(self):
        """OISnapshot 轉換為字典"""
        snapshot = OISnapshot(
            ts=1729048500,
            oi_direction=0.6667,
            oi_intensity=0.6,
            filled_ratio=1.0,
            new_orders=2,
            close_orders=1,
            total_fills=3,
            quality_score=99.5,
        )
        d = snapshot.to_dict()

        assert d["ts"] == 1729048500
        assert d["oi_direction"] == 0.667  # 四捨五入到 3 位
        assert d["oi_intensity"] == 0.6
        assert d["quality_score"] == 99.5


class TestOIEstimatorPerformance:
    """性能測試"""

    def test_performance_100k_events(self):
        """處理 100k 事件 < 500ms"""
        import time

        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            temp_path = f.name

            # 生成 100k 事件
            for i in range(100000):
                event = {
                    "event": "ENTRY" if i % 2 == 0 else "EXIT_LOG",
                    "order_id": f"O{i}",
                    "ts": "2026-10-15T10:00:00"
                }
                f.write(json.dumps(event) + '\n')

        try:
            estimator = OIEstimator(temp_path)
            ts = datetime(2026, 10, 15, 10, 0).timestamp()

            start = time.time()
            snapshot = estimator.estimate_oi(ts)
            elapsed = time.time() - start

            assert elapsed < 0.5, f"Performance too slow: {elapsed:.3f}s"
            assert snapshot is not None
        finally:
            os.unlink(temp_path)


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
