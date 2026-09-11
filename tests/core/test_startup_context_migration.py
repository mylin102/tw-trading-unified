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


def test_durable_fill_ledger_is_primary_and_survives_context_rewrite(tmp_path):
    from core.execution_context_state import persist_execution_context, read_execution_context
    ledger = tmp_path / "logs"
    ledger.mkdir()
    tid = "broker-reconciled-b1769ea375a331bd"
    (ledger / "mts_trade_fills.jsonl").write_text(
        __import__("json").dumps({"trade_id": tid, "execution_source": "BROKER_FILL",
            "instrument_code": "TMFI6", "side": "BUY", "quantity": 1, "price": 45876.0}) + "\n" +
        __import__("json").dumps({"trade_id": tid, "execution_source": "BROKER_FILL",
            "instrument_code": "TMFJ6", "side": "SELL", "quantity": 1, "price": 46059.0}) + "\n")
    persist_execution_context({"requested_mode": "live", "effective_mode": "live_quarantined",
        "live_order_allowed": False, "audit_reasons": ["BROKER_NOT_FLAT"],
        "state_namespace": "live", "exit_only_capability": None}, runtime_dir=str(tmp_path))
    ok, ctx = migrate_stale_live_context(str(tmp_path), _snapshot())
    assert ok and ctx["canonical_holding"]["trade_id"] == tid
    assert ctx["canonical_holding"]["source"] == "durable_broker_fill_ledger"
    # Model the monitor's normal immutable-context rewrite, then re-run the
    # post-startup evidence gate inputs: migration evidence must not vanish.
    from core.mode_transition import ExecutionContext
    rewritten = ExecutionContext(requested_mode="live", effective_mode="live_ready",
        live_order_allowed=True, state_namespace="live",
        canonical_holding=ctx["canonical_holding"])
    persist_execution_context(rewritten.to_dict(), runtime_dir=str(tmp_path))
    after = read_execution_context(runtime_dir=str(tmp_path))
    assert after["canonical_holding"]["trade_id"] == tid
    assert after["canonical_holding"]["source"] == "durable_broker_fill_ledger"


def test_flat_settled_reconciliation_clears_stale_holding_hold(tmp_path):
    from core.execution_context_state import persist_execution_context
    import json
    ledger = tmp_path / "logs"
    ledger.mkdir()
    tid = "broker-reconciled-b1769ea375a331bd"
    fills = [
        {"trade_id": tid, "execution_source": "BROKER_FILL", "instrument_code": "TMFI6",
         "side": "BUY", "quantity": 1, "price": 45876.0, "fill_type": "ENTRY"},
        {"trade_id": tid, "execution_source": "BROKER_FILL", "instrument_code": "TMFJ6",
         "side": "SELL", "quantity": 1, "price": 46059.0, "fill_type": "ENTRY"},
        {"trade_id": tid, "execution_source": "USER_MANUAL_BROKER_FILL", "instrument_code": "TMFI6",
         "side": "SELL", "quantity": 1, "price": 46341.0, "fill_type": "EXIT"},
        {"trade_id": tid, "execution_source": "BROKER_FILL", "instrument_code": "TMFJ6",
         "side": "BUY", "quantity": 1, "price": 46551.0, "fill_type": "RELEASE"},
    ]
    (ledger / "mts_trade_fills.jsonl").write_text("\n".join(json.dumps(x) for x in fills) + "\n")
    (tmp_path / "data" / "state").mkdir(parents=True)
    (tmp_path / "data" / "state" / "mts_position_state.json").write_text(json.dumps({
        "has_position": False, "state": "FLAT", "settlement_status": "SETTLED",
        "trade_id": tid,
    }))
    persist_execution_context({"requested_mode": "live", "effective_mode": "live_quarantined",
        "live_order_allowed": False, "audit_reasons": ["RECONCILIATION_HOLD"],
        "state_namespace": "live", "canonical_holding": {"status": "HOLD"}},
        runtime_dir=str(tmp_path))
    ok, ctx = migrate_stale_live_context(str(tmp_path), {
        "source": "live_broker", "fetch_status": {"capture": "OK"},
        "positions": [], "open_orders": [],
    })
    assert ok is True
    assert ctx["effective_mode"] == "live_ready"
    assert ctx["live_order_allowed"] is True
    assert ctx["canonical_holding"]["status"] != "HOLD"


def test_legacy_fill_without_instrument_identity_cannot_poison_current_trade(tmp_path):
    from core.execution_context_state import persist_execution_context
    import json
    ledger = tmp_path / "logs"
    ledger.mkdir()
    tid = "broker-reconciled-new"
    rows = [
        {"trade_id": "broker-reconciled-old", "execution_source": "BROKER_FILL",
         "instrument_code": None, "side": "BUY", "quantity": 1, "price": 46551.0},
        {"trade_id": tid, "execution_source": "BROKER_FILL", "instrument_code": "TMFI6",
         "side": "SELL", "quantity": 1, "price": 46288.0},
        {"trade_id": tid, "execution_source": "BROKER_FILL", "instrument_code": "TMFJ6",
         "side": "BUY", "quantity": 1, "price": 46451.0},
    ]
    (ledger / "mts_trade_fills.jsonl").write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    persist_execution_context({"requested_mode": "live", "effective_mode": "live_quarantined",
        "live_order_allowed": False, "state_namespace": "live"}, runtime_dir=str(tmp_path))
    ok, ctx = migrate_stale_live_context(str(tmp_path), _snapshot())
    assert ok is True
    assert ctx["canonical_holding"]["trade_id"] == tid
    assert {x["code"] for x in ctx["canonical_holding"]["canonical_legs"]} == {"TMFI6", "TMFJ6"}
