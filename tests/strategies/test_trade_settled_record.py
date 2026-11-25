"""2026-11-25 Hermes Agent: one TRADE_SETTLED per completed trade (fills-derived).

Before this, the independent-leg-bracket path emitted only per-leg fills. On
2026-11-25 four trades completed and there were ZERO settlement events, so every
consumer had to re-sum legs by hand — the same place where the trigger-vs-
realised P&L confusion originated.
"""
import json
import pytest

import strategies.plugins.futures.active.tmf_spread as ts


def _fill(leg, side, price, ftype, realized=None, trade_id="T1"):
    return {"trade_id": trade_id, "leg": leg, "side": side, "price": price,
            "fill_type": ftype, "realized_pnl": realized}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    log = tmp_path / "fills.jsonl"
    monkeypatch.setattr(ts, "_MTS_FILL_LOG", str(log))
    monkeypatch.setattr(ts, "_SETTLED_TRADES", set())
    events = []
    monkeypatch.setattr(ts, "_append_event", lambda name, **kw: events.append((name, kw)))
    monkeypatch.setattr(ts, "get_point_value", lambda ticker: 10.0)

    def write(rows):
        with open(log, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    return write, events, log


def test_settles_only_when_both_legs_closed(harness):
    write, events, _ = harness
    write([_fill("NEAR", "SHORT", 37599, "ENTRY"),
           _fill("FAR", "LONG", 37690, "ENTRY"),
           _fill("FAR", "SELL", 37658, "RELEASE", realized=-375.1)])
    ts._maybe_emit_trade_settled("T1", "TMF", "RELEASE", 37658, "FAR")
    assert events == []                      # near leg still open

    write([_fill("NEAR", "SHORT", 37599, "ENTRY"),
           _fill("FAR", "LONG", 37690, "ENTRY"),
           _fill("FAR", "SELL", 37658, "RELEASE", realized=-375.1),
           _fill("NEAR", "BUY", 37473, "EXIT", realized=1205.0)])
    ts._maybe_emit_trade_settled("T1", "TMF", "EXIT", 37473, "NEAR")
    assert len(events) == 1
    name, kw = events[0]
    assert name == "TRADE_SETTLED"
    assert kw["trade_id"] == "T1"
    # near SHORT 37599->37473 = +126 pts ; far LONG 37690->37658 = -32 pts
    assert kw["gross_points"] == 94.0
    assert kw["gross_twd"] == 940.0
    assert kw["realized_pnl_twd"] == 829.9
    assert kw["fees_twd"] == round(940.0 - 829.9, 1)
    assert {l["leg"] for l in kw["legs"]} == {"NEAR", "FAR"}


def test_emits_once_per_trade(harness):
    write, events, _ = harness
    rows = [_fill("NEAR", "SHORT", 100, "ENTRY"), _fill("FAR", "LONG", 200, "ENTRY"),
            _fill("NEAR", "BUY", 90, "EXIT", realized=90.0),
            _fill("FAR", "SELL", 195, "RELEASE", realized=-5.0)]
    write(rows)
    ts._maybe_emit_trade_settled("T1", "TMF", "EXIT", 90, "NEAR")
    ts._maybe_emit_trade_settled("T1", "TMF", "RELEASE", 195, "FAR")
    assert len(events) == 1


def test_ignores_entry_fills(harness):
    write, events, _ = harness
    write([_fill("NEAR", "SHORT", 100, "ENTRY")])
    ts._maybe_emit_trade_settled("T1", "TMF", "ENTRY", 100, "NEAR")
    assert events == []


def test_never_raises_on_corrupt_log(harness):
    write, events, log = harness
    with open(log, "w") as fh:
        fh.write("{not json}\n")
    ts._maybe_emit_trade_settled("T1", "TMF", "EXIT", 100, "NEAR")   # must not raise
    assert events == []
