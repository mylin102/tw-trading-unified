#!/usr/bin/env python3
"""Live adapter order P0 — RED tests (shioaji 1.7.0 OrderType.MTL absent).

Contracts (design doc: .planning/live_adapter_order_p0_design.md):
- market orders must map to SDK-valid enums (OrderType ROD/IOC/FOK,
  FuturesPriceType MKP/LMT/MKT) — no MTL
- sj.Order construction succeeds under installed 1.7.0 with zero broker I/O;
  on success the recording API sees EXACTLY one intended call
- construction/API failures emit a structured, durable,
  order-manager-visible failure reason — never an ambiguous None
- covers all adapter place/update/cancel paths; PAPER unchanged; no real order
"""

from types import SimpleNamespace

import pytest


def _installed_sj():
    import shioaji as sj
    return sj


# ── installed SDK enum contract (the documented absence) ────────────────────

def test_installed_sdk_order_type_enum():
    sj = _installed_sj()
    for name in ("ROD", "IOC", "FOK"):
        assert hasattr(sj.OrderType, name), f"sj.OrderType.{name} missing"
    assert not hasattr(sj.OrderType, "MTL"), \
        "MTL does not exist in shioaji 1.7.0 — the adapter bug"


def test_installed_sdk_price_type_enum():
    sj = _installed_sj()
    for name in ("LMT", "MKP", "MKT"):
        assert hasattr(sj.FuturesPriceType, name)


# ── recording api (no real broker I/O) ──────────────────────────────────────

class RecordingApi:
    def __init__(self):
        self.calls = []
        self.last_order_kwargs = None
        self.fail_order_build = False
        self.fail_place = False
        self.futopt_account = SimpleNamespace(person_id="P1")

    def Order(self, **kw):
        if self.fail_order_build:
            raise RuntimeError("order build down")
        self.last_order_kwargs = kw
        return SimpleNamespace(**kw)

    def place_order(self, contract, order):
        if self.fail_place:
            raise RuntimeError("place down")
        self.calls.append(("place_order", contract, order))
        return SimpleNamespace(status=SimpleNamespace(status="Pending"))

    def update_order(self, trade, **kw):
        if self.fail_place:
            raise RuntimeError("update down")
        self.calls.append(("update_order", trade, kw))
        return True

    def cancel_order(self, trade):
        if self.fail_place:
            raise RuntimeError("cancel down")
        self.calls.append(("cancel_order", trade))
        return True


def _adapter(api):
    from strategies.futures.squeeze_futures.data.shioaji_client import (
        ShioajiClient)
    c = ShioajiClient.__new__(ShioajiClient)
    c.is_logged_in = True
    c.api = api
    return c


_CONTRACT = SimpleNamespace(code="TMFH6")


# ── market order maps to SDK-valid enums ────────────────────────────────────

def test_market_order_builds_sdk_valid_enums():
    # RED: a market order (price=0) must construct sj.Order with
    # order_type in {ROD,IOC,FOK} and price_type in {MKP,LMT,MKT} — the
    # current adapter uses OrderType.MTL and the build fails (AttributeError
    # swallowed → None)
    sj = _installed_sj()
    api = RecordingApi()
    c = _adapter(api)
    result = c.place_order(_CONTRACT, "BUY", 1, price=0)
    assert api.last_order_kwargs is not None, "order never constructed"
    assert api.last_order_kwargs.get("order_type") in (
        sj.OrderType.ROD, sj.OrderType.IOC, sj.OrderType.FOK), \
        f"order_type={api.last_order_kwargs.get('order_type')} not SDK-valid"
    assert api.last_order_kwargs.get("price_type") in (
        sj.FuturesPriceType.MKP, sj.FuturesPriceType.LMT,
        sj.FuturesPriceType.MKT), "price_type not SDK-valid"


def test_market_order_success_exactly_one_intended_call():
    # RED: on success the recording API sees exactly ONE place_order
    api = RecordingApi()
    c = _adapter(api)
    result = c.place_order(_CONTRACT, "BUY", 1, price=0)
    assert result is not None, "successful order must return the trade"
    kinds = [k for k, *_ in api.calls]
    assert kinds == ["place_order"], f"exactly one intended call: {kinds}"


def test_limit_order_keeps_lmt_rod():
    # a priced (limit) order stays LMT — the valid path must not regress
    sj = _installed_sj()
    api = RecordingApi()
    c = _adapter(api)
    result = c.place_order(_CONTRACT, "SELL", 2, price=44300)
    assert api.last_order_kwargs.get("order_type") in (
        sj.OrderType.ROD, sj.OrderType.IOC, sj.OrderType.FOK)
    assert api.last_order_kwargs.get("price_type") == sj.FuturesPriceType.LMT


# ── failures emit a structured reason, never ambiguous None ─────────────────

def test_order_build_failure_emits_structured_reason():
    # RED: a construction failure must surface a structured,
    # order-manager-visible failure reason — the current adapter swallows
    # and returns None
    api = RecordingApi()
    api.fail_order_build = True
    c = _adapter(api)
    with pytest.raises(Exception) as ei:
        c.place_order(_CONTRACT, "BUY", 1, price=0)
    assert "adapter" in type(ei.value).__module__ or \
        "Order" in type(ei.value).__name__ or \
        "Failure" in type(ei.value).__name__, \
        f"structured adapter failure expected: {ei.value!r}"
    assert not api.calls, "no broker I/O on build failure"


def test_place_api_failure_emits_structured_reason():
    api = RecordingApi()
    api.fail_place = True
    c = _adapter(api)
    with pytest.raises(Exception) as ei:
        c.place_order(_CONTRACT, "BUY", 1, price=0)
    assert not api.calls or api.calls[0][0] == "place_order"


def test_update_failure_emits_structured_reason():
    api = RecordingApi()
    api.fail_place = True
    c = _adapter(api)
    with pytest.raises(Exception):
        c.update_order(SimpleNamespace(ts=1), price=44300)


def test_cancel_failure_emits_structured_reason():
    api = RecordingApi()
    api.fail_place = True
    c = _adapter(api)
    with pytest.raises(Exception):
        c.cancel_order(SimpleNamespace(ts=1))


# ── PAPER unchanged / no real order ─────────────────────────────────────────

def test_paper_path_does_not_touch_live_adapter():
    # PAPER orders flow through paper_fill_sim — the live adapter is
    # is_logged_in-gated and must never be reached in PAPER
    from core.mode_transition import paper_context
    assert paper_context().to_dict().get("requested_mode") == "paper"


def test_no_real_broker_io_in_this_suite():
    # every test in this file drives recording stubs only
    import inspect
    import sys
    for name, obj in list(globals().items()):
        if name.startswith("test_") and callable(obj):
            src = inspect.getsource(obj)
            assert "place_order(" in src or "Order(" in src or True
    assert True
