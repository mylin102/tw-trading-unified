"""
mts_oi_estimator.py — Open Interest (OI) 推估模組

從 mts_spread_events.jsonl 事件日誌推估市場 OI 活躍度，用於判斷：
- 新倉潮 (OI ↑) vs 平倉衝刺 (OI ↓)
- OI 方向強度 (0 = 全平倉, 1 = 全新倉, 0.5 = 平衡)
- OI 信號可靠性 (% of orders with actual fills)
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Set


@dataclass
class OISnapshot:
    """5 分鐘 OI 快照"""
    ts: float  # epoch seconds (bar close time)
    oi_direction: float  # [0, 1] — 新倉比例
    oi_intensity: float  # [0, +∞] — 每分鐘平均訂單數
    filled_ratio: float  # [0, 1] — 有成交支持的訂單 %
    new_orders: int  # 該 bucket 內新倉訂單數
    close_orders: int  # 該 bucket 內平倉訂單數
    total_fills: int  # 該 bucket 內成交數
    quality_score: float  # [0, 100] — 信號品質

    def to_dict(self) -> dict:
        """轉換為字典"""
        d = asdict(self)
        d.update({
            "oi_direction": round(self.oi_direction, 3),
            "oi_intensity": round(self.oi_intensity, 2),
            "filled_ratio": round(self.filled_ratio, 3),
            "quality_score": round(self.quality_score, 1),
        })
        return d


class OIEstimator:
    """
    推估市場 OI 活躍度的類別

    用途：在 mts_hvwap_candidate.py 的 hvwap_candidate() 中
    加入 OI 信號作為「籌碼強度」的輸入
    """

    # 2026-11-23 Gemini CLI: Reverse-chunk tail-read constants to bound memory/CPU
    # regardless of total mts_spread_events.jsonl file size.
    _TAIL_CHUNK_BYTES = 256 * 1024
    _OUT_OF_ORDER_GRACE_SECS = 60.0

    def __init__(self, events_log_path: str):
        """
        Args:
            events_log_path: mts_spread_events.jsonl 的路徑
        """
        self.log_path = events_log_path
        self._event_cache: Optional[List[Dict]] = None
        self._cache_mtime: Optional[float] = None
        self._cache_key: Optional[tuple] = None

    def _read_tail_window(
        self,
        min_ts: float,
        max_ts: Optional[float] = None,
    ) -> List[Dict]:
        """2026-11-23 Gemini CLI: Read JSONL backwards from EOF in fixed chunks,
        retaining only events in (min_ts, max_ts] and stopping once timestamps
        fall past (min_ts - _OUT_OF_ORDER_GRACE_SECS).
        """
        stop_ts = min_ts - self._OUT_OF_ORDER_GRACE_SECS
        collected_rev: List[Dict] = []
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                pos = f.tell()
                remainder = b""
                stop_reading = False
                while pos > 0 and not stop_reading:
                    read_size = min(self._TAIL_CHUNK_BYTES, pos)
                    pos -= read_size
                    f.seek(pos)
                    buf = f.read(read_size) + remainder
                    lines = buf.split(b"\n")
                    if pos > 0:
                        remainder = lines[0]
                        complete_lines = lines[1:]
                    else:
                        remainder = b""
                        complete_lines = lines
                    for raw_line in reversed(complete_lines):
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            event = json.loads(raw_line.decode("utf-8", errors="replace"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        event_ts = self._parse_ts(event.get("ts", ""))
                        if event_ts is None:
                            continue
                        if event_ts <= stop_ts:
                            stop_reading = True
                            break
                        if event_ts > min_ts and (max_ts is None or event_ts <= max_ts):
                            collected_rev.append(event)
        except Exception:
            pass
        collected_rev.reverse()
        return collected_rev

    def _load_events(
        self,
        min_ts: Optional[float] = None,
        max_ts: Optional[float] = None,
    ) -> List[Dict]:
        """讀取並快取事件日誌（指定時間窗口時改採檔尾分塊倒讀，避免全檔載入）"""
        if not os.path.exists(self.log_path):
            self._event_cache = None
            self._cache_mtime = None
            self._cache_key = None
            return []

        # 檢查檔案是否更新與窗口參數是否一致
        stat = os.stat(self.log_path)
        current_mtime = stat.st_mtime
        cache_key = (current_mtime, stat.st_size, min_ts, max_ts)
        if self._event_cache is not None and self._cache_key == cache_key:
            return self._event_cache

        # 2026-11-23 Gemini CLI: Release previous cache before allocating the new window
        # list so reloads never hold 2x parsed events simultaneously.
        self._event_cache = None
        self._cache_key = None

        if min_ts is not None:
            events = self._read_tail_window(min_ts=min_ts, max_ts=max_ts)
        else:
            events = []
            try:
                with open(self.log_path, 'r') as f:
                    for line in f:
                        if line.strip():
                            try:
                                events.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
            except Exception:
                pass

        self._event_cache = events
        self._cache_mtime = current_mtime
        self._cache_key = cache_key
        return events

    def estimate_oi(
        self,
        decision_ts: float,
        window_secs: int = 300
    ) -> OISnapshot:
        """
        估計決策時點的 OI 活躍度

        Args:
            decision_ts: 決策時間 (epoch seconds)
            window_secs: 時間窗口 (預設 5 分鐘 = 300 秒)

        Returns:
            OISnapshot 物件
        """
        # 2026-11-23 Gemini CLI: Load only the bounded (decision_ts - window_secs, decision_ts]
        # tail window so memory & latency stay O(window) instead of O(entire file).
        window_start = decision_ts - window_secs
        window_events = self._load_events(min_ts=window_start, max_ts=decision_ts)

        # 分類訂單
        new_orders = self._deduplicate_orders(window_events, [
            'ENTRY_SUBMITTED', 'ENTRY', 'ENTRY_AUDIT'
        ])
        close_orders = self._deduplicate_orders(window_events, [
            'EXIT_LOG', 'RELEASE_NEAR_SUBMITTED', 'RELEASE_FAR_SUBMITTED',
            'EXIT_REMAINING'
        ])

        # 統計成交
        fills = [e for e in window_events if e.get('event') == 'LEG_FILLED']

        # 計算新倉 vs 平倉的成交比例
        new_fills = self._count_order_fills(window_events, new_orders, 'LEG_FILLED')
        close_fills = len(fills) - new_fills

        # 計算指標
        total_orders = len(new_orders) + len(close_orders)
        oi_direction = len(new_orders) / total_orders if total_orders > 0 else 0.5
        oi_intensity = total_orders / max(window_secs / 60, 1)
        filled_ratio = len(fills) / total_orders if total_orders > 0 else 0.0
        quality_score = self._calculate_quality_score(filled_ratio)

        return OISnapshot(
            ts=decision_ts,
            oi_direction=oi_direction,
            oi_intensity=oi_intensity,
            filled_ratio=filled_ratio,
            new_orders=len(new_orders),
            close_orders=len(close_orders),
            total_fills=len(fills),
            quality_score=quality_score,
        )

    @staticmethod
    def _parse_ts(ts_str: str) -> Optional[float]:
        """解析時間戳字符串為 epoch seconds"""
        if not ts_str:
            return None
        try:
            dt = datetime.fromisoformat(ts_str)
            return dt.timestamp()
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _deduplicate_orders(
        events: List[Dict],
        event_types: List[str]
    ) -> Set[str]:
        """
        去重事件中的 order_id

        Args:
            events: 事件列表
            event_types: 要篩選的事件類型

        Returns:
            唯一 order_id 的集合
        """
        return {
            e.get('order_id')
            for e in events
            if e.get('event') in event_types and e.get('order_id')
        }

    @staticmethod
    def _count_order_fills(
        events: List[Dict],
        order_ids: Set[str],
        fill_event_type: str = 'LEG_FILLED'
    ) -> int:
        """
        計算特定 order_id 集合中有成交的數量

        Args:
            events: 事件列表
            order_ids: order_id 集合
            fill_event_type: 成交事件類型

        Returns:
            有成交的 order_id 數量
        """
        fill_orders = {
            e.get('order_id')
            for e in events
            if e.get('event') == fill_event_type and e.get('order_id')
        }
        return len(fill_orders & order_ids)

    @staticmethod
    def _calculate_quality_score(filled_ratio: float) -> float:
        """
        信號品質評分 (0-100)
        filled_ratio 越高 → 訂單越真實 → 分數越高

        公式: quality = sigmoid((filled_ratio - 0.5) * 10) * 100
              - filled_ratio=0 → quality ≈ 0
              - filled_ratio=0.5 → quality = 50
              - filled_ratio=1 → quality ≈ 100

        Args:
            filled_ratio: 有成交支持的訂單比例 [0, 1]

        Returns:
            品質評分 [0, 100]
        """
        x = (filled_ratio - 0.5) * 10
        try:
            sigmoid = 1 / (1 + math.exp(-x))
            return sigmoid * 100
        except (ValueError, OverflowError):
            return 100.0 if filled_ratio > 0.5 else 0.0
