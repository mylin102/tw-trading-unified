"""LIVE fills-recovery gate: at startup the execution context's effective_mode
is NOT yet "live_ready" (the certificate transition happens later), so a gate
keyed on effective_mode lets the fills-led recovery resurrect a ghost position
(reason=fills_recovery observed in production).  LIVE must NEVER recover from
historical fills — broker truth is authoritative.  live_trading alone must gate
the recovery path.
"""
from types import SimpleNamespace
from pathlib import Path

from strategies.futures.monitor import FuturesMonitor


def _stub_monitor(tmp_path, monkeypatch):
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon.live_trading = True
    mon._execution_context = SimpleNamespace(effective_mode="")  # pre-gate!
    mon.cfg = {"mts": {"strategy": "tmf_spread"}}
    mon.far_contract = None
    mon.api = None
    mon.dry_run = True
    mon.ticker = "TMF"
    mon._oco_reconciled = True
    mon.market_data = {}
    mon._check_broker_snapshot_request = lambda: False
    mon._mts_has_open_position_from_fills = lambda: True  # fills say OPEN

    calls = []
    fake_strategy = SimpleNamespace(
        _has_position=False,
        _lifecycle="OPEN",
        _restore_from_fills_log=lambda: (calls.append("restore"), True)[1],
        write_state=lambda *a, **k: calls.append("write_state"),
    )
    mon._registry = SimpleNamespace(get=lambda name: fake_strategy)
    mon._strategy = fake_strategy

    p = Path(tmp_path) / "mts_position_state.json"
    p.write_text('{"has_position": false, "state": "HEARTBEAT"}')
    import strategies.futures.monitor as mod
    monkeypatch.setattr(mod, "_mts_position_state_path", lambda: p)
    return mon, calls


def test_fills_recovery_never_runs_in_live_even_before_live_ready(tmp_path, monkeypatch):
    mon, calls = _stub_monitor(tmp_path, monkeypatch)
    mon._mts_tick(enriched_bar={"ts": 1.0, "close": 45800.0,
                                "spread_z": 1.5, "quote_age_ms": 1})
    # LIVE + fills open + state flat: the recovery must NOT resurrect.
    assert calls == []
