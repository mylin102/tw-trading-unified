"""Contract tests: BidAsk slots are never contaminated by the tick path."""
from types import SimpleNamespace
from strategies.futures.monitor import _entry_bbo_view, _bbo_tick_fields


def bidask_slot(**kw):
    slot = {"bid": 44049.0, "ask": 44051.0}
    slot.update(kw)
    return slot


def test_tick_never_changes_the_entry_view_values():
    """CONTRACT 1: the tick path must not touch the BidAsk slot's bid/ask."""
    slot = bidask_slot(bidask_at=1000.0)
    before = _entry_bbo_view(slot, 1000.5)
    # a tick carrying its own BBO must publish tick_* keys, never bid/ask
    tick = SimpleNamespace(buy_price=44045.0, sell_price=44055.0)
    fields = _bbo_tick_fields(tick, bbo_now=1000.2)
    assert "bid" not in fields and "ask" not in fields, fields
    assert fields.get("tick_bid") == 44045.0 and fields.get("tick_ask") == 44055.0, fields
    slot.update(fields)
    after = _entry_bbo_view(slot, 1000.5)
    assert after == before, (before, after)


def test_stale_bidask_is_reported_and_never_substitutes_tick_values():
    """CONTRACT 2: a stale BidAsk keeps its own values + true age; tick values are never used."""
    slot = bidask_slot(bidask_at=960.0)          # 40 s old
    slot.update({"tick_bid": 44045.0, "tick_ask": 44055.0, "tick_bbo_at": 1000.0})
    view = _entry_bbo_view(slot, 1000.0)
    assert view["bid"] == 44049.0 and view["ask"] == 44051.0, view
    assert view["age_ms"] == 40000, view
    # the stale age is what the consumers' thresholds see (research 30 s => STALE_QUOTE)
    assert view["age_ms"] > 30000
