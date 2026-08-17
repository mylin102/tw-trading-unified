"""RED — broker-confirmed single-leg recovery chain (Codex handoff 2026-08-18,
bounded slice, Hermes implementer / Codex read-only reviewer).

Part 1: when local fills lack RELEASE/FILLED but the canonical broker snapshot
shows exactly one remaining MTS leg AND exactly one current MTS_RELEASE order
identity (released contract absent, remaining contract present with valid
side/qty/avg_cost/session), the strategy must infer the SINGLE_LEG lifecycle
transition WITHOUT synthesizing a release price/PnL, persist
source=broker_reconciliation + snapshot/order identity, and initialize
PENDING_REANCHOR so the RESTART_BASELINE gate can establish a fresh-BBO
baseline.

Part 2: the baseline (anchor/peak/giveback) is established from the
same-session fresh remaining-leg BBO, persisted via the state write, and
restart-safe (a new process re-infers PENDING_REANCHOR — stale peak never
survives).

Fail-closed (zero inference / zero submit): ambiguous identity, query
failure, session mismatch, duplicate pending (two DISTINCT identities),
missing/invalid remaining leg, missing BBO.
"""
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.strategy_context import StrategyContext, MarketData, PositionView
from strategies.futures.monitor import FuturesMonitor
from strategies.futures.mts_ledger_authority import MtsAuthority
from strategies.plugins.futures.active.tmf_spread import (
    PositionLifecycle, PositionPhase, ReleaseGroup, ReleaseGroupStatus,
    TMFSpread, TrailAnchorStatus, TrailGroup, TrailGroupStatus,
)
from tests.strategies.test_tmf_spread_atr import _make_bar, _setup_armed


# ── fixtures ────────────────────────────────────────────────────────────────

def _bare_strategy():
    """TMFSpread bare instance with the attrs hydration/inference touches."""
    s = TMFSpread.__new__(TMFSpread)
    s._has_position = False
    s._trade_id = None
    s._near_side = None
    s._far_side = None
    s._near_entry = 0.0
    s._far_entry = 0.0
    s._near_qty = 0
    s._far_qty = 0
    s._released_leg = None
    s._side = None
    s._lifecycle = "OPEN"
    s._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    s._trail_anchor_status = TrailAnchorStatus.READY
    s._single_leg_peak = 0.0
    s._single_leg_nadir = 0.0
    s._single_leg_anchor_price = 0.0
    s._position_session_type = None
    s._max_quote_age_ms = 999999.0
    return s


def _monitor(tmp_path, *, session_id="sess-1", session_type="day"):
    m = FuturesMonitor.__new__(FuturesMonitor)
    m.live_trading = True
    m.dry_run = False
    m.contract = SimpleNamespace(code="TMFH6")
    m.far_contract = SimpleNamespace(code="TMFI6")
    m._execution_context = SimpleNamespace(
        requested_mode="live", effective_mode="live_quarantined",
        session_id=session_id, live_order_allowed=False,
        exit_only_capability=None)
    m.session_type = session_type
    m._live_broker_authority_at = 0.0
    m._broker_position_observed = False
    m._live_broker_flat_proven = False
    m._broker_authority_degraded = False
    # read-only capture/reconcile helpers mocked for unit isolation
    m._reconcile_local_orders_from_snapshot = lambda snap: None
    m._persist_current_session_canonical = lambda snap: None
    m._write_live_session_upl = lambda pos, ctx: None
    return m


def _snap(positions, open_orders=(), capture="OK", session_id="sess-1",
          hash_="snap-hash-1"):
    return {
        "fetch_status": {"capture": capture},
        "account_identity_hash": "acct-1",
        "canonical_input_hash": hash_,
        "session_id": session_id,
        "positions": positions,
        "open_orders": list(open_orders),
    }


_REMAINING = [{"account": "futures", "code": "TMFI6", "quantity": 1,
               "direction": "Action.Buy", "avg_cost": 46058.0}]


def _order_mgr(orders):
    """orders: list of dicts (order_id/broker_order_id/symbol/strategy)."""
    return SimpleNamespace(
        active_orders={o["order_id"]: o for o in orders},
        completed=[])


def _release_order(order_id="ORD-1", broker="e8c20826", symbol="TMFH6"):
    return {"order_id": order_id, "broker_order_id": broker,
            "symbol": symbol, "strategy": "MTS_RELEASE"}


# ── Part 1: broker-confirmed single-leg inference ───────────────────────────

def test_reconciled_single_leg_qualified_infers_lifecycle(tmp_path):
    """Exactly one MTS_RELEASE identity + released absent + remaining valid
    -> SINGLE_LEG lifecycle + PENDING_REANCHOR + recovery identity persisted,
    no synthetic release price/PnL."""
    m = _monitor(tmp_path)
    snap = _snap(_REMAINING, session_id="sess-1")
    m._capture_post_startup_snapshot = lambda: snap
    m.order_mgr = _order_mgr([_release_order()])
    s = _bare_strategy()
    auth = m._refresh_live_broker_authority(s)

    assert auth is not None and auth.status is MtsAuthority.SINGLE_LEG
    # lifecycle inference (no synthetic price/PnL anywhere)
    assert s._released_leg == "near"            # TMFH6 absent -> near released
    assert s._side == "LONG"                    # remaining TMFI6
    assert s._lifecycle_oca.phase == PositionPhase.SINGLE_LEG
    assert s._lifecycle_oca.release_group.status == ReleaseGroupStatus.FILLED
    assert s._lifecycle_oca.release_group.filled_leg is not None
    assert s._lifecycle_oca.trail_group.status == TrailGroupStatus.ARMED
    assert s._lifecycle_oca.trail_group.remaining_leg is not None
    assert getattr(s, "_release_price", None) in (None, 0.0)  # no synthetic
    # PENDING_REANCHOR + zero extrema (RESTART_BASELINE takes over)
    assert s._trail_anchor_status == TrailAnchorStatus.PENDING_REANCHOR
    assert s._single_leg_peak == 0.0
    assert s._single_leg_nadir == 0.0
    assert s._single_leg_anchor_price == 0.0
    # position session recorded for the baseline session-match gate
    assert s._position_session_type == "day"
    # recovery identity persisted
    assert s._broker_recovery_source == "broker_reconciliation"
    assert s._broker_recovery_order_id == "ORD-1"
    assert s._broker_recovery_broker_order_id == "e8c20826"
    assert s._broker_recovery_snapshot_hash == "snap-hash-1"
    # entry remains blocked (broker position observed)
    assert m._broker_position_observed is True


def test_reconciled_single_leg_duplicate_stale_pending_dedupes(tmp_path):
    """The same broker identity captured twice (stale session-cache
    PendingSubmit) dedupes to ONE identity -> still qualified.  Two
    DISTINCT identities are the fail-closed case (next test)."""
    m = _monitor(tmp_path)
    snap = _snap(_REMAINING, open_orders=[
        {"order_id": "e8c20826", "status": "PendingSubmit"},
        {"order_id": "e8c20826", "status": "PendingSubmit"},
    ], session_id="sess-1")
    m._capture_post_startup_snapshot = lambda: snap
    m.order_mgr = _order_mgr([
        _release_order(),
        _release_order(order_id="ORD-1b", broker="e8c20826"),  # same broker id
    ])
    s = _bare_strategy()
    auth = m._refresh_live_broker_authority(s)

    assert auth is not None and auth.status is MtsAuthority.SINGLE_LEG
    assert s._lifecycle_oca.phase == PositionPhase.SINGLE_LEG
    assert s._trail_anchor_status == TrailAnchorStatus.PENDING_REANCHOR
    assert s._broker_recovery_order_id == "ORD-1"


def test_reconciled_single_leg_no_identity_fails_closed(tmp_path):
    """Zero MTS_RELEASE identity -> fields hydrate but NO lifecycle
    inference, NO PENDING_REANCHOR, NO recovery marker."""
    m = _monitor(tmp_path)
    snap = _snap(_REMAINING, session_id="sess-1")
    m._capture_post_startup_snapshot = lambda: snap
    m.order_mgr = _order_mgr([])  # no MTS_RELEASE orders
    s = _bare_strategy()
    auth = m._refresh_live_broker_authority(s)

    assert auth is not None and auth.status is MtsAuthority.SINGLE_LEG
    assert s._lifecycle_oca.phase != PositionPhase.SINGLE_LEG
    assert s._trail_anchor_status != TrailAnchorStatus.PENDING_REANCHOR
    assert not hasattr(s, "_broker_recovery_source")


def test_reconciled_single_leg_ambiguous_identity_fails_closed(tmp_path):
    """TWO distinct MTS_RELEASE identities -> ambiguous -> fail-closed."""
    m = _monitor(tmp_path)
    snap = _snap(_REMAINING, session_id="sess-1")
    m._capture_post_startup_snapshot = lambda: snap
    m.order_mgr = _order_mgr([
        _release_order(order_id="ORD-1", broker="e8c20826"),
        _release_order(order_id="ORD-2", broker="aaabbb11"),
    ])
    s = _bare_strategy()
    auth = m._refresh_live_broker_authority(s)

    assert auth is not None and auth.status is MtsAuthority.SINGLE_LEG
    assert s._lifecycle_oca.phase != PositionPhase.SINGLE_LEG
    assert not hasattr(s, "_broker_recovery_source")


def test_reconciled_single_leg_remaining_invalid_fails_closed(tmp_path):
    """Remaining leg row without valid side/qty/avg_cost -> degraded
    (auth None), zero inference."""
    m = _monitor(tmp_path)
    snap = _snap([{"account": "futures", "code": "TMFI6", "quantity": 1,
                   "direction": "Action.Buy", "avg_cost": 0.0}],
                 session_id="sess-1")
    m._capture_post_startup_snapshot = lambda: snap
    m.order_mgr = _order_mgr([_release_order()])
    s = _bare_strategy()
    auth = m._refresh_live_broker_authority(s)

    assert auth is None
    assert not hasattr(s, "_broker_recovery_source")


def test_reconciled_single_leg_capture_failure_fails_closed(tmp_path):
    m = _monitor(tmp_path)
    snap = _snap(_REMAINING, capture="FAIL", session_id="sess-1")
    m._capture_post_startup_snapshot = lambda: snap
    m.order_mgr = _order_mgr([_release_order()])
    s = _bare_strategy()
    auth = m._refresh_live_broker_authority(s)

    assert auth is None
    assert not hasattr(s, "_broker_recovery_source")


def test_reconciled_single_leg_session_mismatch_fails_closed(tmp_path):
    """Snapshot session_id != execution-context session -> fail-closed
    (fields hydrate but no lifecycle inference)."""
    m = _monitor(tmp_path, session_id="sess-1")
    snap = _snap(_REMAINING, session_id="sess-old")
    m._capture_post_startup_snapshot = lambda: snap
    m.order_mgr = _order_mgr([_release_order()])
    s = _bare_strategy()
    auth = m._refresh_live_broker_authority(s)

    assert auth is not None and auth.status is MtsAuthority.SINGLE_LEG
    assert s._lifecycle_oca.phase != PositionPhase.SINGLE_LEG
    assert not hasattr(s, "_broker_recovery_source")


# ── Part 2: baseline establishment, persistence, restart-safety ─────────────

def _qualified_monitor(tmp_path, strategy, *, session_id="sess-1"):
    """Monitor whose refresh performs the qualified recovery on strategy."""
    m = _monitor(tmp_path, session_id=session_id)
    m._capture_post_startup_snapshot = lambda: _snap(
        _REMAINING, session_id=session_id)
    m.order_mgr = _order_mgr([_release_order()])
    auth = m._refresh_live_broker_authority(strategy)
    assert auth is not None
    # satisfy the post-fill warmup so the normal trail path can run
    strategy._single_leg_entered_mono = time.monotonic() - 5.0
    strategy._single_leg_post_fill_ticks = 2
    return m


def test_reconciled_baseline_established_from_fresh_bbo(tmp_path):
    """Qualified recovery -> PENDING_REANCHOR -> first fresh remaining-leg BBO
    (same session) establishes the baseline via the RESTART_BASELINE gate."""
    s, config = _setup_armed(tmp_path, confirm_ticks=0)
    _qualified_monitor(tmp_path, s)
    assert s._trail_anchor_status == TrailAnchorStatus.PENDING_REANCHOR
    # remaining leg = FAR (released near); asymmetric BBO mid 45910 != close 45900
    bar = _make_bar(near_close=45600, far_close=45900, session_type="day",
                    far_bid=45890.0, far_ask=45930.0, far_tick_age_ms=5.0)
    ctx = StrategyContext(
        market=MarketData(last_bar=bar, ticker="TMF"),
        position=PositionView(size=1), config=config)
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        with patch("strategies.plugins.futures.active.tmf_spread._append_event"):
            result = s.on_bar(ctx)
    assert result is None
    assert s._trail_anchor_status == TrailAnchorStatus.READY
    assert s._single_leg_anchor_price == pytest.approx(45910.0)
    assert s._single_leg_nadir == pytest.approx(45910.0)
    assert s._single_leg_peak == pytest.approx(45910.0)
    assert s._trail_anchor_source == "RESTART_BASELINE_FRESH_BBO"


def test_reconciled_restart_peak_reset(tmp_path):
    """Restart-safe: a NEW process re-infers PENDING_REANCHOR with zero
    extrema — the previous session's peak never survives."""
    # session A: qualified recovery + baseline (peak = 45910)
    s1, config = _setup_armed(tmp_path, confirm_ticks=0)
    _qualified_monitor(tmp_path, s1)
    bar = _make_bar(near_close=45600, far_close=45900, session_type="day",
                    far_bid=45890.0, far_ask=45930.0, far_tick_age_ms=5.0)
    ctx = StrategyContext(
        market=MarketData(last_bar=bar, ticker="TMF"),
        position=PositionView(size=1), config=config)
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        with patch("strategies.plugins.futures.active.tmf_spread._append_event"):
            s1.on_bar(ctx)
    assert s1._single_leg_anchor_price == pytest.approx(45910.0)

    # session B: restart — brand-new strategy + brand-new monitor, same
    # durable evidence (orders file + snapshot) -> re-inference
    s2, _ = _setup_armed(tmp_path, confirm_ticks=0)
    _qualified_monitor(tmp_path, s2)
    assert s2._trail_anchor_status == TrailAnchorStatus.PENDING_REANCHOR
    assert s2._single_leg_peak == 0.0
    assert s2._single_leg_nadir == 0.0
    assert s2._single_leg_anchor_price == 0.0
    assert s2._broker_recovery_source == "broker_reconciliation"


def test_reconciled_state_write_persists_anchor_and_identity(tmp_path):
    """The single-leg state write carries the baseline anchor/peak AND the
    broker-reconciliation identity (restart-safe persistence)."""
    s, config = _setup_armed(tmp_path, confirm_ticks=0)
    _qualified_monitor(tmp_path, s)
    # bar1: baseline tick (PENDING_REANCHOR -> READY, anchor 45910)
    bar1 = _make_bar(near_close=45600, far_close=45900, session_type="day",
                     far_bid=45890.0, far_ask=45930.0, far_tick_age_ms=5.0)
    ctx1 = StrategyContext(
        market=MarketData(last_bar=bar1, ticker="TMF"),
        position=PositionView(size=1), config=config)
    # bar2: benign manage tick (READY single-leg path -> state write)
    bar2 = _make_bar(near_close=45610, far_close=45910, session_type="day",
                     far_bid=45900.0, far_ask=45920.0, far_tick_age_ms=5.0)
    ctx2 = StrategyContext(
        market=MarketData(last_bar=bar2, ticker="TMF"),
        position=PositionView(size=1), config=config)
    calls = {}

    def _cap(**kw):
        calls.update(kw)

    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state",
               side_effect=_cap):
        with patch("strategies.plugins.futures.active.tmf_spread._append_event"):
            s.on_bar(ctx1)  # baseline tick
            s.on_bar(ctx2)  # manage tick
    # a state write happened and carries the anchor + recovery identity
    assert calls, "no state write captured"
    assert calls.get("trail_peak") == pytest.approx(45910.0)
    assert calls.get("trail_nadir") == pytest.approx(45910.0)
    assert calls.get("broker_recovery_source") == "broker_reconciliation"
    assert calls.get("broker_recovery_order_id") == "ORD-1"
    assert calls.get("broker_recovery_snapshot_hash") == "snap-hash-1"
