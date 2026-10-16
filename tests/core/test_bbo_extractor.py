# Tests: pure BBO extractor quote-quality gate (2026-08-04 review item 1).
# _extract_bbo returns (bid, ask, quality) with quality in
#   BBO_VALID / TICK_ONLY / DATA_QUALITY_BLOCKED.
# The buy_price/sell_price fallback (f3743daa) was removed — futures ticks
# carry last/close only and must NEVER masquerade as BBO.
import sys, types, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.futures.monitor import _extract_bbo
from core.exit_shadow_f import FShadowCollector


def make_bidask(bid=None, ask=None, bv=None, av=None, dt="2026-08-04T13:00:00.000"):
    o = types.SimpleNamespace()
    o.bid_price = bid if bid is not None else ["100.0"]
    o.ask_price = ask if ask is not None else ["100.5"]
    o.bid_volume = bv if bv is not None else [5]
    o.ask_volume = av if av is not None else [5]
    o.datetime = dt
    o.code = "MXFH6"
    return o


def make_no_bbo():
    # object WITHOUT bid_price/ask_price (close exists but must not be used)
    o = types.SimpleNamespace()
    o.close = 99.9
    o.code = "MXFH6"
    return o


def make_futures_tick(last=43423.0, buy_price=None, sell_price=None):
    # exact runtime shape observed in dynamics capture: last present, no BBO
    o = types.SimpleNamespace()
    o.code = "TMFH6"
    o.last = last
    o.close = last
    o.buy_price = buy_price
    o.sell_price = sell_price
    return o


def test_extract_first_level_bbo():
    q = make_bidask(bid=["100.0"], ask=["100.5"], bv=[5], av=[7])
    bid, ask, quality = _extract_bbo(q)
    assert (bid, ask) == (100.0, 100.5)
    assert quality == "BBO_VALID"


def test_scalar_bid_ask_valid():
    o = types.SimpleNamespace(bid=43542.0, ask=43545.0)
    bid, ask, quality = _extract_bbo(o)
    assert quality == "BBO_VALID" and (bid, ask) == (43542.0, 43545.0)


def test_extract_empty_quotes_rejected():
    bid, ask, quality = _extract_bbo(make_bidask(bid=[], ask=[]))
    assert quality in ("TICK_ONLY", "DATA_QUALITY_BLOCKED")
    assert bid is None and ask is None


def test_extract_non_numeric_rejected():
    bid, ask, quality = _extract_bbo(make_bidask(bid=["abc"], ask=["100.5"]))
    assert quality == "DATA_QUALITY_BLOCKED"
    assert bid is None and ask is None


def test_extract_invalid_prices_rejected():
    for bad in (["0.0"], ["-1"]):
        bid, ask, quality = _extract_bbo(make_bidask(bid=bad, ask=["100.5"]))
        assert quality == "DATA_QUALITY_BLOCKED"
        assert bid is None and ask is None
    # ask < bid inverted
    bid, ask, quality = _extract_bbo(make_bidask(bid=["100.5"], ask=["100.4"]))
    assert quality == "DATA_QUALITY_BLOCKED"


def test_futures_tick_is_tick_only_never_bbo():
    # The 2026-08-04 root cause: futures tick stream (TMFH6) has last only.
    # buy_price/sell_price fallback must NOT resurrect it as BBO.
    bid, ask, quality = _extract_bbo(make_futures_tick())
    assert quality == "TICK_ONLY"
    assert bid is None and ask is None


def test_futures_tick_with_stale_buy_sell_still_not_bbo():
    # Even if buy_price/sell_price exist on a tick, they are NOT the
    # executable BBO contract — TICK_ONLY, never BBO_VALID.
    bid, ask, quality = _extract_bbo(make_futures_tick(buy_price=43420.0, sell_price=43426.0))
    assert quality == "TICK_ONLY"
    assert bid is None and ask is None


def test_no_close_fallback():
    q = make_no_bbo()  # no bid_price/ask_price — close exists but unused
    bid, ask, quality = _extract_bbo(q)
    assert quality == "TICK_ONLY"   # close classifies as tick-only diagnostics
    assert bid is None and ask is None


def test_empty_object_blocked():
    bid, ask, quality = _extract_bbo(types.SimpleNamespace())
    assert quality == "DATA_QUALITY_BLOCKED"


def test_same_datetime_updates_have_unique_pair_keys():
    # Two BBO updates with identical datetime must produce distinct
    # near/far seq (internal monotonic) — second pair not deduped.
    with tempfile.TemporaryDirectory() as td:
        c = FShadowCollector(os.path.join(td, "sf.jsonl"))
        c.on_quote("NEAR", 100.0, 100.5,
                   receive_ts="2026-08-04T13:00:00.000")   # near
        c.on_quote("FAR", 100.2, 100.7,
                   receive_ts="2026-08-04T13:00:00.000")    # far
        n1, f1 = c._near.get("seq"), c._far.get("seq")
        assert n1 is not None and f1 is not None and (n1, f1) != (None, None)
        c.on_quote("NEAR", 100.1, 100.6,
                   receive_ts="2026-08-04T13:00:00.000")   # near again (same dt)
        c.on_quote("FAR", 100.3, 100.8,
                   receive_ts="2026-08-04T13:00:00.000")    # far again (same dt)
        n2, f2 = c._near.get("seq"), c._far.get("seq")
        assert (n2, f2) != (n1, f1)
        # evaluate with the new pair must not be skipped as duplicate
        c.evaluate({"has_position": True, "near_entry": 100.0,
                    "far_entry": 100.5, "near_side": "SHORT", "far_side": "LONG",
                    "trade_id": "T1", "position_generation": "G1",
                    "entry_order_ids": ["O1", "O2"]})
        assert c._evaluated_pairs is not None
        assert (n2, f2) in c._evaluated_pairs
