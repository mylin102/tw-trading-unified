"""Contract & regression tests for F1 (date-prefixed mts-auto trade_id) and F2 (close_all partial-fill guard).

2026-11-26 Gemini CLI: Covers T1-T5 against real FuturesMonitor callsites.
"""
from datetime import datetime
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.order_management.order import OrderSide, OrderStatus
from strategies.futures.monitor import FuturesMonitor


def _capture_auto_trade_id(ts: datetime) -> str:
    """Invoke the real _submit_mts_order_signal trade_id assignment path and capture trade_id."""
    mon = object.__new__(FuturesMonitor)
    mon.ticker = "TMF"
    mon.live_trading = False
    mon.dry_run = True
    mon.cfg = {"mts": {"enabled": True, "params": {}}}
    mon.contract = SimpleNamespace(code="TMFL6")
    mon.far_contract = SimpleNamespace(code="TMFA7")
    mon.paper_fill_sim = None
    mon.order_mgr = MagicMock()
    mon.market_data = {}
    mon._pending_lifecycle_orders = {}
    mon._mts_pending_fills = {}
    mon._execution_context = SimpleNamespace(requested_mode="paper", effective_mode="paper_active")
    mon._exit_only_authorization_ready = lambda *a, **kw: True
    mon._mts_block_entry_if_open_position = lambda *a, **kw: False
    mon._authorize_intent = lambda *a, **kw: (True, None, "")
    mon._capture_mts_position_baseline = lambda: []
    mon._submit_via_gateway = lambda order: False  # stop right after order creation

    captured = {}

    def _rec(event, **payload):
        if "trade_id" in payload and payload["trade_id"] and "trade_id" not in captured:
            captured["trade_id"] = payload["trade_id"]

    mon._append_mts_event = _rec
    sig = SimpleNamespace(action="SELL_NEAR_BUY_FAR", reason="TMF_SPREAD_WIDE")
    strat = SimpleNamespace(_trade_id=None, _has_position=False, _lifecycle="SUBMITTING")
    bar = {
        "near_close": 37249.0, "far_close": 37352.0, "spread": -103.0, "spread_z": 3.2,
        "bbo_near_bid": 37253.0, "bbo_near_ask": 37256.0,
        "bbo_far_bid": 37348.0, "bbo_far_ask": 37368.0,
        "bbo_near_age_ms": 10, "bbo_far_age_ms": 10,
        "bbo_near_state": "OK", "bbo_far_state": "OK",
    }
    FuturesMonitor._submit_mts_order_signal(mon, sig, strat, bar, ts)
    return captured["trade_id"]


def test_t1_same_hhmmss_different_dates_produce_distinct_trade_ids():
    """T1 (F1): Same HHMMSS.mmm on different dates (2026-10-15 vs 2026-11-25) must never collide."""
    tid_oct = _capture_auto_trade_id(datetime(2026, 10, 15, 18, 10, 2, 448000))
    tid_nov = _capture_auto_trade_id(datetime(2026, 11, 25, 18, 10, 2, 448000))

    assert tid_oct != tid_nov
    assert "20261015" in tid_oct
    assert "20261125" in tid_nov


def test_t2_same_date_same_hhmmss_different_milliseconds_produce_distinct_trade_ids():
    """T2 (F1): Same date & HHMMSS with different milliseconds must remain distinct."""
    tid_a = _capture_auto_trade_id(datetime(2026, 11, 25, 18, 10, 2, 448000))
    tid_b = _capture_auto_trade_id(datetime(2026, 11, 25, 18, 10, 2, 449000))

    assert tid_a != tid_b


def _make_close_all_monitor(tmp_path, *, has_position=True, released_leg=None, fill_symbols=()):
    """Build a real FuturesMonitor wired for _process_manual_trade_flag(action='close_all')."""
    state_path = tmp_path / "mts_position_state.json"
    state_json = {
        "has_position": has_position,
        "state": "HOLDING_SPREAD" if has_position else "FLAT",
        "near_side": "SHORT" if has_position else None,
        "far_side": "LONG" if has_position else None,
        "near_entry": 37249.0 if has_position else 0.0,
        "far_entry": 37352.0 if has_position else 0.0,
        "released_leg": released_leg,
        "trade_id": "mts-auto-20261125-181002-448" if has_position else None,
    }
    state_path.write_text(json.dumps(state_json))

    flag_path = tmp_path / "manual_trade.flag"
    flag_path.write_text(json.dumps({
        "action": "close_all", "command_id": "CMD-CLOSEALL-1", "created_at": time.time(),
    }))

    mon = object.__new__(FuturesMonitor)
    mon.ticker = "TMF"
    mon.dry_run = True
    mon.live_trading = False
    mon.manual_trade_flag_path = str(flag_path)
    mon.cfg = {"mts": {"enabled": True}}
    mon.contract = SimpleNamespace(code="TMFL6")
    mon.far_contract = SimpleNamespace(code="TMFA7")
    mon.market_data = {
        "TMF_NEAR": {"close": 38620.0},
        "TMF_FAR": {"close": 38710.0},
    }
    mon._tick_bars_deque = []
    mon._pending_lifecycle_orders = {}
    mon._processed_flag_ids = set()
    mon._flag_retry_count = 0
    mon._manual_trade_status = "READY"
    mon._lifecycle_generation = 0
    mon._emergency_reset_at = None
    mon._emergency_cmd = None
    mon.trader = MagicMock(position=1 if has_position else 0)
    mon._write_manual_command_status = MagicMock()
    mon._cancel_all_pending_orders = MagicMock()
    mon._save_orders_file_wrapper = MagicMock()

    events = []
    mon._append_mts_event = lambda ev, **kw: events.append((ev, kw))

    strat = SimpleNamespace(
        _has_position=has_position,
        _lifecycle="OPEN" if has_position else "FLAT",
        _near_side="SHORT" if has_position else None,
        _far_side="LONG" if has_position else None,
        _released_leg=released_leg,
        _trade_id="mts-auto-20261125-181002-448" if has_position else None,
        _reset=MagicMock(),
    )
    mon._registry = {"tmf_spread": strat}

    created_orders = []

    def _create_order(**kw):
        o = SimpleNamespace(
            order_id=f"ORD-EMERG-{len(created_orders)+1}",
            intent_id=f"intent-emerg-{len(created_orders)+1}",
            symbol=kw["symbol"],
            side=kw["side"],
            order_type=kw["order_type"],
            quantity=kw["quantity"],
            strategy=kw["strategy"],
            status=OrderStatus.PENDING_SUBMIT,
        )
        created_orders.append(o)
        return o

    def _submit(o):
        o.status = OrderStatus.FILLED if o.symbol in fill_symbols else OrderStatus.SUBMITTED
        if o.status == OrderStatus.FILLED and mon._emergency_cmd:
            mon._emergency_cmd["filled_ids"].add(o.order_id)
        return True

    mon.order_mgr = SimpleNamespace(
        active_orders={},
        reindex_orders=lambda: None,
        create_order=_create_order,
        submit=_submit,
    )
    mon.paper_fill_sim = SimpleNamespace(register=lambda o: None, process_tick=lambda t: None)
    mon._make_synthetic_tick = lambda px, ts, symbol="": SimpleNamespace(close=px, code=symbol, symbol=symbol)
    return mon, strat, state_path, created_orders, events


def test_t3_close_all_both_legs_filled_writes_flat_state(tmp_path, monkeypatch):
    """T3 (F2): Both legs held -> 2 close orders submitted, both FILLED -> state written FLAT."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    mon, strat, state_path, created, events = _make_close_all_monitor(
        tmp_path, has_position=True, released_leg=None, fill_symbols={"TMFL6", "TMFA7"},
    )
    state_writes = []
    with patch("strategies.futures.monitor._mts_position_state_path", return_value=state_path), \
         patch("strategies.plugins.futures.active.tmf_spread.settle_mts_trade"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state",
               side_effect=lambda **kw: state_writes.append(kw)):
        mon._process_manual_trade_flag()

    assert len(created) == 2
    assert [(o.symbol, o.side) for o in created] == [("TMFL6", OrderSide.BUY), ("TMFA7", OrderSide.SELL)]
    assert strat._reset.called
    assert any(w.get("has_position") is False and w.get("action") == "FLAT" for w in state_writes)
    assert not any(ev == "MANUAL_CLOSE_ALL_PARTIAL" for ev, _ in events)


def test_t4_close_all_one_leg_unfilled_keeps_non_flat_and_emits_partial_event(tmp_path, monkeypatch):
    """T4 (F2): Near leg fills but far leg stays unfilled -> MUST NOT reset/write FLAT, must emit PARTIAL."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    mon, strat, state_path, created, events = _make_close_all_monitor(
        tmp_path, has_position=True, released_leg=None, fill_symbols={"TMFL6"},
    )
    state_writes = []
    with patch("strategies.futures.monitor._mts_position_state_path", return_value=state_path), \
         patch("strategies.plugins.futures.active.tmf_spread.settle_mts_trade") as mock_settle, \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state",
               side_effect=lambda **kw: state_writes.append(kw)):
        mon._process_manual_trade_flag()

    assert len(created) == 2
    assert not strat._reset.called
    assert not mock_settle.called
    assert not any(w.get("has_position") is False for w in state_writes)
    assert strat._lifecycle == "OPEN"
    partial_evs = [p for ev, p in events if ev == "MANUAL_CLOSE_ALL_PARTIAL"]
    assert len(partial_evs) == 1
    assert partial_evs[0]["filled_legs"] == ["near"]
    assert partial_evs[0]["unfilled_legs"] == ["far"]


def test_t5_close_all_already_flat_submits_zero_orders(tmp_path, monkeypatch):
    """T5 (F2): Already FLAT -> submits zero orders, completes cleanly without PARTIAL event."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    mon, strat, state_path, created, events = _make_close_all_monitor(
        tmp_path, has_position=False, released_leg=None, fill_symbols=set(),
    )
    with patch("strategies.futures.monitor._mts_position_state_path", return_value=state_path), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        mon._process_manual_trade_flag()

    assert created == []
    assert not any(ev == "MANUAL_CLOSE_ALL_PARTIAL" for ev, _ in events)
    assert any(
        len(c.args) >= 2 and c.args[1] == "COMPLETED"
        for c in mon._write_manual_command_status.call_args_list
    )


def _write_fills_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_t6_close_all_recovers_open_far_leg_from_ledger_when_state_is_flat(tmp_path, monkeypatch):
    """T6 (F2 expanded): state=FLAT (prematurely zeroed) but fills ledger still has open FAR LONG -> close_all submits TMFA7 SELL."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    fills_path = tmp_path / "mts_trade_fills.jsonl"
    _write_fills_jsonl(fills_path, [
        {"timestamp": "2026-11-25T18:10:02.753", "trade_id": "mts-auto-20261125-181002-448",
         "leg": "NEAR", "side": "SHORT", "qty": 1, "price": 37249.0, "fill_type": "ENTRY", "mode": "paper"},
        {"timestamp": "2026-11-25T18:10:02.766", "trade_id": "mts-auto-20261125-181002-448",
         "leg": "FAR", "side": "LONG", "qty": 1, "price": 37352.0, "fill_type": "ENTRY", "mode": "paper"},
        {"timestamp": "2026-11-26T10:05:26.320", "trade_id": "mts-auto-20261125-181002-448",
         "leg": "NEAR", "side": "BUY", "qty": 1, "price": 38620.0, "fill_type": "EXIT", "mode": "paper"},
    ])
    monkeypatch.setenv("MTS_FILL_LOG_PATH", str(fills_path))

    mon, strat, state_path, created, events = _make_close_all_monitor(
        tmp_path, has_position=False, released_leg=None, fill_symbols={"TMFA7"},
    )
    with patch("strategies.futures.monitor._mts_position_state_path", return_value=state_path), \
         patch("strategies.plugins.futures.active.tmf_spread.settle_mts_trade"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        mon._process_manual_trade_flag()

    assert [(o.symbol, o.side) for o in created] == [("TMFA7", OrderSide.SELL)]


def test_t7_close_all_fails_closed_when_state_flat_but_ledger_has_anomaly(tmp_path, monkeypatch):
    """T7 (F2 expanded): state=FLAT, latest trade FLAT, older trade unclosed (anomaly) -> MUST NOT report COMPLETED or write FLAT."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    fills_path = tmp_path / "mts_trade_fills.jsonl"
    _write_fills_jsonl(fills_path, [
        {"timestamp": "2026-11-25T18:10:02.753", "trade_id": "mts-auto-20261125-181002-448",
         "leg": "FAR", "side": "LONG", "qty": 1, "price": 37352.0, "fill_type": "ENTRY", "mode": "paper"},
        {"timestamp": "2026-11-26T09:00:01.000", "trade_id": "mts-auto-20261126-090001-001",
         "leg": "NEAR", "side": "SHORT", "qty": 1, "price": 38500.0, "fill_type": "ENTRY", "mode": "paper"},
        {"timestamp": "2026-11-26T09:05:01.000", "trade_id": "mts-auto-20261126-090001-001",
         "leg": "NEAR", "side": "BUY", "qty": 1, "price": 38490.0, "fill_type": "EXIT", "mode": "paper"},
    ])
    monkeypatch.setenv("MTS_FILL_LOG_PATH", str(fills_path))

    mon, strat, state_path, created, events = _make_close_all_monitor(
        tmp_path, has_position=False, released_leg=None, fill_symbols=set(),
    )
    state_writes = []
    with patch("strategies.futures.monitor._mts_position_state_path", return_value=state_path), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state",
               side_effect=lambda **kw: state_writes.append(kw)):
        mon._process_manual_trade_flag()

    assert created == []
    assert state_writes == []
    assert not any(
        len(c.args) >= 2 and c.args[1] == "COMPLETED"
        for c in mon._write_manual_command_status.call_args_list
    )
    assert any(
        len(c.args) >= 3 and c.args[1] == "FAILED" and "UNKNOWN" in c.args[2]
        for c in mon._write_manual_command_status.call_args_list
    )
    assert any(ev == "MANUAL_CLOSE_ALL_LEDGER_DIVERGENCE" for ev, _ in events)


def _load_dashboard_funcs(*names):
    """Extract target function defs from ui/dashboard.py without executing top-level Streamlit code."""
    import ast
    from pathlib import Path
    from typing import Optional

    dash_path = Path(__file__).resolve().parents[2] / "ui" / "dashboard.py"
    src = dash_path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(dash_path))
    wanted = set(names)
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert len(selected) == len(wanted), f"Missing functions in ui/dashboard.py: {wanted - {n.name for n in selected}}"

    calls = {"info": [], "warning": [], "caption": []}
    stub_st = SimpleNamespace(
        info=lambda msg: calls["info"].append(str(msg)),
        warning=lambda msg: calls["warning"].append(str(msg)),
        caption=lambda msg: calls["caption"].append(str(msg)),
    )
    ns = {
        "Optional": Optional,
        "st": stub_st,
        "_load_mts_trade_fills": lambda: [],
        "_mts_leg_bracket_config": lambda cfg: {"enabled": True, "take_profit_pts": 15.0, "stop_loss_pts": 30.0},
    }
    mod = ast.Module(body=selected, type_ignores=[])
    exec(compile(mod, str(dash_path), "exec"), ns)
    return ns, calls


def test_t8_dashboard_detects_state_flat_vs_ledger_open():
    """T8 (F3): state=FLAT with open FAR leg in fills ledger -> diverged=True, display_status=UNKNOWN."""
    ns, _ = _load_dashboard_funcs("check_mts_state_ledger_divergence")
    fills = [
        {"timestamp": "2026-11-25T18:10:02", "trade_id": "mts-auto-20261125-181002-448",
         "leg": "FAR", "side": "LONG", "qty": 1, "price": 37352.0, "fill_type": "ENTRY", "mode": "paper"},
    ]
    res = ns["check_mts_state_ledger_divergence"]({"has_position": False}, fills=fills)
    assert res["diverged"] is True
    assert res["display_status"] == "UNKNOWN"
    assert "LEDGER_OPEN_STATE_FLAT" in res["reason"]


def test_t9_dashboard_detects_state_flat_vs_ledger_anomaly():
    """T9 (F3): state=FLAT with unclosed older trade in ledger anomalies -> diverged=True, display_status=UNKNOWN."""
    ns, _ = _load_dashboard_funcs("check_mts_state_ledger_divergence")
    fills = [
        {"timestamp": "2026-11-25T18:10:02", "trade_id": "mts-auto-20261125-181002-448",
         "leg": "FAR", "side": "LONG", "qty": 1, "price": 37352.0, "fill_type": "ENTRY", "mode": "paper"},
        {"timestamp": "2026-11-26T09:00:01", "trade_id": "mts-auto-20261126-090001-001",
         "leg": "NEAR", "side": "SHORT", "qty": 1, "price": 38500.0, "fill_type": "ENTRY", "mode": "paper"},
        {"timestamp": "2026-11-26T09:05:01", "trade_id": "mts-auto-20261126-090001-001",
         "leg": "NEAR", "side": "BUY", "qty": 1, "price": 38490.0, "fill_type": "EXIT", "mode": "paper"},
    ]
    res = ns["check_mts_state_ledger_divergence"]({"has_position": False}, fills=fills)
    assert res["diverged"] is True
    assert res["display_status"] == "UNKNOWN"
    assert "LEDGER_ANOMALY_STATE_FLAT" in res["reason"]


def test_t10_dashboard_panels_show_unknown_never_flat_on_divergence():
    """T10 (F3): Both exit panel and hvwap panel display UNKNOWN / 待對帳 and NEVER FLAT when diverged."""
    ns, calls = _load_dashboard_funcs(
        "check_mts_state_ledger_divergence",
        "render_mts_spread_exit_panel",
        "_hvwap_latest",
        "_hvwap_panel_payload",
        "_hvwap_status_color",
        "_hvwap_panel_markdown",
    )
    fills = [
        {"timestamp": "2026-11-25T18:10:02", "trade_id": "mts-auto-20261125-181002-448",
         "leg": "FAR", "side": "LONG", "qty": 1, "price": 37352.0, "fill_type": "ENTRY", "mode": "paper"},
    ]
    ns["_load_mts_trade_fills"] = lambda: fills
    ns["_hvwap_execution_mode"] = lambda: {
        "mode_label": "PAPER", "hvwap_mode": "shadow", "live": False,
        "effective_mode": "paper_active", "live_order_allowed": False,
        "release_wiring_active": False, "detail": "paper shadow",
    }
    ns["render_mts_spread_exit_panel"]({"has_position": False}, {}, fills=fills)
    assert any("UNKNOWN / 待對帳" in w for w in calls["warning"])
    assert not any("FLAT" in c for c in calls["caption"])

    payload = ns["_hvwap_panel_payload"](
        {"has_position": False, "state": "HEARTBEAT", "reason": "mts_sync_status"},
        [],
    )
    assert payload["state"]["ledger_divergence"]["diverged"] is True
    html = ns["_hvwap_panel_markdown"](payload)
    assert "UNKNOWN / 待對帳" in html
    assert "持倉: FLAT" not in html


def test_t11_dashboard_clean_flat_shows_flat():
    """T11 (F3): Clean state=FLAT + clean ledger FLAT -> diverged=False and displays FLAT."""
    ns, calls = _load_dashboard_funcs(
        "check_mts_state_ledger_divergence",
        "render_mts_spread_exit_panel",
    )
    fills = [
        {"timestamp": "2026-11-26T09:00:01", "trade_id": "mts-auto-20261126-090001-001",
         "leg": "NEAR", "side": "SHORT", "qty": 1, "price": 38500.0, "fill_type": "ENTRY", "mode": "paper"},
        {"timestamp": "2026-11-26T09:05:01", "trade_id": "mts-auto-20261126-090001-001",
         "leg": "NEAR", "side": "BUY", "qty": 1, "price": 38490.0, "fill_type": "EXIT", "mode": "paper"},
    ]
    res = ns["check_mts_state_ledger_divergence"]({"has_position": False}, fills=fills)
    assert res["diverged"] is False
    assert res["display_status"] == "FLAT"

    ns["render_mts_spread_exit_panel"]({"has_position": False}, {}, fills=fills)
    assert calls["warning"] == []
    assert any("FLAT" in c for c in calls["caption"])
