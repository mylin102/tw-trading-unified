"""Periodic, read-only broker/ledger reconciliation.

The detector never submits or cancels orders.  A caller supplies the existing
sanctioned fill-repair callback; broker terminal evidence is recorded before
any local holding/authorization decision is evaluated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable


@dataclass(frozen=True)
class ReconciliationEvent:
    code: str
    detail: str
    trade_id: str = ""
    order_id: str = ""


@dataclass
class ReconciliationReport:
    events: list[ReconciliationEvent] = field(default_factory=list)
    repaired_fill_count: int = 0
    hold: bool = False
    entry_blocked: bool = False


def _value(row, *keys):
    for key in keys:
        if isinstance(row, dict) and row.get(key) is not None:
            return row[key]
        value = getattr(row, key, None)
        if value is not None:
            return value
    return None


def _terminal(row) -> bool:
    return str(_value(row, "status", "broker_status") or "").lower().split(".")[-1] in {
        "filled", "cancelled", "canceled", "rejected", "expired", "done"
    }


def _deal_rows(trade) -> Iterable[dict]:
    deals = _value(trade, "deals") or _value(_value(trade, "status"), "deals") or ()
    for deal in deals:
        yield {
            "trade_id": _value(deal, "trade_id", "broker_trade_id", "deal_id", "fill_id") or "",
            "deal_id": _value(deal, "deal_id", "trade_id", "fill_id") or "",
            "instrument_code": _value(deal, "instrument_code", "code", "symbol") or _value(trade, "code", "symbol") or "",
            "side": str(_value(deal, "side", "action") or _value(trade, "side", "action") or "").upper(),
            "quantity": _value(deal, "quantity", "qty") or 0,
            "price": _value(deal, "price", "avg_price") or 0,
            "broker_order_id": _value(trade, "broker_order_id", "id", "order_id") or "",
        }


def _fill_key(row):
    return (str(_value(row, "trade_id", "broker_trade_id") or ""),
            str(_value(row, "deal_id", "fill_id", "exchange_fill_id") or ""),
            str(_value(row, "instrument_code", "code", "symbol") or ""))


def detect_broker_reconciliation(*, broker_trades, ledger_rows, broker_legs,
                                 canonical_legs, tick_size: float = 1.0,
                                 trade_id: str = "",
                                 repair_fill: Callable[[dict], None] | None = None,
                                 emit: Callable[[ReconciliationEvent], None] | None = None) -> ReconciliationReport:
    """Compare both directions and repair only broker-proven missing fills."""
    report = ReconciliationReport()
    if broker_trades is None:
        event = ReconciliationEvent(
            "BROKER_EVIDENCE_UNAVAILABLE",
            "authoritative broker trade evidence unavailable; empty is not inferred")
        report.events.append(event)
        report.hold = True
        report.entry_blocked = True
        if emit:
            emit(event)
        return report
    ledger_keys = {_fill_key(row) for row in ledger_rows or ()
                   if str(_value(row, "execution_source") or "") == "BROKER_FILL"}
    broker_keys = set()
    for trade in broker_trades or ():
        if not _terminal(trade):
            continue
        for fill in _deal_rows(trade):
            key = _fill_key(fill)
            broker_keys.add(key)
            if key not in ledger_keys:
                event = ReconciliationEvent("BROKER_FILL_MISSING_FROM_LEDGER",
                    "terminal broker deal absent from durable ledger",
                    trade_id=fill["trade_id"], order_id=fill["broker_order_id"])
                report.events.append(event)
                if emit:
                    emit(event)
                if repair_fill:
                    repair_fill({**fill, "execution_source": "BROKER_FILL",
                                 "price_source": "BROKER_TRUTH_RECONCILE"})
                    report.repaired_fill_count += 1
    for row in ledger_rows or ():
        if str(_value(row, "execution_source") or "") != "BROKER_FILL":
            continue
        if _fill_key(row) not in broker_keys:
            event = ReconciliationEvent("BROKER_FILL_PHANTOM",
                "durable broker fill has no authoritative broker deal",
                trade_id=str(_value(row, "trade_id") or ""))
            report.events.append(event)
            if emit:
                emit(event)

    # A broker working order is an active risk, even when positions currently
    # match.  The detector is read-only and deliberately does not cancel it.
    for order in broker_trades:
        if not _terminal(order):
            event = ReconciliationEvent(
                "BROKER_WORKING_ORDER_HOLD",
                "broker reports a non-terminal working order",
                order_id=str(_value(order, "broker_order_id", "id", "order_id") or ""))
            report.events.append(event)
            report.hold = True
            report.entry_blocked = True
            if emit:
                emit(event)

    def legs_map(rows):
        return {str(_value(row, "code", "instrument_code", "symbol")): row for row in rows or ()}
    broker_map, canonical_map = legs_map(broker_legs), legs_map(canonical_legs)
    mismatch = set(broker_map) != set(canonical_map)
    for code in set(broker_map) & set(canonical_map):
        left, right = broker_map[code], canonical_map[code]
        for key in ("side", "qty"):
            if str(_value(left, key)).upper() != str(_value(right, key)).upper():
                mismatch = True
        try:
            if abs(float(_value(left, "avg_cost")) - float(_value(right, "avg_cost"))) > tick_size / 2:
                mismatch = True
        except (TypeError, ValueError):
            mismatch = True
    if mismatch:
        event = ReconciliationEvent("RECONCILIATION_HOLD",
            "broker positions disagree with canonical holding")
        report.events.append(event)
        report.hold = True
        if emit:
            emit(event)
    if not broker_map and canonical_map and trade_id:
        has_exit = any(
            str(_value(row, "trade_id") or "") == str(trade_id)
            and str(_value(row, "fill_type") or "").upper() == "EXIT"
            for row in ledger_rows or ()
        )
        if not has_exit:
            event = ReconciliationEvent(
                "BROKER_FLAT_WITHOUT_EXIT_EVIDENCE",
                "broker is flat but no audited exit fill proves local closure",
                trade_id=str(trade_id))
            report.events.append(event)
            report.hold = True
            report.entry_blocked = True
            if emit:
                emit(event)
    return report
