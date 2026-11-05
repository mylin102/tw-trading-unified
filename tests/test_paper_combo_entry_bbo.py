"""2026-11-03 Hermes Agent: PAPER combo ENTRY must fill at the executable
side of the book (sell at bid, buy at ask), like PAPER exits already do
(2026-10-12 Codex, PAPER_EXIT_BBO_INVALID_OR_STALE).

Incident mts-auto-151203-918 (Mini PAPER 15:12:04): SELL_NEAR_BUY_FAR filled
near @43230 (a stale BAR_CLOSE) while the live near book was 43124/43129, far
@43220 (far ask 43220). The near leg was booked 106 pts better than sellable,
so the ledger showed +870 TWD within a minute.

Contract
  * monitor._process_pending_paper_fills(..., bbo=bar) attaches the fresh
    bid/ask (age <= max_quote_age_ms) to the synthetic ticks; stale/invalid
    -> bid/ask = None.
  * PaperFillSimulator.process_combo_ticks: a leg that is SOLD fills at bid,
    BOUGHT at ask. If a tick carries bid/ask attributes but they are not a
    valid book, the combo stays pending (fail closed). Ticks with no bid/ask
    attributes at all (legacy callers/tests) keep the close behaviour.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.order_management.order import Order, OrderSide, OrderStatus, OrderType
from core.order_management.paper_fill import PaperFillSimulator


class _Manager:
    def __init__(self):
        self.orders, self.fills = {}, []

    def on_fill(self, **p):
        self.fills.append(p)
        self.orders[p["order_id"]].fill(p["fill_price"], p["fill_qty"])

    def reject(self, order_id, reason, source=""):
        self.orders[order_id].status = OrderStatus.REJECTED


def _combo(action):
    m = _Manager()
    sim = PaperFillSimulator(m)
    o = Order(symbol="TMFK6-TMFL6", side=OrderSide.SELL, order_type=OrderType.MKP,
              quantity=1, price=-106.0,
              combo_legs=[{"symbol": "TMFK6", "side": "sell", "quantity": 1},
                          {"symbol": "TMFL6", "side": "buy", "quantity": 1}],
              combo_strategy="TIME_SPREAD")
    o.submit("PAPER-COMBO")
    m.orders[o.order_id] = o
    meta = {"spread_side": action}
    sim.register_combo(o, near_symbol="TMFK6", far_symbol="TMFL6", metadata=meta)
    return sim, m, o, meta


def _t(sym, close, bid, ask):
    return SimpleNamespace(symbol=sym, close=close, bid=bid, ask=ask)


def test_incident_sell_near_buy_far_fills_near_at_bid_far_at_ask():
    sim, m, o, meta = _combo("SELL_NEAR_BUY_FAR")
    sim.process_combo_ticks(_t("TMFK6", 43230.0, 43124.0, 43129.0),
                            _t("TMFL6", 43220.0, 43207.0, 43220.0))
    assert meta["near_fill_price"] == 43124.0     # not the stale 43230 close
    assert meta["far_fill_price"] == 43220.0
    assert o.status is OrderStatus.FILLED


def test_buy_near_sell_far_fills_near_at_ask_far_at_bid():
    sim, m, o, meta = _combo("BUY_NEAR_SELL_FAR")
    sim.process_combo_ticks(_t("TMFK6", 100.0, 99.0, 101.0), _t("TMFL6", 200.0, 198.0, 202.0))
    assert (meta["near_fill_price"], meta["far_fill_price"]) == (101.0, 198.0)
    assert m.fills[0]["fill_price"] == pytest.approx(101.0 - 198.0)


@pytest.mark.parametrize("nb,na", [(None, 101.0), (102.0, 101.0), (0.0, 101.0), (float("nan"), 101.0)])
def test_invalid_book_keeps_combo_pending(nb, na):
    sim, m, o, meta = _combo("BUY_NEAR_SELL_FAR")
    sim.process_combo_ticks(_t("TMFK6", 100.0, nb, na), _t("TMFL6", 200.0, 198.0, 202.0))
    assert m.fills == [] and sim.get_pending_count() == 1


def test_legacy_ticks_without_book_attributes_keep_close_behaviour():
    sim, m, o, meta = _combo("BUY_NEAR_SELL_FAR")
    sim.process_combo_ticks(SimpleNamespace(symbol="TMFK6", close=100.0),
                            SimpleNamespace(symbol="TMFL6", close=200.0))
    assert (meta["near_fill_price"], meta["far_fill_price"]) == (100.0, 200.0)


def _monitor():
    from unittest.mock import patch
    from strategies.futures.monitor import FuturesMonitor
    with patch.object(FuturesMonitor, "__init__", lambda self: None):
        mon = FuturesMonitor()
    mon.contract = SimpleNamespace(code="TMFK6")
    mon.far_contract = SimpleNamespace(code="TMFL6")
    mon.ticker = "TMF"
    mon.paper_fill_sim = MagicMock()
    mon.paper_fill_sim.get_pending_count.return_value = 1
    mon.cfg = {"mts": {"params": {"max_quote_age_ms": 30000}}}
    return mon


BAR = {"bbo_near_bid": 43124.0, "bbo_near_ask": 43129.0, "bbo_near_age_ms": 117.0,
       "bbo_far_bid": 43207.0, "bbo_far_ask": 43220.0, "bbo_far_age_ms": 111.0}


def test_monitor_attaches_fresh_bbo_to_combo_ticks():
    mon = _monitor()
    mon._process_pending_paper_fills(43230.0, 43220.0, ts=None, bbo=BAR)
    near, far = mon.paper_fill_sim.process_combo_ticks.call_args.args
    assert (near.bid, near.ask, far.bid, far.ask) == (43124.0, 43129.0, 43207.0, 43220.0)


def test_monitor_stale_bbo_becomes_none():
    mon = _monitor()
    bar = dict(BAR, bbo_near_age_ms=99999.0)
    mon._process_pending_paper_fills(43230.0, 43220.0, ts=None, bbo=bar)
    near, far = mon.paper_fill_sim.process_combo_ticks.call_args.args
    assert (near.bid, near.ask) == (None, None)
    assert (far.bid, far.ask) == (43207.0, 43220.0)


def test_monitor_without_any_book_keeps_close_behaviour():
    mon = _monitor()
    mon._process_pending_paper_fills(43230.0, 43220.0, ts=None)
    near, far = mon.paper_fill_sim.process_combo_ticks.call_args.args
    assert not hasattr(near, "bid") and not hasattr(far, "ask")


def test_live_callsite_passes_the_bar_book():
    import strategies.futures.monitor as mod
    src = Path(mod.__file__).read_text()
    i = src.index("self._process_pending_paper_fills(\n                near_price=_n_close")
    assert "bbo=_bar_dict" in src[i:i + 250]


# ── 2026-11-04 Hermes Agent: the IMMEDIATE fill at submit time ──────────────
# Incident mts-auto-112954-494 (Mini PAPER 11:29:54, running 1f2a629d which
# already fixed the per-tick pending path): SELL_NEAR_BUY_FAR filled near
# @42538 (book 42535/42538) and far @42594 -- BELOW the far bid 42604 (book
# 42604/42613). _submit_mts_combo_entry fills the PAPER combo on the spot with
# synthetic ticks that carry no book, so the pending-path fix never ran.
from datetime import datetime as _dt  # noqa: E402

from test_paper_combo_fill import real_paper_monitor  # noqa: E402,F401  (tests/ is on sys.path)

INCIDENT_BAR = {"spread": 56.0, "spread_z": 3.16,
                "bbo_near_bid": 42535.0, "bbo_near_ask": 42538.0, "bbo_near_age_ms": 40.0,
                "bbo_far_bid": 42604.0, "bbo_far_ask": 42613.0, "bbo_far_age_ms": 40.0}


def _submit_entry(mon, tmp_path, monkeypatch, bar, action="SELL_NEAR_BUY_FAR"):
    import json
    import strategies.plugins.futures.active.tmf_spread as mod
    from core.order_management.order import OrderSide
    from core.strategy_context import MarketData, PositionView, StrategyContext
    monkeypatch.setattr(mod, "_MTS_FILL_LOG", str(tmp_path / "fills.jsonl"))
    monkeypatch.setattr(mod, "_MTS_EVENT_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(mod, "_get_state_file_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(mod, "_PENDING_STATE_REWRITE", None)
    monkeypatch.delenv("MTS_BACKTEST", raising=False)
    strategy = mod.TMFSpread()
    strategy.init(StrategyContext(market=MarketData(last_bar={}, ticker="TMF"),
                                  position=PositionView(size=0), config={"ticker": "TMF"}))
    mon.ticker = "TMF"
    mon.cfg = {"ticker": "TMF"}
    mon._registry = {"tmf_spread": strategy}
    mon.order_mgr.register_callback("on_fill", lambda ev:
        mon._check_mts_multi_leg_fill(ev.order_id, ev.fill_price))
    sell_near = action == "SELL_NEAR_BUY_FAR"
    mon._submit_mts_combo_entry(
        trade_id="BBO-AUDIT", action=action, reason="entry bbo regression",
        near_code="TMFK6", far_code="TMFL6",
        near_side=OrderSide.SELL if sell_near else OrderSide.BUY,
        far_side=OrderSide.BUY if sell_near else OrderSide.SELL,
        near_price=42538.0, far_price=42594.0,          # the incident's signal prices
        ts=_dt(2026, 11, 4, 11, 29, 54), snapshot={}, bar_dict=bar)
    p = tmp_path / "fills.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    return {r["leg"]: r["price"] for r in rows if r.get("fill_type") == "ENTRY"}


def test_submit_time_fill_uses_executable_book_incident(real_paper_monitor, tmp_path, monkeypatch):
    fills = _submit_entry(real_paper_monitor, tmp_path, monkeypatch, INCIDENT_BAR)
    assert fills == {"NEAR": 42535.0, "FAR": 42613.0}    # sell@bid, buy@ask


def test_submit_time_fill_buy_near_sell_far(real_paper_monitor, tmp_path, monkeypatch):
    fills = _submit_entry(real_paper_monitor, tmp_path, monkeypatch, INCIDENT_BAR,
                          action="BUY_NEAR_SELL_FAR")
    assert fills == {"NEAR": 42538.0, "FAR": 42604.0}    # buy@ask, sell@bid


def test_submit_time_stale_book_does_not_fill_at_signal_price(real_paper_monitor, tmp_path,
                                                              monkeypatch):
    bar = dict(INCIDENT_BAR, bbo_far_age_ms=999999.0)
    fills = _submit_entry(real_paper_monitor, tmp_path, monkeypatch, bar)
    assert fills == {}                                   # stays pending, fail closed
