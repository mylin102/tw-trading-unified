"""Regression tests: MANUAL_TRADE action='spread' must fail-closed when tmf_spread._has_position is True.

2026-11-25 Gemini CLI: Locks both the entry gate (monitor.py:15362) and the
in-branch fail-closed guard at monitor.py:15753 so manual spread entry is
rejected whenever tmf_spread holds a position, even if trader.position == 0.
"""
import json
from types import SimpleNamespace

from strategies.futures.monitor import FuturesMonitor


def _make_manual_trade_monitor(tmp_path, strat, submitted):
    flag_path = tmp_path / "manual_trade.flag"
    flag_path.write_text(json.dumps({"action": "spread", "side": "SELL_NEAR_BUY_FAR"}))

    mon = object.__new__(FuturesMonitor)
    mon.manual_trade_flag_path = str(flag_path)
    mon.cfg = {"mts": {"enabled": True}}
    mon.ticker = "TMF"
    mon.dry_run = True
    mon.live_trading = False
    mon.trader = SimpleNamespace(position=0, balance=100000.0)
    mon._registry = {"tmf_spread": strat}
    mon._processed_flag_ids = set()
    mon._flag_retry_count = 0
    mon._current_flag_id = None
    mon._manual_trade_status = "READY"
    mon.market_data = {}
    mon._far_current_bar = {"close": 46000.0}
    mon.EXEC = {"margin_per_lot": 18000.0}
    mon.contract = SimpleNamespace(code="TMFL6")
    mon.far_contract = SimpleNamespace(code="TMFA7")
    mon._resolve_entry_price = lambda _flag: (45700.0, "DRY_RUN")
    mon._check_entry_quote_freshness = lambda **kw: (True, {})
    mon._authorize_intent = lambda *a, **kw: (True, None, "")
    mon._append_mts_event = lambda *a, **kw: None
    mon._mts_block_entry_if_open_position = lambda *a, **kw: False
    mon._pending_lifecycle_orders = {}
    mon._mts_pending_fills = {}
    mon.paper_fill_sim = None
    mon._submit_via_gateway = lambda order: (submitted.append(order), True)[1]
    mon.order_mgr = SimpleNamespace(
        create_order=lambda **kw: SimpleNamespace(
            order_id=f"ORD-{len(submitted)+1}",
            intent_id="intent-1",
            symbol=kw["symbol"],
            side=kw["side"],
            order_type=kw["order_type"],
            price=0.0,
            quantity=kw["quantity"],
        )
    )
    return mon


def test_manual_spread_entry_blocked_when_mts_has_position(tmp_path, monkeypatch):
    """End-to-end: holding position with trader.position==0 must block manual spread entry."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    strat = SimpleNamespace(
        _has_position=True,
        _lifecycle="TRAILING_LONG",
        _released_leg="near",
        _trade_id="mts-existing-001",
    )
    submitted = []
    mon = _make_manual_trade_monitor(tmp_path, strat, submitted)

    handled = FuturesMonitor._process_manual_trade_flag(mon)

    assert handled is True
    assert mon._manual_trade_status in ("SKIPPED: MTS_POSITION_EXISTS", "FAILED: POS_EXIST")
    assert strat._lifecycle == "TRAILING_LONG"
    assert strat._trade_id == "mts-existing-001"
    assert submitted == []
    assert not (tmp_path / "manual_trade.flag.processing").exists()


def test_manual_spread_l15753_guard_fails_closed_if_position_opens_mid_flight(tmp_path, monkeypatch):
    """L15753 guard: if strategy._has_position becomes True before L15753 (e.g. concurrent fill),
    L15753 must reject with FAILED: POS_EXIST, keep _lifecycle untouched, and submit zero orders.
    """
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))

    class _ConcurrentFillStrat:
        def __init__(self):
            self._reads = 0
            self._lifecycle = "OPEN"
            self._trade_id = "mts-existing-002"

        @property
        def _has_position(self):
            self._reads += 1
            # First read (L15364) sees False; second read (L15753) sees True after concurrent fill
            return self._reads >= 2

    strat = _ConcurrentFillStrat()
    submitted = []
    mon = _make_manual_trade_monitor(tmp_path, strat, submitted)

    handled = FuturesMonitor._process_manual_trade_flag(mon)

    assert handled is True
    assert mon._manual_trade_status == "FAILED: POS_EXIST"
    assert strat._lifecycle == "OPEN"
    assert strat._trade_id == "mts-existing-002"
    assert submitted == []
    assert not (tmp_path / "manual_trade.flag.processing").exists()
