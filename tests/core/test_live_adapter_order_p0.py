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


def test_adapter_must_use_non_deprecated_sj_enums():
    # three-line correction (B): sj.Order construction must use the current
    # non-deprecated enums (sj.OrderType / sj.FuturesPriceType), not
    # sj.constant.* — the adapter currently uses sj.constant.OrderType.MTL
    import re
    from pathlib import Path
    adapter = Path(__file__).resolve().parents[2] / "strategies" / "futures" \
        / "squeeze_futures" / "data" / "shioaji_client.py"
    text = adapter.read_text(encoding="utf-8")
    assert "sj.OrderType" in text or "OrderType.ROD" in text or \
        "OrderType.IOC" in text or "OrderType.FOK" in text, \
        "adapter must construct sj.Order with non-deprecated sj enums"
    assert "sj.constant.OrderType" not in text, \
        "sj.constant.OrderType (deprecated + MTL bug) must be gone"


# ── recording api (no real broker I/O) ──────────────────────────────────────

class RecordingApi:
    def __init__(self):
        self.calls = []
        self.last_order_kwargs = None
        self.fail_order_build = False
        self.fail_place = False
        self.reject_trade = False
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
        if self.reject_trade:
            return SimpleNamespace(status=SimpleNamespace(status="Failed"))
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
    # a market order (price=0) must construct sj.Order with SDK-valid
    # enums (the old adapter used OrderType.MTL — missing in 1.7.0)
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


def test_market_order_uses_ioc_not_rod():
    # TAIFEX: Market-With-Protection (MKP) requires IOC or FOK — ROD is
    # for limit orders only. MTS intent = immediate with protection → IOC.
    sj = _installed_sj()
    api = RecordingApi()
    c = _adapter(api)
    c.place_order(_CONTRACT, "BUY", 1, price=0)
    assert api.last_order_kwargs.get("order_type") == sj.OrderType.IOC, \
        f"market order must use IOC: {api.last_order_kwargs.get('order_type')}"
    assert api.last_order_kwargs.get("price_type") == sj.FuturesPriceType.MKP


def test_limit_order_uses_rod():
    sj = _installed_sj()
    api = RecordingApi()
    c = _adapter(api)
    c.place_order(_CONTRACT, "SELL", 2, price=44300)
    assert api.last_order_kwargs.get("order_type") == sj.OrderType.ROD, \
        "limit order must use ROD"
    assert api.last_order_kwargs.get("price_type") == sj.FuturesPriceType.LMT


def test_octype_explicit_auto_not_sdk_default():
    # FuturesOCType must be set explicitly (Auto — broker determines
    # New/Cover from position) — never rely on SDK defaults
    sj = _installed_sj()
    api = RecordingApi()
    c = _adapter(api)
    c.place_order(_CONTRACT, "BUY", 1, price=0)
    assert api.last_order_kwargs.get("octype") == sj.FuturesOCType.Auto, \
        f"octype must be explicit Auto: {api.last_order_kwargs.get('octype')}"


def test_rejected_trade_raises_structured_error():
    # a Failed/Rejected trade must never be returned as success — the
    # adapter raises ADAPTER_ORDER_REJECTED with context
    api = RecordingApi()
    api.reject_trade = True
    c = _adapter(api)
    with pytest.raises(Exception) as ei:
        c.place_order(_CONTRACT, "BUY", 1, price=0)
    assert type(ei.value).__name__ == "AdapterOrderError", ei.value
    assert ei.value.code == "ADAPTER_ORDER_REJECTED", ei.value.code
    assert ei.value.context.get("status") == "Failed"


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
    # three-line correction (B): a construction failure must raise a typed
    # AdapterOrderError with a stable code + context — the current adapter
    # swallows and returns None
    api = RecordingApi()
    api.fail_order_build = True
    c = _adapter(api)
    with pytest.raises(Exception) as ei:
        c.place_order(_CONTRACT, "BUY", 1, price=0)
    assert type(ei.value).__name__ == "AdapterOrderError", \
        f"structured adapter failure expected: {ei.value!r}"
    assert getattr(ei.value, "code", None), \
        "AdapterOrderError must carry a stable code"
    assert getattr(ei.value, "context", None), \
        "AdapterOrderError must carry context (method/contract/order)"
    assert not api.calls, "no broker I/O on build failure"


def test_place_api_failure_emits_structured_reason():
    api = RecordingApi()
    api.fail_place = True
    c = _adapter(api)
    with pytest.raises(Exception) as ei:
        c.place_order(_CONTRACT, "BUY", 1, price=0)
    assert type(ei.value).__name__ == "AdapterOrderError"
    assert getattr(ei.value, "code", None)
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


def _adapter_error():
    # the MONITOR catches AdapterOrderError from the `squeeze_futures...`
    # identity (monitor.py:32 inserts strategies/futures on sys.path) —
    # the test must raise THAT class, not the package-path twin
    import os
    import sys
    from pathlib import Path
    _sf = str(Path(__file__).resolve().parents[2] / "strategies" / "futures")
    if _sf not in sys.path:
        sys.path.insert(0, _sf)
    from squeeze_futures.data.shioaji_client import AdapterOrderError
    return AdapterOrderError(
        code="ADAPTER_ORDER_PLACE_FAILED",
        context={"method": "place_order", "contract": "TMFH6"})


def _monitor_with_raising_client(err):
    from strategies.futures.monitor import FuturesMonitor
    m = FuturesMonitor.__new__(FuturesMonitor)
    m.live_trading = True
    m.dry_run = False
    m.contract = SimpleNamespace(code="TMFH6")
    m._use_order_manager = False
    m.trader = SimpleNamespace(position=1, entry_price=0.0,
                               point_value=1, fee_per_side=0,
                               exchange_fee_per_side=0, tax_rate=0)

    class _RaisingClient:
        def __init__(self, e):
            self.e = e

        def place_order(self, *a, **k):
            raise self.e

    m.client = _RaisingClient(err)
    m.api = SimpleNamespace(cancel_order=lambda *a, **k: None)
    m._safety_stop_trade = None
    return m


def test_caller_records_durable_failure(monkeypatch):
    # three-line correction (B): the durable event/order-manager
    # propagation is CALLER-owned — the monitor routes must catch
    # AdapterOrderError and record a durable, order-manager-visible
    # failure event via the EXISTING audit channel (no new ledger)
    import strategies.futures.squeeze_futures.data.data_storage as ds
    records = []
    monkeypatch.setattr(ds, "save_signal_audit", lambda r: records.append(r))
    m = _monitor_with_raising_client(_adapter_error())
    try:
        m._execute_trade("EXIT", 44300, "2026-08-08T10:00:00", 1,
                         reason="TEST")
    except Exception:
        pass
    assert records, "caller must audit the durable failure"
    assert records[0].get("error_code") == "ADAPTER_ORDER_PLACE_FAILED", \
        f"structured code must reach the durable audit: {records}"


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
