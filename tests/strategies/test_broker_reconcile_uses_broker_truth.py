"""S2: the ADR-009 broker-position recovery must decide with BROKER truth.

Live 2026-09-29 (trade broker-reconciled-b44e20763a980f92): the combined exit
filled both legs at the broker (48063 / 48206) while the local exit fills were
dropped downstream (BROKER_RECEIPT_IDENTITY_MISSING).  `self.trader.position`
- a NEAR-leg mirror fed only by fills - therefore stayed -1, and the ADR-009
recovery restored the lifecycle to SPREAD again and again ... in the same tick
where POSITION_AUTHORITY had just forced the strategy to FLAT because broker
truth proved the account flat (log: "Ledger FLAT + memory open - force-syncing
strategy to FLAT" immediately followed by "broker_pos=-1 -> lifecycle restored
to SPREAD").

The reset path already consults broker truth (`_broker_truth_is_flat`); the
restore path must use the same authority instead of a local mirror.

RED at the base: test_broker_flat_never_reopens_a_recovered_spread fails.
Control: test_broker_holding_the_spread_still_restores proves the block is
really reached (so the RED cannot pass by an early return).
"""
from pathlib import Path
from types import SimpleNamespace

from strategies.futures.monitor import FuturesMonitor


class _Auto:
    """Auto-stub: callable, attr-able, falsy, arithmetic-safe."""

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


class _Strat:
    """Tolerant strategy stub: real attributes for the reconcile decision, an
    auto-stub for everything else (the tick flow continues into on_bar)."""

    def __init__(self):
        self._has_position = False
        self._ticker = "TMF"
        self._trade_id = "broker-reconciled-b44e20763a980f92"
        self._lifecycle_oca = SimpleNamespace(phase=SimpleNamespace(value="FLAT"))
        self._near_entry = 48176.0
        self._far_entry = 48320.0
        self._near_side = "SHORT"
        self._far_side = "LONG"
        self._released_leg = None
        self._last_exit_ts = None
        self._lifecycle = "FLAT"

    def __getattr__(self, name):
        return _Auto()

    def on_bar(self, ctx):
        return None


def _auth(status):
    return SimpleNamespace(status=SimpleNamespace(value=status))


def _build(monkeypatch, tmp_path, *, broker_status, mirror_pos, fills_open=True):
    mon = _AutoMonitor.__new__(_AutoMonitor)
    mon.live_trading = True
    mon.dry_run = True
    mon.ticker = "TMF"
    mon.cfg = {"mts": {"strategy": "tmf_spread"}}
    mon.far_contract = None
    mon.api = None
    mon._oco_reconciled = True
    mon._check_broker_snapshot_request = lambda: False
    mon._mts_has_open_position_from_fills = lambda: fills_open
    mon.trader = SimpleNamespace(position=mirror_pos)
    mon._execution_context = SimpleNamespace(requested_mode="live",
                                             effective_mode="live_ready")
    mon._refresh_live_broker_authority = lambda strategy: _auth(broker_status)

    strat = _Strat()
    mon._registry = SimpleNamespace(get=lambda name: strat)
    mon._strategy = strat

    p = Path(tmp_path) / "mts_position_state.json"
    p.write_text('{"has_position": false, "state": "HEARTBEAT"}')

    import strategies.futures.monitor as mod
    monkeypatch.setattr(mod, "_mts_position_state_path", lambda: p)
    monkeypatch.setattr(mod, "is_taifex_futures_market_open", lambda: True)

    writes = []
    import strategies.plugins.futures.active.tmf_spread as tmf
    monkeypatch.setattr(tmf, "_write_mts_state",
                        lambda **kw: writes.append(kw.get("action")))
    return mon, strat, writes


_BAR = {"ts": 1.0, "close": 45800.0, "spread_z": 1.5, "quote_age_ms": 1}


def test_broker_flat_never_reopens_a_recovered_spread(monkeypatch, tmp_path):
    """Broker proven FLAT + ledger OPEN + mirror -1: no SPREAD restore."""
    mon, strat, writes = _build(monkeypatch, tmp_path,
                               broker_status="FLAT", mirror_pos=-1)
    mon._mts_tick(enriched_bar=dict(_BAR))

    assert "BROKER_RECONCILED" not in writes
    assert strat._has_position is False


def test_broker_holding_the_spread_still_restores(monkeypatch, tmp_path):
    """Control: broker truth says the spread is held -> restore must happen."""
    mon, strat, writes = _build(monkeypatch, tmp_path,
                               broker_status="SPREAD", mirror_pos=-1)
    mon._mts_tick(enriched_bar=dict(_BAR))

    assert "BROKER_RECONCILED" in writes
    assert strat._has_position is True


def test_unknown_broker_truth_keeps_the_existing_recovery_behaviour(monkeypatch, tmp_path):
    """Broker truth unavailable (None) is not a flat proof: behaviour unchanged."""
    mon, strat, writes = _build(monkeypatch, tmp_path,
                               broker_status=None, mirror_pos=-1)
    mon._mts_tick(enriched_bar=dict(_BAR))

    assert "BROKER_RECONCILED" in writes
