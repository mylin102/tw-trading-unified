"""RESTART_BASELINE slice (2026-08-18): the post-restart single-leg trail
anchor must be established ONLY from the remaining leg's fresh broker-grade
BBO, in the same trading session, and the baseline is trailing-protection
metadata only — it must never leak into avg_cost / UPL / Policy-J / two-leg
entry time.  Missing / stale / session-mismatch inputs fail closed (stay
PENDING_REANCHOR).  Within one process session the baseline is established
exactly once (idempotent).

Contract (user spec, bounded slice):
  1. single-leg broker-confirmed fresh BBO only
  2. baseline = trailing-protection metadata only, never
     avg_cost / UPL / Policy-J / two-leg entry time
  3. missing / stale / session mismatch -> fail-closed (PENDING_REANCHOR)
  4. same-session idempotence
  5. no deploy / restart / broker mutation (test-only slice)
"""
import json
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from core.strategy_context import StrategyContext, MarketData, PositionView
from strategies.plugins.futures.active.tmf_spread import (
    Leg, PositionLifecycle, PositionPhase, ReleaseGroup, ReleaseGroupStatus,
    TMFSpread, TrailAnchorStatus, TrailGroup, TrailGroupStatus,
)
from tests.strategies.test_tmf_spread_atr import _make_bar, _setup_armed


@pytest.fixture(autouse=True)
def _isolate_mts_state_path(tmp_path, monkeypatch):
    """MTS_STATE_PATH 每測試隔離 — Mini 上 /tmp/test_mts_position_state.json
    是 shared 殘留 (其他 suite / 平行 worktree 寫的), 不隔離會讓 state-file
    路徑誤錨 (已知陷阱, P0-A round2)。"""
    monkeypatch.setenv("MTS_STATE_PATH",
                       str(tmp_path / "mts_position_state.json"))


def _single_leg_pending(tmp_path, *, released_leg="near",
                        broker_flat=False,
                        position_session: str | None = "day",
                        **setup_kw):
    """Restored SINGLE_LEG position in PENDING_REANCHOR (restart shape).

    released_leg="near" -> remaining FAR (side = far_side = LONG in the
    _setup_armed fixture); released_leg="far" -> remaining NEAR (SHORT).
    """
    s, config = _setup_armed(tmp_path, confirm_ticks=0, **setup_kw)
    s._released_leg = released_leg
    s._side = "LONG" if released_leg == "near" else "SHORT"
    s._lifecycle = f"TRAILING_{s._side}"
    _rel_enum = Leg.NEAR if released_leg == "near" else Leg.FAR
    _rem_enum = Leg.FAR if released_leg == "near" else Leg.NEAR
    s._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.COMPLETED,
                                   filled_leg=_rel_enum),
        trail_group=TrailGroup(status=TrailGroupStatus.ARMED,
                               remaining_leg=_rem_enum),
    )
    s._broker_truth_flat = broker_flat
    s._position_session_type = position_session
    s._set_single_leg_extrema(peak=0.0, nadir=0.0)
    s._single_leg_anchor_price = 0.0
    s._trail_anchor_status = TrailAnchorStatus.PENDING_REANCHOR
    s._trail_anchor_source = "RESTORE_RECONSTRUCTION"
    # post-fill warmup already satisfied so the normal trail path can run
    # once the baseline is established
    s._single_leg_entered_mono = time.monotonic() - 5.0
    s._single_leg_post_fill_ticks = 2
    return s, config


def _run_bar(s, config, bar):
    ctx = StrategyContext(
        market=MarketData(last_bar=bar, ticker="TMF"),
        position=PositionView(size=1),
        config=config,
    )
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        with patch("strategies.plugins.futures.active.tmf_spread._append_event"):
            with patch("strategies.plugins.futures.active.tmf_spread._append_fill"):
                return s.on_bar(ctx)


def _skip_reason(s):
    return getattr(getattr(s, "last_eval", None), "skip_reason", "") or ""


# ── 1. single-leg broker-confirmed fresh BBO only ──────────────────────────

def test_restart_baseline_establishes_from_fresh_bbo(tmp_path):
    """released NEAR -> remaining FAR: fresh far BBO mid anchors the trail."""
    s, config = _single_leg_pending(tmp_path, released_leg="near",
                                    position_session="day")
    # BBO mid (45890+45930)/2 = 45910 differs from close 45900 — the anchor
    # must come from the BBO, not the close.
    bar = _make_bar(near_close=45600, far_close=45900, session_type="day",
                    far_bid=45890.0, far_ask=45930.0, far_tick_age_ms=5.0)
    result = _run_bar(s, config, bar)
    assert result is None
    assert s._trail_anchor_status == TrailAnchorStatus.READY
    assert s._single_leg_anchor_price == pytest.approx(45910.0)
    assert s._single_leg_nadir == pytest.approx(45910.0)
    assert s._single_leg_peak == pytest.approx(45910.0)
    assert s._trail_anchor_source == "RESTART_BASELINE_FRESH_BBO"
    assert _skip_reason(s) == "TRAIL_REANCHOR_INITIALIZED"


def test_restart_baseline_uses_remaining_leg_bbo_only(tmp_path):
    """The RELEASED leg's BBO is never read — single-leg BBO only."""
    s, config = _single_leg_pending(tmp_path, released_leg="near",
                                    position_session="day")
    bar = _make_bar(near_close=45600, far_close=45900, session_type="day",
                    near_bid=None, near_ask=0, near_tick_age_ms=999999.0,
                    far_bid=45890.0, far_ask=45930.0, far_tick_age_ms=5.0)
    _run_bar(s, config, bar)
    assert s._trail_anchor_status == TrailAnchorStatus.READY
    assert s._single_leg_anchor_price == pytest.approx(45910.0)
    assert s._trail_anchor_source == "RESTART_BASELINE_FRESH_BBO"


# ── 2. baseline is trailing-protection metadata only ───────────────────────

def test_restart_baseline_is_trailing_metadata_only(tmp_path):
    """Never avg_cost / UPL / Policy-J / two-leg entry time."""
    _MISSING = object()
    s, config = _single_leg_pending(tmp_path, released_leg="near",
                                    position_session="day")
    for _name in ("_entry_guard_start_ms", "_entry_ts_ms"):
        if not hasattr(s, _name):
            setattr(s, _name, None)
    before = {
        "_near_entry": s._near_entry,
        "_far_entry": s._far_entry,
        "_peak_net_exit_pnl_twd": getattr(s, "_peak_net_exit_pnl_twd",
                                          _MISSING),
        "_pj_durable_peak": getattr(s, "_pj_durable_peak", _MISSING),
        "_entry_guard_start_ms": s._entry_guard_start_ms,
        "_entry_ts_ms": s._entry_ts_ms,
        "_entry_ts": s._entry_ts,
        "_mfe_pts": s._mfe_pts,
        "_mae_pts": s._mae_pts,
    }
    bar = _make_bar(near_close=45600, far_close=45900, session_type="day",
                    far_bid=45890.0, far_ask=45930.0, far_tick_age_ms=5.0)
    _run_bar(s, config, bar)
    assert s._trail_anchor_status == TrailAnchorStatus.READY
    assert s._trail_anchor_source == "RESTART_BASELINE_FRESH_BBO"
    for _k, _v in before.items():
        assert getattr(s, _k, _MISSING) == _v, \
            f"RESTART_BASELINE mutated {_k}: {_v!r} -> {getattr(s, _k, _MISSING)!r}"
    # the trail metadata itself IS the baseline
    assert s._single_leg_peak == pytest.approx(45910.0)
    assert s._nadir == pytest.approx(45910.0)


# ── 3. missing / stale / session mismatch fail-closed ──────────────────────

def test_restart_baseline_missing_bbo_fails_closed(tmp_path):
    """released FAR -> remaining NEAR; near BBO absent -> stay PENDING."""
    s, config = _single_leg_pending(tmp_path, released_leg="far",
                                    position_session="day")
    bar = _make_bar(near_close=45600, far_close=45900, session_type="day")
    del bar["near_bid"]
    del bar["near_ask"]
    _run_bar(s, config, bar)
    assert s._trail_anchor_status == TrailAnchorStatus.PENDING_REANCHOR
    assert s._single_leg_anchor_price == 0.0
    assert s._single_leg_nadir == 0.0
    assert _skip_reason(s) == "TRAIL_REANCHOR_BBO_MISSING"


@pytest.mark.parametrize("far_bid,far_ask", [
    (0.0, 45910.0),             # zero bid
    (45890.0, 0.0),             # zero ask
    (float("nan"), 45910.0),    # NaN bid
    (45920.0, 45910.0),         # inverted (ask < bid)
    ("45890", 45910.0),         # non-numeric str bid
    (True, 45910.0),            # bool bid
])
def test_restart_baseline_invalid_bbo_fails_closed(tmp_path, far_bid, far_ask):
    s, config = _single_leg_pending(tmp_path, released_leg="near",
                                    position_session="day")
    bar = _make_bar(near_close=45600, far_close=45900, session_type="day",
                    far_bid=far_bid, far_ask=far_ask, far_tick_age_ms=5.0)
    _run_bar(s, config, bar)
    assert s._trail_anchor_status == TrailAnchorStatus.PENDING_REANCHOR
    assert s._single_leg_anchor_price == 0.0
    assert _skip_reason(s) == "TRAIL_REANCHOR_BBO_INVALID"


def test_restart_baseline_stale_quote_fails_closed(tmp_path):
    s, config = _single_leg_pending(tmp_path, released_leg="near",
                                    position_session="day")
    # max_quote_age_ms is hot-reloaded from config.params on every bar (on_bar)
    config["params"]["max_quote_age_ms"] = 1000.0
    bar = _make_bar(near_close=45600, far_close=45900, session_type="day",
                    far_bid=45890.0, far_ask=45930.0, far_tick_age_ms=5000.0)
    _run_bar(s, config, bar)
    assert s._trail_anchor_status == TrailAnchorStatus.PENDING_REANCHOR
    assert s._single_leg_anchor_price == 0.0
    assert _skip_reason(s) == "TRAIL_REANCHOR_QUOTE_STALE"


def test_restart_baseline_unknown_quote_age_fails_closed(tmp_path):
    """No per-leg age and no bar-level age -> freshness unprovable."""
    s, config = _single_leg_pending(tmp_path, released_leg="near",
                                    position_session="day")
    bar = _make_bar(near_close=45600, far_close=45900, session_type="day",
                    far_bid=45890.0, far_ask=45930.0, far_tick_age_ms=None)
    _run_bar(s, config, bar)
    assert s._trail_anchor_status == TrailAnchorStatus.PENDING_REANCHOR
    assert _skip_reason(s) == "TRAIL_REANCHOR_QUOTE_AGE_UNKNOWN"


def test_restart_baseline_session_mismatch_fails_closed(tmp_path):
    s, config = _single_leg_pending(tmp_path, released_leg="near",
                                    position_session="day")
    bar = _make_bar(near_close=45600, far_close=45900, session_type="night",
                    far_bid=45890.0, far_ask=45930.0, far_tick_age_ms=5.0)
    _run_bar(s, config, bar)
    assert s._trail_anchor_status == TrailAnchorStatus.PENDING_REANCHOR
    assert _skip_reason(s) == "TRAIL_REANCHOR_SESSION_MISMATCH"


def test_restart_baseline_unknown_position_session_fails_closed(tmp_path):
    s, config = _single_leg_pending(tmp_path, released_leg="near",
                                    position_session=None)
    bar = _make_bar(near_close=45600, far_close=45900, session_type="day",
                    far_bid=45890.0, far_ask=45930.0, far_tick_age_ms=5.0)
    _run_bar(s, config, bar)
    assert s._trail_anchor_status == TrailAnchorStatus.PENDING_REANCHOR
    assert _skip_reason(s) == "TRAIL_REANCHOR_SESSION_UNKNOWN"


def test_restart_baseline_not_broker_confirmed_fails_closed(tmp_path):
    s, config = _single_leg_pending(tmp_path, released_leg="near",
                                    broker_flat=True)
    bar = _make_bar(near_close=45600, far_close=45900, session_type="day",
                    far_bid=45890.0, far_ask=45930.0, far_tick_age_ms=5.0)
    _run_bar(s, config, bar)
    assert s._trail_anchor_status == TrailAnchorStatus.PENDING_REANCHOR
    assert s._single_leg_anchor_price == 0.0
    assert _skip_reason(s) == "TRAIL_REANCHOR_NOT_BROKER_CONFIRMED"


def test_restart_baseline_not_single_leg_fails_closed(tmp_path):
    """released_leg missing -> remaining leg unknowable -> no anchor (a
    default NEAR derivation would be cross-leg contamination)."""
    s, config = _single_leg_pending(tmp_path, released_leg="near",
                                    position_session="day")
    s._released_leg = None
    bar = _make_bar(near_close=45600, far_close=45900, session_type="day",
                    far_bid=45890.0, far_ask=45930.0, far_tick_age_ms=5.0)
    _run_bar(s, config, bar)
    assert s._trail_anchor_status == TrailAnchorStatus.PENDING_REANCHOR
    assert _skip_reason(s) == "TRAIL_REANCHOR_NOT_SINGLE_LEG"


# ── 4. same-session idempotence ────────────────────────────────────────────

def test_restart_baseline_same_session_idempotent(tmp_path):
    """The baseline is established exactly once; later fresh quotes do not
    re-anchor (the normal trail path owns subsequent updates)."""
    s, config = _single_leg_pending(tmp_path, released_leg="near",
                                    position_session="day")
    bar = _make_bar(near_close=45600, far_close=45900, session_type="day",
                    far_bid=45890.0, far_ask=45930.0, far_tick_age_ms=5.0)
    _run_bar(s, config, bar)
    assert s._trail_anchor_status == TrailAnchorStatus.READY
    assert s._trail_anchor_source == "RESTART_BASELINE_FRESH_BBO"
    _anchor = s._single_leg_anchor_price
    _ns = s._single_leg_anchor_event_time_ns
    _src = s._trail_anchor_source
    # same session, different (still fresh) BBO
    bar2 = _make_bar(near_close=45600, far_close=45900, session_type="day",
                     far_bid=45900.0, far_ask=45940.0, far_tick_age_ms=5.0)
    _run_bar(s, config, bar2)
    assert s._trail_anchor_status == TrailAnchorStatus.READY
    assert s._single_leg_anchor_price == _anchor
    assert s._single_leg_anchor_event_time_ns == _ns
    assert s._trail_anchor_source == _src


# ── 5. position session is recorded at the PENDING_REANCHOR sources ────────

def test_restore_records_position_session_from_fills(tmp_path, monkeypatch):
    """Fill-log reconstruction must record the position's trading session so
    the baseline gate can reject quotes from a different session."""
    from strategies.plugins.futures.active import tmf_spread as _m

    _now = datetime.now()
    rows = [
        {"fill_type": "ENTRY", "trade_id": "t-restart-1", "leg": "NEAR",
         "price": 45700.0, "side": "SHORT", "qty": 1,
         "timestamp": (_now - timedelta(minutes=30)).isoformat(),
         "session": "day"},
        {"fill_type": "ENTRY", "trade_id": "t-restart-1", "leg": "FAR",
         "price": 46000.0, "side": "LONG", "qty": 1,
         "timestamp": (_now - timedelta(minutes=29)).isoformat(),
         "session": "day"},
        {"fill_type": "RELEASE", "trade_id": "t-restart-1", "leg": "NEAR",
         "price": 45600.0, "side": "BUY", "qty": 1,
         "timestamp": (_now - timedelta(minutes=5)).isoformat(),
         "session": "night"},
    ]
    fills = tmp_path / "fills.jsonl"
    fills.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(_m, "_MTS_FILL_LOG", str(fills))

    s, config = _setup_armed(tmp_path, confirm_ticks=0)
    s._broker_truth_flat = False  # broker/fills truth confirms the position
    ok = s._restore_position_state()
    assert ok is True
    assert s._has_position is True
    assert s._released_leg == "near"
    assert s._trail_anchor_status == TrailAnchorStatus.PENDING_REANCHOR
    # session from the release fill (night) wins over the entry fills (day)
    assert s._position_session_type == "night"


def test_sync_release_records_position_session(tmp_path):
    """The release-fill path records the fill-time session when no
    remaining-leg price is available (PENDING_REANCHOR restart shape)."""
    s, config = _setup_armed(tmp_path, confirm_ticks=0)
    s._broker_truth_flat = False
    with patch("strategies.plugins.futures.active.tmf_spread._session_label",
               return_value="night"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_event"):
        s.sync_release(leg="near", price=0.0)  # no rem price -> PENDING
    assert s._released_leg == "near"
    assert s._trail_anchor_status == TrailAnchorStatus.PENDING_REANCHOR
    assert s._position_session_type == "night"
