"""mts_vol_quality.py — Vol Quality Score (修正版本)"""

from dataclasses import dataclass, asdict
from datetime import datetime, time
from typing import Optional, List, Dict


@dataclass
class QualityScore:
    """成交品質評分"""
    ts: float
    quality_score: float
    quality_level: str
    order_count: int
    fill_count: int
    fraud_indicators: dict
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d.update({
            "quality_score": round(self.quality_score, 1),
            "fraud_indicators": {
                k: round(v, 1) for k, v in self.fraud_indicators.items()
            }
        })
        return d


class VolQuality:
    """成交品質評分"""
    
    @staticmethod
    def compute(ts: float, order_count: int, fill_count: int, event_sequence: List[Dict]) -> QualityScore:
        """主計算函數"""
        fraud_indicators = VolQuality._compute_fraud_indicators(ts, order_count, fill_count, event_sequence)
        
        # 加權平均詐欺指標
        fraud_score = (
            fraud_indicators.get("stacking", 0) * 0.3 +
            fraud_indicators.get("density", 0) * 0.2 +
            fraud_indicators.get("time_cluster", 0) * 0.25 +
            fraud_indicators.get("imbalance", 0) * 0.25
        )
        
        # follow_through 獨立處理（如果成交差，直接降低質量）
        follow_through = fraud_indicators.get("follow_through", 50)
        if follow_through > 50:  # 成交差
            fraud_score = max(fraud_score, follow_through - 30)  # 至少要有 follow_through - 30 的詐欺分
        
        quality_score = 100 - min(100, fraud_score)
        quality_level = VolQuality._classify_quality(quality_score)
        
        return QualityScore(ts=ts, quality_score=quality_score, quality_level=quality_level,
                          order_count=order_count, fill_count=fill_count, fraud_indicators=fraud_indicators)
    
    @staticmethod
    def _compute_fraud_indicators(ts: float, order_count: int, fill_count: int, event_sequence: List[Dict]) -> dict:
        return {
            "stacking": VolQuality._compute_order_stacking(event_sequence),
            "density": VolQuality._compute_event_density(event_sequence),
            "time_cluster": VolQuality._compute_time_clustering(ts),
            "imbalance": VolQuality._compute_bid_ask_imbalance(event_sequence),
            "follow_through": VolQuality._compute_follow_through(order_count, fill_count),
        }
    
    @staticmethod
    def _compute_order_stacking(event_sequence: List[Dict]) -> float:
        order_ids = [e.get('order_id') for e in event_sequence if e.get('order_id')]
        if not order_ids: return 0.0
        order_counts = {}
        for oid in order_ids:
            order_counts[oid] = order_counts.get(oid, 0) + 1
        repeated = sum(1 for count in order_counts.values() if count > 1)
        return min(100, (repeated / len(order_counts)) * 150)
    
    @staticmethod
    def _compute_event_density(event_sequence: List[Dict]) -> float:
        expected_per_5min = 20
        actual = len(event_sequence)
        if actual <= expected_per_5min: return 0.0
        ratio = actual / expected_per_5min
        return min(100, (ratio - 1) * 50)
    
    @staticmethod
    def _compute_time_clustering(ts: float) -> float:
        # 2026-10-19 Hermes Agent: score only the agreed session open/close intervals.
        clock = datetime.fromtimestamp(ts).time()
        if time(13, 35) <= clock <= time(13, 45): return 70.0
        if time(4, 50) <= clock <= time(5, 0): return 85.0
        if time(8, 45) <= clock <= time(8, 55): return 50.0
        if time(15, 0) <= clock <= time(15, 10): return 50.0
        return 0.0
    
    @staticmethod
    def _compute_bid_ask_imbalance(event_sequence: List[Dict]) -> float:
        sells = sum(1 for e in event_sequence if e.get('side') == 'sell')
        buys = sum(1 for e in event_sequence if e.get('side') == 'buy')
        total = sells + buys
        if total == 0: return 0.0
        imbalance = abs(sells - buys) / total
        return imbalance * 100
    
    @staticmethod
    def _compute_follow_through(order_count: int, fill_count: int) -> float:
        """填充率的反向指標：成交差 → 分數高"""
        if order_count == 0: return 50.0
        fill_ratio = fill_count / order_count
        if fill_ratio >= 0.85: return 0.0  # 成交好
        elif fill_ratio <= 0.15: return 100.0  # 成交差
        else:
            return (0.85 - fill_ratio) / 0.7 * 100  # 線性
    
    @staticmethod
    def _classify_quality(score: float) -> str:
        if score >= 85: return "EXCELLENT"
        elif score >= 70: return "GOOD"
        elif score >= 50: return "SUSPICIOUS"
        else: return "FRAUDULENT"
