"""2026-11-10 Hermes Agent: regression for main.py:429's undefined-name bug.

`bidask_dispatcher()`'s OTM-skew branch read `getattr(futures_mon, ...)` where
`futures_mon` (singular) was never defined in scope (the loop variable
elsewhere in this function is `f_mon`, iterating the plural `futures_mons`
parameter). The NameError was silently swallowed by the enclosing
`except Exception` at the bottom of `on_bidask()`, so every OTM-option
bidask callback fell through to the "bidask unmatched" warning path and
`skew_engine.on_quote()` was NEVER invoked in production -- a fully dead
integration with no error surfaced anywhere.
"""
import types

import main as main_mod


class _FakeContract:
    def __init__(self, code, strike_price=0.0, delivery_date=""):
        self.code = code
        self.strike_price = strike_price
        self.delivery_date = delivery_date


class _FakeBidAsk:
    def __init__(self, code, bid, ask):
        self.code = code
        self.bid_price = [bid]
        self.ask_price = [ask]


def _make_options_mon():
    mon = types.SimpleNamespace(
        active_contracts={"C": None, "P": None, "MTX": None},
        market_data={},
    )
    return types.SimpleNamespace(monitor=mon)


def test_otm_skew_bidask_reaches_skew_engine_without_nameerror():
    otm_contract = _FakeContract("TXO20500C", strike_price=20500.0)
    f_mon = types.SimpleNamespace(
        contract=None, far_contract=None,
        _skew_otm_contracts={"TXO20500C": otm_contract},
        market_data={},
    )
    skew_calls = []
    skew_engine = types.SimpleNamespace(on_quote=lambda event: skew_calls.append(event))

    on_bidask = main_mod.bidask_dispatcher(
        futures_mons=[f_mon], options_mon=_make_options_mon(), skew_engine=skew_engine)

    # Must not raise (the bug's NameError was previously caught silently by
    # the dispatcher's own except-block, so this call "succeeding" alone is
    # not sufficient proof -- assert the event actually arrived below).
    on_bidask("TAIFEX", _FakeBidAsk("TXO20500C", 50.0, 52.0))

    assert len(skew_calls) == 1, "skew_engine.on_quote was never called -- OTM skew branch is still dead"
    assert skew_calls[0].symbol == "TXO20500C"
    assert skew_calls[0].strike == 20500.0


def test_otm_skew_aggregates_across_multiple_monitors():
    """Two FuturesMonitor instances, OTM contract lives on the second one."""
    f_mon_a = types.SimpleNamespace(
        contract=None, far_contract=None, _skew_otm_contracts={}, market_data={})
    f_mon_b = types.SimpleNamespace(
        contract=None, far_contract=None,
        _skew_otm_contracts={"TXO21000P": _FakeContract("TXO21000P", strike_price=21000.0)},
        market_data={})
    skew_calls = []
    skew_engine = types.SimpleNamespace(on_quote=lambda event: skew_calls.append(event))

    on_bidask = main_mod.bidask_dispatcher(
        futures_mons=[f_mon_a, f_mon_b], options_mon=_make_options_mon(), skew_engine=skew_engine)
    on_bidask("TAIFEX", _FakeBidAsk("TXO21000P", 30.0, 32.0))

    assert len(skew_calls) == 1
    assert skew_calls[0].symbol == "TXO21000P"


def test_otm_skew_fires_on_repeat_ticks_not_just_first_appearance():
    """2026-11-10 Hermes Agent: regression for the second bug found while
    verifying the NameError fix. `_seen` (the print-dedup set) was reused as
    the fallback-branch gate; since `_seen.add(code)` ran unconditionally
    earlier in the SAME call, `code not in _seen` was already False by the
    time the OTM-skew check ran -- on every call, including the very first.
    This asserts the OTM match fires repeatedly across many ticks of the
    same code, not just (never, as it turned out) on first sight."""
    otm_contract = _FakeContract("TXO20500C", strike_price=20500.0)
    f_mon = types.SimpleNamespace(
        contract=None, far_contract=None,
        _skew_otm_contracts={"TXO20500C": otm_contract}, market_data={})
    skew_calls = []
    skew_engine = types.SimpleNamespace(on_quote=lambda event: skew_calls.append(event))

    on_bidask = main_mod.bidask_dispatcher(
        futures_mons=[f_mon], options_mon=_make_options_mon(), skew_engine=skew_engine)

    for _ in range(3):
        on_bidask("TAIFEX", _FakeBidAsk("TXO20500C", 50.0, 52.0))

    assert len(skew_calls) == 3, "OTM skew dispatch must fire on every tick, not just the first"
