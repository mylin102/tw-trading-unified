"""2026-10-02 Codex: real MTS restore/write/read boundaries must not mix modes."""
import ast
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import runtime_paths as rp
from core.order_management.order import Order, OrderSide, OrderStatus, OrderType
from strategies.futures import monitor as fm
from strategies.plugins.futures.active import tmf_spread as tmf
from squeeze_futures.data import data_storage as storage


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    # Emulate the actual Mini layout, including PM2's legacy canonical override.
    rt = tmp_path / "runtime"
    rt.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NODE_ENV", "production")
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("MTS_STATE_PATH", str(rt / "data/state/mts_position_state.json"))
    monkeypatch.setenv("TRADING_SAFETY_STATUS_PATH", str(rt / "safety.json"))
    monkeypatch.setenv("MTS_SESSION_EVENT_LOG_PATH", str(rt / "session-events.jsonl"))
    monkeypatch.setenv("MTS_BACKTEST", "0")
    for key in ("MTS_FILL_LOG_PATH", "MTS_EVENT_LOG_PATH", "MTS_RECOVERY_AUDIT_LOG_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(rp, "_MTS_DATA_MODE", None, raising=False)
    for name, value in {
        "_MTS_STATE_FILE": str(rt / "data/state/mts_position_state.json"),
        "_MTS_FILL_LOG": str(rt / "logs/mts_trade_fills.jsonl"),
        "_MTS_EVENT_LOG": str(rt / "logs/mts_spread_events.jsonl"),
        "_MTS_RECOVERY_AUDIT_LOG": str(rt / "logs/mts_recovery_audit.jsonl"),
        "_SETTLEMENT_FAILURE_LATCH": str(rt / "logs/runtime/settlement_failure_latch.json"),
        "_CRITICAL_FAILURE_SENTINEL": str(rt / "logs/runtime/critical.sentinel"),
    }.items():
        monkeypatch.setattr(tmf, name, value)
    monkeypatch.setattr(fm, "MTS_POSITION_STATE_PATH", rt / "data/state/mts_position_state.json")
    monkeypatch.setattr(fm._thread_local, "state_path", None, raising=False)
    monkeypatch.setattr(storage, "_storage", None)
    loader = SimpleNamespace(load_latest_csv=lambda *_: True, status=lambda: "offline fixture")
    monkeypatch.setattr(fm, "get_spread_loader", lambda: loader)
    # No broker object is created/logged in; only injected API data is available.
    yield rt


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _context(rt, mode):
    _write(rt / "execution_context.json", {
        "requested_mode": mode, "effective_mode": "paper_active" if mode == "paper" else "live_quarantined",
        "live_order_allowed": False, "state_namespace": mode,
    })


def _historical_live(rt):
    _context(rt, "live")
    fills = rt / "logs/mts_trade_fills.jsonl"
    fills.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"trade_id": "broker-lifecycle-2036fac0a7fc4f2c", "mode": "live",
             "execution_source": "BROKER_FILL", "fill_type": "ENTRY", "ticker": "TMF",
             "leg": leg, "side": side, "qty": 1, "price": price, "contract": code}
            for leg, side, price, code in (("NEAR", "LONG", 46288.0, "TMFJ6"),
                                           ("FAR", "SHORT", 46451.0, "TMFK6"))]
    fills.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    state = rp.stamp_runtime_state({"has_position": True, "trade_id": rows[0]["trade_id"],
                                   "near_entry": 46288.0, "far_entry": 46451.0,
                                   "near_side": "LONG", "far_side": "SHORT"})
    _write(rt / "data/state/mts_position_state.json", state)
    order = Order("TMFJ6", OrderSide.BUY, OrderType.MKP, 1,
                  order_id="LIVE-HISTORICAL", strategy="MTS_ENTRY")
    order.submit("broker-old")
    today = datetime.now().strftime("%Y%m%d")
    _write(rt / f"exports/trades/TMF_{today}_orders.json", [order.to_dict()])
    return {path: path.read_bytes() for path in rt.rglob("*") if path.is_file()
            and path.name != "execution_context.json"}


def _monitor(rt, monkeypatch):
    config = rt.parent / "paper.yaml"
    config.write_text("ticker: TMF\nlive_trading: false\nmonitoring:\n  use_order_manager: true\n")
    return fm.FuturesMonitor(api=SimpleNamespace(), config_path=str(config), dry_run=True)


def _unchanged(originals):
    assert all(path.read_bytes() == contents for path, contents in originals.items())


def test_real_startup_binds_paper_before_shared_ledger_bootstrap(isolated_runtime, monkeypatch):
    rt = isolated_runtime
    originals = _historical_live(rt)
    mon = _monitor(rt, monkeypatch)
    assert mon._state_path == rt / "paper/data/state/mts_position_state.json"
    assert mon._ledger_projection._path == str(rt / "paper/logs/mts_trade_fills.jsonl")
    assert not mon._mts_has_open_position_from_fills()
    assert mon.order_mgr.get_pending() == []
    assert mon.order_mgr.get_completed() == []
    assert mon._execution_context.requested_mode.value == "paper"
    assert mon.order_mgr.broker_adapter is None
    _unchanged(originals)


def test_real_fill_writer_and_restart_do_not_restore_live_trade(isolated_runtime, monkeypatch):
    rt = isolated_runtime
    originals = _historical_live(rt)
    mon = _monitor(rt, monkeypatch)
    for leg, side, price, code in (("NEAR", "LONG", 46000.0, "TMFJ6"),
                                   ("FAR", "SHORT", 46100.0, "TMFK6")):
        tmf._append_fill("TMF", code, leg, side, 1, price, "ENTRY", "paper-own", durable=True)
    path = rt / "paper/logs/mts_trade_fills.jsonl"
    assert path.exists()
    assert {row["trade_id"] for row in map(json.loads, path.read_text().splitlines())} == {"paper-own"}
    restarted = _monitor(rt, monkeypatch)
    assert restarted._mts_has_open_position_from_fills()
    assert restarted._ledger_projection.snapshot().trade_id == "paper-own"
    _unchanged(originals)


def test_real_order_export_and_restore_are_mode_scoped(isolated_runtime, monkeypatch):
    rt = isolated_runtime
    originals = _historical_live(rt)
    mon = _monitor(rt, monkeypatch)
    order = Order("TMFJ6", OrderSide.BUY, OrderType.MKP, 1,
                  order_id="PAPER-OWN", strategy="MTS_ENTRY")
    order.submit("paper-sim")
    mon.order_mgr.active_orders[order.order_id] = order
    mon._save_orders_file_wrapper()
    today = datetime.now().strftime("%Y%m%d")
    path = rt / f"paper/exports/trades/TMF_{today}_orders.json"
    assert path.exists()
    assert [row["order_id"] for row in json.loads(path.read_text())] == ["PAPER-OWN"]
    restarted = _monitor(rt, monkeypatch)
    assert [order.order_id for order in restarted.order_mgr.get_pending()] == ["PAPER-OWN"]
    _unchanged(originals)


def test_real_confirmed_deal_trade_storage_writes_only_paper(isolated_runtime, monkeypatch):
    rt = isolated_runtime
    originals = _historical_live(rt)
    mon = _monitor(rt, monkeypatch)
    mon._pending_lifecycle_orders["PAPER-DEAL"] = {"signal": "BUY", "reason": "offline test",
                                                   "stop_loss": 45900.0, "generation": 0}
    event = SimpleNamespace(order_id="PAPER-DEAL", fill_price=46000.0, fill_qty=1,
                            deal_id="paper-receipt", symbol="TMFJ6", side=OrderSide.BUY,
                            status=OrderStatus.FILLED, timestamp=datetime.now())
    assert mon._apply_confirmed_futures_deal(event)
    saved = storage.get_storage("TMF")
    saved._flush_buffer()
    assert saved.trade_dir == rt / "paper/exports/trades"
    assert saved.market_dir == rt / "paper/logs/market_data"
    assert saved.trade_file.exists()
    assert {row["type"] for row in json.loads(saved.trade_file.read_text())} >= {"BUY"}
    assert list(saved.trade_dir.glob("*_trades.csv"))
    assert list(saved.market_dir.glob("*_trades.csv"))
    _unchanged(originals)


def test_real_state_intent_event_and_latch_writers_are_mode_scoped(isolated_runtime, monkeypatch):
    rt = isolated_runtime
    originals = _historical_live(rt)
    mon = _monitor(rt, monkeypatch)
    tmf._write_mts_state(has_position=True, action="ENTRY_FILLED", reason="offline test",
                         near_entry=46000.0, far_entry=46100.0, near_side="LONG", far_side="SHORT",
                         trade_id="paper-own", ticker="TMF")
    path = rt / "paper/data/state/mts_position_state.json"
    assert path.exists()
    assert json.loads(path.read_text())["trade_id"] == "paper-own"
    mon._append_mts_event("PAPER_TEST", trade_id="paper-own")
    assert (rt / "paper/logs/mts_spread_events.jsonl").exists()
    assert fm._mts_intent_log_dir() == str(rt / "paper/logs")
    assert tmf._exit_intent_log().log_dir == str(rt / "paper/logs")
    tmf._write_settlement_failure_latch(trade_id="paper-own", execution_id="paper-exit")
    assert (rt / "paper/logs/runtime/settlement_failure_latch.json").exists()
    assert tmf._load_settlement_failure_latch()["trade_id"] == "paper-own"
    _unchanged(originals)


def _actual_ui_nodes(relative, names, namespace):
    # Execute actual production source nodes, not reimplemented path logic or a string assertion.
    tree = ast.parse((Path(__file__).resolve().parents[2] / relative).read_text())
    nodes = [node for node in ast.walk(tree) if
             (isinstance(node, ast.FunctionDef) and node.name in names) or
             (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names
                                                  for t in node.targets))]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), relative, "exec"), namespace)
    return namespace


@pytest.mark.parametrize("relative,names", [
    ("ui/pages/02_positions_orders.py", {"pos_file", "fills_file"}),
    ("ui/pages/03_trade_review.py", {"fills_path", "events_path"}),
    ("ui/dashboard.py", {"_read_canonical_mts_state", "_fills_path", "_events_path", "FUTURES_TRADES"}),
])
def test_actual_ui_read_sites_follow_runtime_not_stale_ui_env(isolated_runtime, monkeypatch, relative, names):
    rt = isolated_runtime
    _historical_live(rt)
    _context(rt, "paper")
    monkeypatch.setenv("TRADING_FUTURES_MODE", "live")
    state = rp.stamp_runtime_state({"has_position": False, "trade_id": "paper-ui"})
    _write(rt / "paper/data/state/mts_position_state.json", state)
    ns = {"ROOT": rt.parent, "Path": Path, "os": __import__("os"), "json": json,
          "runtime_path": rp.runtime_path, "mts_position_state_path": rp.mts_position_state_path,
          "validate_runtime_state": rp.validate_runtime_state}
    if hasattr(rp, "mts_runtime_path"):
        ns["mts_runtime_path"] = rp.mts_runtime_path
    _actual_ui_nodes(relative, names, ns)
    for name in names - {"_read_canonical_mts_state"}:
        assert Path(ns[name]).is_relative_to(rt / "paper")
    if "_read_canonical_mts_state" in names:
        actual, status, path = ns["_read_canonical_mts_state"]()
        assert status == "OK" and actual["trade_id"] == "paper-ui"


def test_live_paths_remain_legacy_and_stock_options_are_unchanged(isolated_runtime, monkeypatch):
    rt = isolated_runtime
    _context(rt, "live")
    assert rp.mts_position_state_path() == str(rt / "data/state/mts_position_state.json")
    assert tmf._fills_ledger_path() == str(rt / "logs/mts_trade_fills.jsonl")
    assert rp.runtime_path("exports", "trades", "OPTIONS_orders.json") == str(rt / "exports/trades/OPTIONS_orders.json")
    assert rp.runtime_logs("stocks") == str(rt / "logs/stocks")
