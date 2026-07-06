import logging
from typing import Optional
from core.reconciliation.schemas import (
    PositionState, 
    ReconcileResult, 
    ReconcileStatus, 
    ReconcileSeverity
)

logger = logging.getLogger(__name__)

def reconcile_positions(
    broker_pos: PositionState, 
    local_pos: PositionState, 
    ledger_pos: PositionState
) -> ReconcileResult:
    """
    Reconciles three sources of position truth.
    - broker_pos: Direct from Shioaji /portfolio
    - local_pos: In-memory truth (PaperTrader.position or OrderManager.active_state)
    - ledger_pos: Derived from DB/CSV trade logs
    """
    
    # 1. Quantity Mismatch (CRITICAL)
    # Check broker vs local first
    if broker_pos.qty != local_pos.qty:
        return ReconcileResult(
            status=ReconcileStatus.MISMATCH,
            severity=ReconcileSeverity.CRITICAL,
            reason="BROKER_LOCAL_QTY_DIFF",
            details=f"Broker: {broker_pos.qty}, Local: {local_pos.qty}"
        )
    
    # Check local vs ledger (consistency of the recording system)
    if local_pos.qty != ledger_pos.qty:
        return ReconcileResult(
            status=ReconcileStatus.MISMATCH,
            severity=ReconcileSeverity.CRITICAL,
            reason="LOCAL_LEDGER_QTY_DIFF",
            details=f"Local: {local_pos.qty}, Ledger: {ledger_pos.qty}"
        )

    # 2. Average Price Mismatch (WARNING)
    # Floating point comparison with small epsilon
    EPSILON = 1e-4
    if abs(local_pos.avg_price - ledger_pos.avg_price) > EPSILON:
        return ReconcileResult(
            status=ReconcileStatus.MISMATCH,
            severity=ReconcileSeverity.WARNING,
            reason="LOCAL_LEDGER_AVG_PRICE_DIFF",
            details=f"Local: {local_pos.avg_price}, Ledger: {ledger_pos.avg_price}"
        )

    # Note: Broker average price can sometimes differ due to different fee accounting,
    # so we prioritize Local vs Ledger for avg price reconciliation.
    if abs(broker_pos.avg_price - local_pos.avg_price) > 0.5: # Wider tolerance for broker
         logger.warning(
             f"Broker/Local average price discrepancy detected but not halting: "
             f"Broker {broker_pos.avg_price} vs Local {local_pos.avg_price}"
         )

    return ReconcileResult(status=ReconcileStatus.OK)
