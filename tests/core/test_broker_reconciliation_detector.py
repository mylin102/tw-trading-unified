import json

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


def test_terminal_trade_without_deal_identity_is_evidence_unavailable():
    report = detect_broker_reconciliation(
        broker_trades=[{"id": "broker-order", "status": "Filled",
                        "deals": [{"price": 46288, "quantity": 1}]}],
        ledger_rows=[], broker_legs=[], canonical_legs=[])
    assert report.hold is True
    assert report.entry_blocked is True
    assert [e.code for e in report.events] == ["BROKER_EVIDENCE_UNAVAILABLE"]


def test_l3_va0pg_freezes_captures_and_attributes_release_premise(tmp_path):
    report = detect_broker_reconciliation(
        broker_trades=[], ledger_rows=[], broker_legs=[],
        canonical_legs=[{"code": "va0pG", "side": "LONG", "qty": 1, "avg_cost": 10}],
        event_window=[{"event": "ORDER_INTENT", "action": "RELEASE", "code": "va0pG"}],
        diagnostic_report_dir=tmp_path,
    )

    assert report.hold is True
    assert report.send_orders_frozen is True
    assert report.manual_release_required is True
    assert report.order_submission_allowed is False
    assert report.attribution["code"] == "PREMISE_VIOLATION_RELEASE"
    assert report.events[0].diagnostic_report_path == report.diagnostic_report_path
    assert report.events[0].evidence_hash == report.evidence["evidence_hash"]
    saved = json.loads(open(report.diagnostic_report_path, encoding="utf-8").read())
    assert saved["disposition"] == "FROZEN_PENDING_HUMAN_CONFIRMATION"
    assert saved["facts"][0].startswith("[observed]")
    assert saved["attribution"]["conclusion"].startswith("[inferred]")


def test_l3_unknown_is_unclassified_with_full_evidence(tmp_path):
    report = detect_broker_reconciliation(
        broker_trades=[], ledger_rows=[{"tail": 1}],
        broker_legs=[{"code": "A", "side": "LONG", "qty": 1, "avg_cost": 1}],
        canonical_legs=[{"code": "B", "side": "LONG", "qty": 1, "avg_cost": 1}],
        internal_flags={"flag": "x"}, event_window=[{"event": "ODD_UNKNOWN"}],
        diagnostic_report_dir=tmp_path,
    )

    assert report.attribution["code"] == "UNCLASSIFIED"
    assert report.evidence["sections"]["ledger_tail"] == [{"tail": 1}]
    assert report.evidence["section_hashes"]["broker_legs"]
    assert report.evidence["evidence_hash"]


def test_l3_duplicate_divergence_does_not_write_duplicate_report(tmp_path):
    kwargs = dict(
        broker_trades=[], ledger_rows=[], broker_legs=[],
        canonical_legs=[{"code": "va0pG", "side": "LONG", "qty": 1, "avg_cost": 10}],
        event_window=[{"event": "ORDER_INTENT", "action": "RELEASE", "code": "va0pG"}],
        diagnostic_report_dir=tmp_path,
    )
    first = detect_broker_reconciliation(**kwargs)
    second = detect_broker_reconciliation(**kwargs)
    assert first.diagnostic_deduplicated is False
    assert second.diagnostic_deduplicated is True
    assert len(list(tmp_path.glob("divergence-*.json"))) == 1


def test_l3_mutation_guard_attribution_is_evidence_dependent(tmp_path):
    premise = detect_broker_reconciliation(
        broker_trades=[], ledger_rows=[], broker_legs=[],
        canonical_legs=[{"code": "va0pG", "side": "LONG", "qty": 1, "avg_cost": 10}],
        event_window=[{"event": "ORDER_INTENT", "action": "RELEASE", "code": "va0pG"}],
        diagnostic_report_dir=tmp_path / "premise",
    )
    unknown = detect_broker_reconciliation(
        broker_trades=[], ledger_rows=[],
        broker_legs=[{"code": "A", "side": "LONG", "qty": 1, "avg_cost": 1}],
        canonical_legs=[{"code": "B", "side": "LONG", "qty": 1, "avg_cost": 1}],
        event_window=[{"event": "ODD_UNKNOWN"}],
        diagnostic_report_dir=tmp_path / "unknown",
    )
    assert premise.attribution["code"] != unknown.attribution["code"]
    assert premise.attribution["code"] == "PREMISE_VIOLATION_RELEASE"
    assert unknown.attribution["code"] == "UNCLASSIFIED"
