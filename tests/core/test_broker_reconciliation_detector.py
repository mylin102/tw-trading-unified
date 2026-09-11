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


def test_working_broker_order_enters_hold_before_any_exit_action():
    report = detect_broker_reconciliation(
        broker_trades=[{"id": "mobile-near", "code": "TMFI6",
                        "status": "Working"}],
        ledger_rows=[], broker_legs=_legs(1), canonical_legs=_legs(1))
    assert report.hold is True
    assert report.entry_blocked is True
    assert any(event.code == "BROKER_WORKING_ORDER_HOLD"
               for event in report.events)


def test_broker_flat_local_holding_without_exit_fill_blocks_entry():
    report = detect_broker_reconciliation(
        broker_trades=[], ledger_rows=[], broker_legs=[],
        canonical_legs=_legs(1), trade_id="broker-reconciled-b1769ea375a331bd")
    assert report.hold is True
    assert report.entry_blocked is True
    assert any(event.code == "BROKER_FLAT_WITHOUT_EXIT_EVIDENCE"
               for event in report.events)


def test_broker_flat_local_holding_with_audited_exit_fill_can_reconcile():
    rows = [{"trade_id": "broker-reconciled-b1769ea375a331bd",
             "deal_id": "near-exit", "instrument_code": "TMFI6",
             "execution_source": "BROKER_FILL", "fill_type": "EXIT"}]
    report = detect_broker_reconciliation(
        broker_trades=[], ledger_rows=rows, broker_legs=[],
        canonical_legs=[], trade_id="broker-reconciled-b1769ea375a331bd")
    assert report.entry_blocked is False
    assert not any(event.code == "BROKER_FLAT_WITHOUT_EXIT_EVIDENCE"
                   for event in report.events)


def test_broker_evidence_unavailable_is_fail_closed_not_flat():
    report = detect_broker_reconciliation(
        broker_trades=None, ledger_rows=[], broker_legs=[], canonical_legs=[])
    assert report.hold is True
    assert report.entry_blocked is True
    assert any(event.code == "BROKER_EVIDENCE_UNAVAILABLE"
               for event in report.events)
