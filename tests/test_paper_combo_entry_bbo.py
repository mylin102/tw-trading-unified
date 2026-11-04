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
