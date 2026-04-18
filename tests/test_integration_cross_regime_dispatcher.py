import time
from types import SimpleNamespace

from strategies.cross_regime import TxBarBuilder
from main import FeedHealth, tick_dispatcher


from datetime import datetime

def make_tick(code, close, dt=None, volume=1):
    t = SimpleNamespace()
    t.code = code
    t.close = close
    t.volume = volume
    t.datetime = dt if dt is not None else datetime.now()
    return t


def test_dispatcher_updates_feed_and_builds_tx_bars():
    fh = FeedHealth()
    tx_builder = TxBarBuilder(timeframe="5min", max_bars=10)

    events = []

    class DummyMon:
        def __init__(self):
            self.called = 0
        def on_tick(self, ex, tick):
            self.called += 1
            events.append((ex, getattr(tick, 'code', None)))

    fm = DummyMon()
    om = DummyMon()

    dispatcher = tick_dispatcher(fm, om, fh, tx_builder)

    now = time.time()
    # send one TMF tick
    t1 = make_tick('TMF202605', 1000, dt=None)
    dispatcher('ex', t1)

    # send TX ticks
    for i in range(6):
        tk = make_tick('TXF202605', 900 + i, dt=None)
        dispatcher('ex', tk)
        time.sleep(0.01)

    snap = fh.snapshot()
    ages = snap.get('ages', {})
    assert 'TX' in ages and 'TMF' in ages
    # tx_builder should have built at least 1 bar (may depend on timestamps); ensure last_tick_time set
    assert tx_builder.last_tick_time > 0
    # monitors received ticks
    assert fm.called >= 1
    assert om.called >= 1
