# Tests: pure BBO extractor + internal seq pair-key uniqueness.
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


def test_extract_first_level_bbo():
    q = make_bidask(bid=["100.0"], ask=["100.5"], bv=[5], av=[7])
    assert _extract_bbo(q) == (100.0, 100.5, 5, 7)


def test_extract_empty_quotes_rejected():
    assert _extract_bbo(make_bidask(bid=[], ask=[])) is None
    assert _extract_bbo(make_no_bbo()) is None


def test_extract_non_numeric_rejected():
    assert _extract_bbo(make_bidask(bid=["abc"], ask=["100.5"])) is None


def test_extract_invalid_prices_rejected():
    assert _extract_bbo(make_bidask(bid=["0.0"], ask=["100.5"])) is None  # bid<=0
    assert _extract_bbo(make_bidask(bid=["100.5"], ask=["100.4"])) is None  # ask<bid
    assert _extract_bbo(make_bidask(bid=["-1"], ask=["100.5"])) is None


def test_no_close_fallback():
    q = make_no_bbo()  # no bid_price/ask_price — close exists but unused
    assert _extract_bbo(q) is None


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
