import logging
from typing import Dict, Any, Optional
from core.performance.performance_aggregator import PerformanceAggregator

logger = logging.getLogger(__name__)

class KillSwitch:
    """
    Automated safety circuit breaker based on real-time performance.
    Transitions system from L3 (Risk Guarded) to L4 (Quantifiable Decisions).
    """

    def __init__(self, aggregator: PerformanceAggregator, config: Dict[str, Any]):
        self.aggregator = aggregator
        self.cfg = config
        self.daily_loss_limit = config.get("daily_loss_limit", 20000)
        self.strategy_win_rate_threshold = config.get("strategy_min_win_rate", 35.0)
        self.strategy_min_trades = config.get("strategy_min_trades", 5)

    def check_system_health(self) -> (bool, str):
        """
        Checks overall system health. 
        Returns (is_healthy, reason).
        """
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        metrics = self.aggregator.get_daily_metrics(today)
        
        if not metrics:
            return True, "OK"

        # Check Daily Loss
        try:
            # net_pnl_cash is formatted string like "+1,234"
            pnl_val = float(str(metrics["net_pnl_cash"]).replace(",", "").replace("+", ""))
            if pnl_val < -self.daily_loss_limit:
                msg = f"DAILY_LOSS_LIMIT_EXCEEDED: {pnl_val} < -{self.daily_loss_limit}"
                logger.critical(f"[KILL_SWITCH][SYSTEM] {msg}")
                return False, msg
        except (ValueError, KeyError):
            pass

        return True, "OK"

    def is_strategy_allowed(self, strategy_name: str) -> (bool, str):
        """
        Checks if a specific strategy has edge based on recent performance.
        """
        metrics = self.aggregator.get_strategy_performance(strategy_name, lookback_trades=20)
        
        if not metrics or metrics.get("count", 0) < self.strategy_min_trades:
            return True, "INSUFFICIENT_DATA"

        # Check Win Rate
        try:
            wr_val = float(str(metrics["win_rate"]).replace("%", ""))
            if wr_val < self.strategy_win_rate_threshold:
                msg = f"STRATEGY_UNDER_THRESHOLD: {strategy_name} WR {wr_val}% < {self.strategy_win_rate_threshold}%"
                logger.warning(f"[KILL_SWITCH][STRATEGY] {msg}")
                return False, msg
        except (ValueError, KeyError):
            pass

        return True, "OK"
