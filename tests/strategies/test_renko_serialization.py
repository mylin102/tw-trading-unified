# A3: serialization round-trip equivalence — tracker A → to_dict → from_dict
# → tracker B must behave identically on the same next tick.
import pytest

from strategies.plugins.futures.active.renko_tracker import RenkoTracker


def _build_tracker():
    t = RenkoTracker(anchor_price=44000.0, brick_size=10.0,
                     episode_id="EPI_A", trade_id="TRADE_1")
    t.add(44012.0)   # 1 brick up (44000 -> 44010)
    t.add(44032.0)   # 2 bricks (44010 -> 44030)
    return t


def test_round_trip_next_tick_equivalence():
    a = _build_tracker()
    d = a.to_dict()
    b = RenkoTracker.from_dict(d)

    # state projection equal
    assert b.renko_open == a.renko_open
    assert b.renko_close == a.renko_close
    assert b.trend == a.trend
    assert b.brick_sequence == a.brick_sequence
    assert b.total_bricks == a.total_bricks
    assert b.generation_id == a.generation_id
    assert b.locked_brick_size == a.locked_brick_size

    # same next tick → identical update result + final state
    r_a = a.add(44044.0)
    r_b = b.add(44044.0)
    assert r_a[0] == r_b[0]          # bricks created
    assert r_a[1] == r_b[1]          # trend
    assert b.renko_open == a.renko_open
    assert b.renko_close == a.renko_close
    assert b.trend == a.trend
    assert b.brick_sequence == a.brick_sequence


def test_round_trip_schema_version_present():
    d = _build_tracker().to_dict()
    assert d["schema_version"] == 1
    assert d["capability_available"] is True
    assert d["tracker_initialized"] is True
    assert "recent_bricks" in d
    assert isinstance(d["recent_bricks"], list)


def test_from_dict_empty_safe():
    b = RenkoTracker.from_dict({})
    assert b.anchor_price == 0.0
    assert b.trend == 0
    assert b.total_bricks == 0


def test_recent_bricks_bounded():
    t = RenkoTracker(anchor_price=44000.0, brick_size=1.0)
    for i in range(150):
        t.add(44000.0 + i)  # continuous up bricks
    assert len(t.get_recent_bricks()) <= 100
    assert t.get_recent_bricks(5) == t.get_recent_bricks()[-5:]
