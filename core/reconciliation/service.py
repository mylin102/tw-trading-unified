import time
import logging
from typing import Optional, Any
from core.reconciliation.schemas import PositionState, ReconcileStatus, ReconcileSeverity
from core.reconciliation.position_reconciler import reconcile_positions

logger = logging.getLogger(__name__)

class ReconciliationService:
    """
    Background service that performs periodic position reconciliation.
    Integrated with Shioaji API and the local PaperTrader engine.
    """

    def __init__(self, api, trader, db_manager, symbol: str = "TMF"):
        self.api = api
        self.trader = trader
        self.db_manager = db_manager
        self.symbol = symbol
        self.is_halted = False
        self.last_reconcile_at = 0
        self.reconcile_interval = 60 # Default to 60 seconds

    def run_check(self) -> bool:
        """
        Executes a three-way reconciliation check.
        Returns True if system is healthy, False if mismatch detected.
        """
        try:
            # 1. Fetch Broker Position
            broker_qty = 0
            broker_avg_price = 0.0
            
            if self.api:
                # safe_api_call could be used here if needed
                positions = self.api.list_positions()
                if positions:
                    for pos in positions:
                        if pos.code == self.symbol or pos.symbol == self.symbol:
                            broker_qty = int(pos.quantity)
                            broker_avg_price = float(pos.price)
                            break
            
            broker_state = PositionState(
                qty=broker_qty, 
                avg_price=broker_avg_price, 
                symbol=self.symbol, 
                source="broker"
            )

            # 2. Fetch Local State (In-memory)
            local_state = self.trader.get_position_state()

            # 3. Fetch Ledger State (from SQLite Database)
            ledger_qty = self._calculate_ledger_position()
            ledger_state = PositionState(
                qty=ledger_qty,
                avg_price=local_state.avg_price, # Simplified
                symbol=self.symbol,
                source="ledger"
            )

            # Perform Reconciliation
            result = reconcile_positions(broker_state, local_state, ledger_state)

            if not result.is_ok:
                level = logging.ERROR if result.is_critical else logging.WARNING
                logger.log(level, f"🚨 [RECONCILE] {result.reason}: {result.details}")
                
                if result.is_critical:
                    self.is_halted = True
                    logger.critical("🛑 TRADING HALTED due to position mismatch.")
                    return False
            
            self.last_reconcile_at = time.time()
            return True

        except Exception as e:
            logger.exception(f"Error during reconciliation check: {e}")
            return False

    def _calculate_ledger_position(self) -> int:
        """Calculates current net position from the trades table."""
        if not self.db_manager:
            return self.trader.position
            
        try:
            # Use SQLite directly for aggregation to be more efficient than get_trade_history
            query = """
                SELECT 
                    SUM(CASE 
                        WHEN type = 'ENTRY' AND direction = 'LONG' THEN lots
                        WHEN type = 'ENTRY' AND direction = 'SHORT' THEN -lots
                        WHEN type IN ('EXIT', 'PARTIAL_EXIT', 'PARTIAL') AND direction = 'LONG' THEN -lots
                        WHEN type IN ('EXIT', 'PARTIAL_EXIT', 'PARTIAL') AND direction = 'SHORT' THEN lots
                        ELSE 0 
                    END) as net_pos
                FROM trades
                WHERE ticker = ?
            """
            with self.db_manager._get_connection() as conn:
                row = conn.execute(query, (self.symbol,)).fetchone()
                return int(row["net_pos"] or 0)
        except Exception as e:
            logger.error(f"Failed to calculate ledger position: {e}")
            return self.trader.position
