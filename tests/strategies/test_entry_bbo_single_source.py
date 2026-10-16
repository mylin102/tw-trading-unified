"""The entry pricing must read ONE BBO source: the bar fields, else the live BidAsk slots."""
import time
from types import SimpleNamespace

from strategies.futures.monitor import _entry_bbo_from_bar_or_slots


def _mon(now, near_age=0.05, far_age=0.06):
    return SimpleNamespace(ticker="TMF", market_data={
        "TMF": {"bid": 48259.0, "ask": 48260.0, "bidask_at": now - near_age},
        "TMF_FAR": {"bid": 48423.0, "ask": 48434.0, "bidask_at": now - far_age},
    })


def test_falls_back_to_the_live_bidask_view_when_the_bar_lacks_the_bbo():
    # This is the production shape that starved the entry path: a bar with no bbo_* keys.
    bar = {"spread": -191.0}
    v = _entry_bbo_from_bar_or_slots(_mon(time.time()), bar)
    assert (v["near_bid"], v["near_ask"]) == (48259.0, 48260.0)
    assert (v["far_bid"], v["far_ask"]) == (48423.0, 48434.0)
    assert v["near_state"] == "OK" and v["far_state"] == "OK"
    assert 0 <= v["near_age_ms"] < 5000 and 0 <= v["far_age_ms"] < 5000


def test_newer_tick_does_not_void_the_view():
    bar = {"bbo_near_bid": None, "bbo_far_bid": None}
    v = _entry_bbo_from_bar_or_slots(_mon(time.time()), bar)
    assert v["near_bid"] == 48259.0 and v["far_bid"] == 48423.0


def test_bar_values_are_used_when_present():
    bar = {"bbo_near_bid": 1.0, "bbo_near_ask": 1.1, "bbo_far_bid": 2.0, "bbo_far_ask": 2.1,
           "bbo_near_age_ms": 10, "bbo_far_age_ms": 20,
           "bbo_near_state": "OK", "bbo_far_state": "OK"}
    v = _entry_bbo_from_bar_or_slots(_mon(time.time()), bar)
    assert (v["near_bid"], v["far_ask"], v["near_age_ms"]) == (1.0, 2.1, 10)


def test_no_bidask_anywhere_reports_the_state():
    v = _entry_bbo_from_bar_or_slots(SimpleNamespace(ticker="TMF", market_data={}), {"spread": -1.0})
    assert v["near_bid"] is None and v["near_state"] == "NO_BIDASK"
    assert v["far_state"] == "NO_BIDASK"
