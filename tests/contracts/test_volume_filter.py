"""
Volume filter contracts for _filter_watchlist_by_strength().

Tests call the REAL _filter_watchlist_by_strength() method from production monitor.py.
All tests are pure unit: no real API, no Shioaji, no network, no orders.

2026-09-20 Antigravity: Gate 1 absolute volume threshold contracts.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── Minimal StockMonitor factory ──────────────────────────────────────────────

def _make_monitor(watchlist: list, min_daily_amount: int = 50_000_000):
    """Create a minimal StockMonitor with just enough state for _filter_watchlist_by_strength."""
    from strategies.stocks.monitor import StockMonitor
    sm = object.__new__(StockMonitor)
    sm.api       = MagicMock()
    sm.watchlist = list(watchlist)
    sm.positions = {}
    sm.cfg = {
        "stocks": {
            "canslim": {"min_daily_amount": min_daily_amount}
        }
    }
    return sm


def _snap(total_amount: int, close: float = 101.0, open_: float = 100.0) -> MagicMock:
    """Build a mock snapshot object."""
    s = MagicMock()
    s.total_amount = total_amount
    s.close        = close
    s.open         = open_
    return s


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestVolumeGate1Threshold:

    def test_below_threshold_excluded(self):
        """
        Gate 1: ticker 日成交金額 < 5000 萬 → 必須被排除。
        sorted list 為空 → fallback 回傳完整 watchlist。
        這是 Gate 1 的核心 contract。
        """
        sm = _make_monitor(["LOW_LIQ"])
        sm.api.Contracts.Stocks.__getitem__ = MagicMock(return_value=MagicMock())
        # 1000 萬，低於 5000 萬門檻
        sm.api.snapshots.return_value = [_snap(10_000_000)]

        result = sm._filter_watchlist_by_strength()

        # LOW_LIQ 被 Gate 1 過濾；scored 空 → fallback
        assert result == sm.watchlist  # fallback 觸發，代表 Gate 1 有作用

    def test_above_threshold_strong_included(self):
        """
        Gate 1 + Gate 2 都通過（高量 + 強勢開盤）→ ticker 出現在結果。
        """
        sm = _make_monitor(["2330"])
        sm.api.Contracts.Stocks.__getitem__ = MagicMock(return_value=MagicMock())
        # 50 億，遠超門檻；收 > 開（強勢）
        sm.api.snapshots.return_value = [_snap(5_000_000_000, close=900, open_=880)]

        result = sm._filter_watchlist_by_strength()

        assert "2330" in result

    def test_above_threshold_but_weak_not_in_result(self):
        """
        Gate 1 通過（量足夠）但 Gate 2 失敗（收 <= 開） → 不進入 scored，不在結果。
        watchlist 只有這一個 → fallback。
        """
        sm = _make_monitor(["WEAK"])
        sm.api.Contracts.Stocks.__getitem__ = MagicMock(return_value=MagicMock())
        # 2 億（通過 Gate 1），但收 < 開（失敗 Gate 2）
        sm.api.snapshots.return_value = [_snap(200_000_000, close=99, open_=101)]

        result = sm._filter_watchlist_by_strength()

        assert result == sm.watchlist  # fallback（scored 空）


class TestFallbackBehavior:

    def test_all_filtered_returns_full_watchlist(self):
        """
        全部 ticker 都低量 → scored 空 → fallback 回傳完整 watchlist 並告警。
        避免系統空轉。
        """
        sm = _make_monitor(["A", "B", "C"])
        sm.api.Contracts.Stocks.__getitem__ = MagicMock(return_value=MagicMock())
        # 全部 100 萬，低於門檻
        sm.api.snapshots.return_value = [_snap(1_000_000)]

        result = sm._filter_watchlist_by_strength()

        assert result == ["A", "B", "C"]  # fallback

    def test_snapshot_exception_conservative_passthrough(self):
        """
        snapshot 拋例外 → 保守策略：不誤殺，ticker 不進入 scored 但也不進 filtered_out。
        watchlist 只有例外 ticker → fallback。
        """
        sm = _make_monitor(["ERR"])
        sm.api.Contracts.Stocks.__getitem__ = MagicMock(return_value=MagicMock())
        sm.api.snapshots.side_effect = RuntimeError("API unavailable")

        result = sm._filter_watchlist_by_strength()

        # 例外後 scored 空 → fallback（不因例外誤殺 → 回原始 watchlist）
        assert result == sm.watchlist

    def test_api_none_skips_all_filtering(self):
        """
        api=None（非盤中 / dry-run） → 直接回傳完整 watchlist，完全不打 API。
        """
        sm = _make_monitor(["X", "Y", "Z"])
        sm.api = None  # 非盤中

        result = sm._filter_watchlist_by_strength()

        assert result == ["X", "Y", "Z"]


class TestRelativeRankingPreserved:

    def test_relative_ranking_order(self):
        """
        多個合格 ticker → 仍依成交金額降序排列，取前半（現有 Gate 2 邏輯不變）。
        """
        sm = _make_monitor(["A", "B", "C", "D"])

        # 設定各 ticker 的成交金額（全部超過 5000 萬門檻）
        amounts = {"A": 100_000_000, "B": 500_000_000, "C": 300_000_000, "D": 200_000_000}

        # 為每個 ticker 建立獨立的 contract mock，name 屬性設為 ticker
        contracts = {}
        for t in ["A", "B", "C", "D"]:
            c = MagicMock()
            c.name = t
            contracts[t] = c

        # __getitem__ 回傳對應的 contract
        sm.api.Contracts.Stocks.__getitem__ = MagicMock(side_effect=lambda t: contracts[t])

        # snapshots 根據傳入的 contract 的 name 屬性回傳對應金額（收 > 開，全部強勢）
        def _snapshots(contract_list):
            t = contract_list[0].name
            return [_snap(amounts[t], close=101, open_=100)]
        sm.api.snapshots.side_effect = _snapshots

        result = sm._filter_watchlist_by_strength()

        # 4 tickers, max_tickers = max(3, 4//2) = 3
        # 按量降序: B(5億) > C(3億) > D(2億) > A(1億)，取前 3
        assert len(result) == 3
        assert result[0] == "B"  # 最高量排第一



class TestCustomThreshold:

    def test_config_threshold_override(self):
        """
        YAML 設定 min_daily_amount=100_000_000（1 億）→ 使用 1 億門檻，
        而非預設 5000 萬。6000 萬不夠 → 被過濾 → fallback。
        """
        # 設定 1 億門檻
        sm = _make_monitor(["MID_CAP"], min_daily_amount=100_000_000)
        sm.api.Contracts.Stocks.__getitem__ = MagicMock(return_value=MagicMock())
        # 6000 萬：超過預設 5000 萬，但低於 config 的 1 億
        sm.api.snapshots.return_value = [_snap(60_000_000)]

        result = sm._filter_watchlist_by_strength()

        # 6000 萬 < 1 億門檻 → Gate 1 過濾 → fallback
        assert result == sm.watchlist
