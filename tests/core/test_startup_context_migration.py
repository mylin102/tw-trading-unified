from core.reconciliation.startup_migration import migrate_stale_live_context


def _cap():
    return {"legs": [
        {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1, "avg_cost": 45876.0},
        {"symbol": "TMFJ6", "side": "sell", "remaining_qty": 1, "avg_cost": 46059.0},
    ]}


def _snapshot(cost=45876.0):
    return {"source": "live_broker", "fetch_status": {"capture": "OK"},
            "positions": [
                {"account": "futures", "code": "TMFI6", "quantity": 1, "direction": "Action.Buy", "avg_cost": cost},
                {"account": "futures", "code": "TMFJ6", "quantity": 1, "direction": "Action.Sell", "avg_cost": 46059.0},
            ], "open_orders": []}


def test_stale_context_matching_broker_migrates_to_normal_live(tmp_path):
    from core.execution_context_state import persist_execution_context, read_execution_context
    persist_execution_context({"requested_mode": "live", "effective_mode": "live_quarantined",
        "live_order_allowed": False, "audit_reasons": ["BROKER_NOT_FLAT"],
        "account_id_hash": "a", "session_id": "s", "config_hash": "c",
        "state_namespace": "live", "exit_only_capability": _cap()}, runtime_dir=str(tmp_path))
    ok, ctx = migrate_stale_live_context(str(tmp_path), _snapshot())
    assert ok and ctx["effective_mode"] == "live_ready"
    assert ctx["live_order_allowed"] is True
    assert ctx["exit_only_capability"] is None
    assert ctx["canonical_holding"]["raw_broker_legs"]


def test_stale_context_mismatch_is_hold_and_no_live_authority(tmp_path):
    from core.execution_context_state import persist_execution_context
    persist_execution_context({"requested_mode": "live", "effective_mode": "live_quarantined",
        "live_order_allowed": False, "audit_reasons": ["BROKER_NOT_FLAT"],
        "account_id_hash": "a", "session_id": "s", "config_hash": "c",
        "state_namespace": "live", "exit_only_capability": _cap()}, runtime_dir=str(tmp_path))
    ok, ctx = migrate_stale_live_context(str(tmp_path), _snapshot(45870.0))
    assert ok is False and ctx["effective_mode"] == "live_quarantined"
    assert ctx["audit_reasons"][0] == "RECONCILIATION_HOLD"
    assert ctx["live_order_allowed"] is False
