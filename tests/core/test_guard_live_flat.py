"""Entry guard LIVE semantics: the fills-ledger orphan (a broker-side close
the system never saw) must not block a fresh entry when the live broker
snapshot directly confirms flat.  The guard's _live_broker_flat_proven flag
is in-memory and can be stale during degraded windows; the canonical
snapshot (fresh + OK capture + no futures position + no open orders) is
direct flat evidence.
"""
import json
import time
from pathlib import Path
from types import SimpleNamespace

from tests.core.test_fills_recovery_live_gate import _AutoMonitor


def _write_canonical(p: Path, captured_at_ms: int):
    p.write_text(json.dumps({
        "source": "live_broker",
        "captured_at": captured_at_ms,
        "fetch_status": {"capture": "OK"},
        "positions": [],
        "open_orders": [],
        "broker_trades": [],
    }))


def test_guard_allows_entry_when_canonical_confirms_flat(tmp_path, monkeypatch):
    import core.runtime_paths as rp
    canon = tmp_path / "broker_snapshot_canonical.json"
    _write_canonical(canon, int(time.time() * 1000))
    monkeypatch.setattr(
        rp, "runtime_path",
        lambda *parts: str(tmp_path / parts[-1]))

    mon = _AutoMonitor.__new__(_AutoMonitor)
    mon._execution_context = SimpleNamespace(requested_mode="live")
    mon._mts_has_open_position_from_fills = lambda: True   # ledger orphan!
    mon._mts_has_pending_mts_orders = lambda: False
    mon._broker_position_observed = False
    mon._broker_authority_degraded = False
    mon._live_broker_flat_proven = False  # in-memory flag stale!

    p = tmp_path / "mts_position_state.json"
    p.write_text('{"has_position": false, "state": "HEARTBEAT"}')
    import strategies.futures.monitor as mod
    monkeypatch.setattr(mod, "_mts_position_state_path", lambda: p)

    strat = SimpleNamespace(_has_position=False)
    blocked = mon._mts_block_entry_if_open_position(
        strat, "SELL_NEAR_BUY_FAR")
    assert blocked is False  # canonical flat proof wins over ledger orphan
