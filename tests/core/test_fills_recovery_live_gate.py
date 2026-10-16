"""LIVE fills-recovery gate: at startup the execution context's effective_mode
is NOT yet "live_ready" (the certificate transition happens later), so a gate
keyed on effective_mode let the fills-led recovery resurrect a ghost position
(reason=fills_recovery observed in production).  LIVE must NEVER recover from
historical fills — broker truth is authoritative.  live_trading alone must gate
the recovery path.  Uses an auto-stub so the whole _mts_tick flow is inert;
the ONLY observable is the strategy's _restore_from_fills_log call.
"""
from types import SimpleNamespace
from pathlib import Path

from strategies.futures.monitor import FuturesMonitor


class _Auto:
    """Auto-stub: callable, comparable, bool-able, attr-able, arithmetic-safe."""

    def __call__(self, *a, **k):
        return _Auto()

    def __getattr__(self, name):
        return _Auto()

    def __sub__(self, o):
        return 0.0

    def __rsub__(self, o):
        return 0.0

    def __radd__(self, o):
        return 0.0

    def __gt__(self, o):
        return False

    def __lt__(self, o):
        return False

    def __bool__(self):
        return False

    def __float__(self):
        return 0.0

    def __add__(self, o):
        return 0.0


class _AutoMonitor(FuturesMonitor):
    def __getattr__(self, name):
        return _Auto()


class _AutoStrategy:
    """Inert strategy whose fills-recovery call is recorded."""

    def __init__(self):
        self.calls = []
        self._has_position = False

    def __getattr__(self, name):
        return _Auto()

    def _restore_from_fills_log(self):
        self.calls.append("restore")
        return True

    def write_state(self, *a, **k):
        self.calls.append("write_state")


def test_fills_recovery_never_runs_in_live_even_before_live_ready(tmp_path, monkeypatch):
    mon = _AutoMonitor.__new__(_AutoMonitor)
    mon.live_trading = False  # futures_live.yaml has NO live_trading key!
    mon._execution_context = SimpleNamespace(requested_mode="live",
                                             effective_mode="")  # pre-gate!
    mon.cfg = {"mts": {"strategy": "tmf_spread"}}
    mon.far_contract = None
    mon.api = None
    mon.dry_run = True
    mon.ticker = "TMF"
    mon._oco_reconciled = True
    mon._check_broker_snapshot_request = lambda: False
    mon._mts_has_open_position_from_fills = lambda: True  # fills say OPEN

    strat = _AutoStrategy()
    mon._registry = SimpleNamespace(get=lambda name: strat)
    mon._strategy = strat

    p = Path(tmp_path) / "mts_position_state.json"
    p.write_text('{"has_position": false, "state": "HEARTBEAT"}')
    import strategies.futures.monitor as mod
    monkeypatch.setattr(mod, "_mts_position_state_path", lambda: p)

    mon._mts_tick(enriched_bar={"ts": 1.0, "close": 45800.0,
                                "spread_z": 1.5, "quote_age_ms": 1})
    # LIVE + fills open + state flat: the recovery must NOT resurrect.
    assert strat.calls == []
