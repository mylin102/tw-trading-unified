"""2026-11-10 Hermes Agent: contract-resolution failure must FAIL CLOSED.

Observed twice in production (2026-11-07 and 2026-11-10): the Shioaji session
was not established, so `get_contracts_list()` returned [] and the log showed

    [FuturesMonitor] Found 0 TMF contracts
    [FuturesMonitor] No TMF contracts found!

`_resolve_contracts()` only printed a red line and returned, leaving
`self.contract = None`. The monitor then entered run(); `_strategy_tick()`
raised on every tick at the settlement-day check:

    AttributeError: 'NoneType' object has no attribute 'delivery_date'

which killed the monitor thread. The process finished "cleanly" (exit 0) and
PM2's `stop_exit_codes: [0]` treats a clean exit as a deliberate stop, so it
never restarted. The system stayed dead until a human noticed.

Fix: raise CONTRACT_RESOLUTION_FAILED from _resolve_contracts(), which routes
into main.py's CRITICAL_CRASH handling (non-zero exit -> supervised restart).
"""
import types
from datetime import date, timedelta

import pytest

from strategies.futures.monitor import FuturesMonitor


def _monitor(dry_run=False, ticker="TMF"):
    m = FuturesMonitor.__new__(FuturesMonitor)
    m.ticker = ticker
    m.api = object()
    m.dry_run = dry_run
    m.contract = None
    m.far_contract = None
    m._ingestion = types.SimpleNamespace(set_contract=lambda c: None)
    return m


def _contract(code, days_ahead):
    return types.SimpleNamespace(
        code=code,
        delivery_date=date.today() + timedelta(days=days_ahead),
    )


def test_zero_contracts_fails_closed(monkeypatch):
    """The exact 2026-11-10 production shape: broker returns no contracts."""
    import core.broker.shioaji_compat as compat
    monkeypatch.setattr(compat, "get_contracts_list", lambda api, kind, sym: [])

    m = _monitor()
    with pytest.raises(RuntimeError) as exc:
        m._resolve_contracts()

    assert "CONTRACT_RESOLUTION_FAILED" in str(exc.value)
    assert m.contract is None, "must not invent a contract"


def test_broker_exception_also_fails_closed(monkeypatch):
    """A raising contract query must not be swallowed into a None contract."""
    import core.broker.shioaji_compat as compat

    def boom(api, kind, sym):
        raise RuntimeError("SessionNotEstablished")

    monkeypatch.setattr(compat, "get_contracts_list", boom)

    m = _monitor()
    with pytest.raises(RuntimeError) as exc:
        m._resolve_contracts()

    assert "CONTRACT_RESOLUTION_FAILED" in str(exc.value)


def test_dry_run_tolerates_no_contracts(monkeypatch):
    """dry_run is indicator-only (it returns before strategy logic), so it
    must keep working without a resolvable contract."""
    import core.broker.shioaji_compat as compat
    monkeypatch.setattr(compat, "get_contracts_list", lambda api, kind, sym: [])

    m = _monitor(dry_run=True)
    m._resolve_contracts()  # must not raise
    assert m.contract is None


def test_resolved_contracts_do_not_raise(monkeypatch):
    """Happy path must be unaffected by the new guard."""
    import core.broker.shioaji_compat as compat
    monkeypatch.setattr(
        compat, "get_contracts_list",
        lambda api, kind, sym: [_contract("TMFK6", 20), _contract("TMFL6", 50)])

    m = _monitor()
    m._resolve_contracts()

    assert m.contract is not None
    assert m.contract.code == "TMFK6"
    assert m.far_contract is not None
    assert m.far_contract.code == "TMFL6"
