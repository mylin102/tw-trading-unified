from core.broker_reconciliation_detector import detect_broker_reconciliation


def _trade(status="Filled"):
    return {"id": "e7ea2632", "code": "TMFJ6", "status": status,
            "deals": [{"trade_id": "broker-reconciled-b1769ea375a331bd",
                        "deal_id": "deal-far-1703", "code": "TMFJ6",
                        "side": "BUY", "quantity": 1, "price": 46429}]}


def _legs(qty=1):
    return [{"code": "TMFI6", "side": "LONG", "qty": qty, "avg_cost": 45876},
            {"code": "TMFJ6", "side": "SHORT", "qty": 1, "avg_cost": 46059}]


def test_1703_hold_still_repairs_broker_fill_and_never_broker_not_found():
    repaired, events = [], []
    report = detect_broker_reconciliation(
        broker_trades=[_trade()], ledger_rows=[], broker_legs=_legs(1),
        canonical_legs=_legs(0), repair_fill=repaired.append, emit=events.append)
    assert report.hold is True
    assert report.repaired_fill_count == 1
    assert repaired[0]["execution_source"] == "BROKER_FILL"
    assert repaired[0]["trade_id"] == "broker-reconciled-b1769ea375a331bd"
    assert {event.code for event in events} == {
        "BROKER_FILL_MISSING_FROM_LEDGER", "RECONCILIATION_HOLD"}
    assert not any(event.code == "BROKER_NOT_FOUND" for event in events)


def test_ledger_fill_without_broker_trade_is_phantom():
    report = detect_broker_reconciliation(
        broker_trades=[], ledger_rows=[{
            "trade_id": "broker-reconciled-b1769ea375a331bd",
            "deal_id": "deal-far-1703", "instrument_code": "TMFJ6",
            "execution_source": "BROKER_FILL"}],
        broker_legs=_legs(1), canonical_legs=_legs(1))
    assert [event.code for event in report.events] == ["BROKER_FILL_PHANTOM"]


def test_hold_clear_allows_trail_evaluation_input():
    report = detect_broker_reconciliation(
        broker_trades=[_trade()], ledger_rows=[{
            "trade_id": "broker-reconciled-b1769ea375a331bd",
            "deal_id": "deal-far-1703", "instrument_code": "TMFJ6",
            "execution_source": "BROKER_FILL"}],
        broker_legs=_legs(1), canonical_legs=_legs(1))
    assert report.hold is False
    assert report.events == []
