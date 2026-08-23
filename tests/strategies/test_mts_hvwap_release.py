# 2026-08-23 Hermes Agent: regression tests for the paper-only direct VWAP
# release wiring (MTS 2.0 Hierarchical VWAP candidate -> existing paper
# lifecycle/order simulator). Covers:
#   1. paper-only pass        - ALIGNED_PASS creates ONE paper release intent
#   2. live fail-closed       - live modes / live_order_allowed never release
#   3. non-pass no-op         - UNKNOWN/BLOCK/HOLD verdicts emit nothing
#   4. risk exit precedence   - baseline decision / lifecycle / timers win
#   5. duplicate prevention   - one intent per trade; lifecycle blocks repeats
#   6. existing spread state  - wrong phase / released leg / flat / manual exit
#   7. fresh-bar gate         - no retroactive release on startup
#   8. defensive quote gates  - stale / invalid / wide quotes block
#   9. on_bar integration     - baseline result wins; candidate only when clear
import datetime as dt
from collections import deque

import pytest

from strategies.plugins.futures.active.tmf_spread import (
    PositionPhase,
    ReleaseGroupStatus,
    Side,
    TMFSpread,
)

TW = dt.timezone(dt.timedelta(hours=8))


def ts(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=TW).timestamp()


def _skeleton(**over):
    s = object.__new__(TMFSpread)
    s._hvwap_5m_bars = deque(maxlen=320)
    s._hvwap_last_bucket = None
    s._hvwap_pending_snapshot = None
    s._hvwap_last_emit_bucket = None
    s._hvwap_last_verdict = None
    s._hvwap_armed = True
    s._hvwap_release_sent_trade_id = None
    s._hvwap_baseline_clear_tick = True
    s._hvwap_execution_mode = "paper_active"
    s._hvwap_live_order_allowed = False
    s._has_position = True
    s._near_side = "LONG"; s._far_side = "SHORT"
    s._near_entry = 100.0; s._far_entry = 90.0
    s._trade_id = "mts-test-001"
    s._ticker = "TMF"
    s._lifecycle = "OPEN"
    s._released_leg = None
    s._manual_exit_requested = False
    s._release_pending_mono = 0.0
    s._release_near_ticks = 0
    s._release_far_ticks = 0
    s._max_quote_age_ms = 10_000.0
    s._max_spread_width = 50.0
    s._lifecycle_oca = None
    s._trend_confirmed_snapshot = None
    s._position_session_type = None
    s._last_exit_ts = None
    s._set_eval = lambda **k: None
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _spread_lifecycle(rg_status=ReleaseGroupStatus.ARMED):
    from strategies.plugins.futures.active.mts_lifecycle_adapter import PositionLifecycle
    lc = PositionLifecycle(phase=PositionPhase.SPREAD)
    lc.release_group.status = rg_status
    return lc


def _pass_verdict():
    """A minimal ALIGNED_PASS-shaped verdict (real dataclass)."""
    from strategies.plugins.futures.active.mts_hvwap_candidate import (
        HvwapStatus, LegVwapSource, Regime60m, Signal15m,
        HvwapCandidateVerdict, LegVwapState,
    )
    return HvwapCandidateVerdict(
        decision_ts="2026-08-22T18:00:00",
        session_label="2026-08-22",
        status=HvwapStatus.ALIGNED_PASS,
        block_reason=None,
        regime_60m=Regime60m.BULLISH_TREND,
        signal_15m=Signal15m.CONFIRMED_CONTINUATION,
        consecutive_confirmed_bars=2,
        bars_complete=True,
        session_boundary_ok=True,
        n_completed_5m_bars=12,
        near=LegVwapState(leg="NEAR", side="LONG", price=105.0, vwap=100.0,
                          vwap_source=LegVwapSource.SESSION_ACCUMULATED,
                          slope=0.01, atr_15m=5.0,
                          atr_normalized_distance=1.0, is_overextended=False,
                          aligned=True, issue=None),
        far=LegVwapState(leg="FAR", side="SHORT", price=95.0, vwap=100.0,
                         vwap_source=LegVwapSource.SESSION_ACCUMULATED,
                         slope=-0.01, atr_15m=5.0,
                         atr_normalized_distance=1.0, is_overextended=False,
                         aligned=True, issue=None),
        retained_direction="BULLISH",
        hypothetical_release_leg="FAR",
        position_phase="SPREAD",
        quote_age_ms=100.0,
        max_quote_age_ms=10_000.0,
    )


def _bar(**over):
    b = {
        "ts": ts(2026, 8, 22, 18, 0),          # bucket-START
        "near_close": 105.0, "far_close": 95.0,
        "volume": 100.0, "far_volume": 90.0,
        "atr": 5.0,
        "near_tick_age_ms": 100.0, "far_tick_age_ms": 120.0,
        "near_bid": 104.8, "near_ask": 105.2,
        "far_bid": 94.8, "far_ask": 95.2,
        "spread_z": 2.5,
    }
    b.update(over)
    return b


def _events(monkeypatch):
    from strategies.plugins.futures.active import tmf_spread as T
    events = []
    monkeypatch.setattr(
        T, "_append_event",
        lambda *a, **kw: events.append(((a[0] if a else None), kw)))
    return events


# ── 1. paper-only pass ────────────────────────────────────────────────────

def test_paper_active_aligned_pass_emits_release_intent(monkeypatch):
    events = _events(monkeypatch)
    s = _skeleton()
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    sig = s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0)
    assert sig is not None
    assert sig.action == "PARTIAL_EXIT"
    assert "RELEASE_FAR" in sig.reason            # hypothetical release leg FAR
    intents = [p for e, p in events if e == "HVWAP_RELEASE_INTENT"]
    assert len(intents) == 1
    assert intents[0]["source"] == "HVWAP_CANDIDATE"
    assert intents[0]["release_leg"] == "FAR"
    assert intents[0]["status"] == "ALIGNED_PASS"
    assert intents[0]["decision_ts"] == "2026-08-22T18:00:00"
    assert intents[0]["regime_60m"] == "BULLISH_TREND"
    # release state applied through the existing lifecycle path
    assert s._lifecycle == "RELEASE_FAR"
    assert s._lifecycle_oca.release_group.status == ReleaseGroupStatus.TRIGGERED
    assert s._hvwap_release_sent_trade_id == s._trade_id
    assert s._release_price == 95.0


def test_paper_mode_env_aligned_pass(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    events = _events(monkeypatch)
    s = _skeleton(_hvwap_execution_mode=None)     # env-only activation
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    sig = s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0)
    assert sig is not None
    assert any(e == "HVWAP_RELEASE_INTENT" for e, p in events)
    monkeypatch.delenv("PAPER_MODE", raising=False)


def test_unknown_mode_fails_closed(monkeypatch):
    events = _events(monkeypatch)
    s = _skeleton(_hvwap_execution_mode=None)     # no mode, no env
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    sig = s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0)
    assert sig is None
    blocked = [p for e, p in events if e == "HVWAP_RELEASE_BLOCKED"]
    assert blocked and blocked[0]["reason"] == "MODE_UNKNOWN"
    assert s._lifecycle == "OPEN"                 # nothing mutated


# ── 2. live fail-closed ───────────────────────────────────────────────────

@pytest.mark.parametrize("mode,live_allowed", [
    ("live_ready", False), ("live_active", False), ("live_quarantined", False),
    ("paper_active", True),                       # live_order_allowed trumps mode
    ("reconciled_exit_only", False),
])
def test_live_and_non_paper_modes_fail_closed(monkeypatch, mode, live_allowed):
    events = _events(monkeypatch)
    s = _skeleton(_hvwap_execution_mode=mode, _hvwap_live_order_allowed=live_allowed)
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    sig = s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0)
    assert sig is None
    blocked = [p for e, p in events if e == "HVWAP_RELEASE_BLOCKED"]
    assert blocked and blocked[0]["reason"].startswith(
        "LIVE" if mode.startswith("live") or live_allowed else "MODE")
    assert s._lifecycle == "OPEN"
    assert s._hvwap_release_sent_trade_id is None


def test_live_fail_closed_even_with_paper_env(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    s = _skeleton(_hvwap_execution_mode="live_ready")
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    assert s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0) is None
    monkeypatch.delenv("PAPER_MODE", raising=False)


# ── 3. non-pass no-op ─────────────────────────────────────────────────────

def test_non_pass_verdicts_emit_nothing(monkeypatch):
    from strategies.plugins.futures.active.mts_hvwap_candidate import (
        HvwapStatus,
    )
    for st in (HvwapStatus.UNKNOWN, HvwapStatus.BLOCK, HvwapStatus.HOLD):
        events = _events(monkeypatch)
        v = _pass_verdict()
        v = type(v)(
            decision_ts=v.decision_ts, session_label=v.session_label,
            status=st, block_reason="X", regime_60m=v.regime_60m,
            signal_15m=v.signal_15m,
            consecutive_confirmed_bars=v.consecutive_confirmed_bars,
            bars_complete=v.bars_complete,
            session_boundary_ok=v.session_boundary_ok,
            n_completed_5m_bars=v.n_completed_5m_bars,
            near=v.near, far=v.far,
            retained_direction=v.retained_direction,
            hypothetical_release_leg=v.hypothetical_release_leg,
            position_phase=v.position_phase, quote_age_ms=v.quote_age_ms,
            max_quote_age_ms=v.max_quote_age_ms)
        s = _skeleton(_hvwap_last_verdict=v)
        s._lifecycle_oca = _spread_lifecycle()
        sig = s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0)
        assert sig is None
        assert not any(e == "HVWAP_RELEASE_INTENT" for e, p in events)
        assert s._lifecycle == "OPEN"


# ── 4. risk exit precedence ───────────────────────────────────────────────

def test_baseline_decision_present_blocks_candidate(monkeypatch):
    """When the baseline lifecycle adapter produced ANY decision this tick,
    the candidate must not release (risk exit precedence)."""
    events = _events(monkeypatch)
    s = _skeleton(_hvwap_baseline_clear_tick=False)   # baseline spoke
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    assert s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0) is None
    assert s._lifecycle == "OPEN"


def test_release_inflight_timer_blocks_candidate(monkeypatch):
    """A baseline release attempt in progress (timer started / tick counters)
    must block the candidate even when the lifecycle is still OPEN."""
    for field in ("_release_pending_mono", "_release_near_ticks", "_release_far_ticks"):
        over = {field: 1.0} if field.endswith("mono") else {field: 1}
        s = _skeleton(**over)
        s._lifecycle_oca = _spread_lifecycle()
        s._hvwap_last_verdict = _pass_verdict()
        assert s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0) is None


def test_lifecycle_not_open_blocks_candidate(monkeypatch):
    for lc in ("RELEASE_NEAR", "RELEASE_FAR", "EXITING", "SUBMITTING", "TRAILING_LONG"):
        s = _skeleton(_lifecycle=lc)
        s._lifecycle_oca = _spread_lifecycle()
        s._hvwap_last_verdict = _pass_verdict()
        assert s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0) is None


# ── 5. duplicate prevention ───────────────────────────────────────────────

def test_duplicate_intent_prevented(monkeypatch):
    events = _events(monkeypatch)
    s = _skeleton()
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    first = s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0)
    assert first is not None
    # second call: sentinel set + lifecycle RELEASE_FAR + release_group TRIGGERED
    second = s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0)
    assert second is None
    assert sum(1 for e, p in events if e == "HVWAP_RELEASE_INTENT") == 1


def test_new_trade_after_reset_can_release(monkeypatch):
    events = _events(monkeypatch)
    s = _skeleton()
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    assert s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0) is not None
    # simulate flat + re-entry (monitor calls _reset on confirmed fill)
    s._reset(reason="test")
    s._lifecycle_oca = _spread_lifecycle()
    s._has_position = True
    s._lifecycle = "OPEN"                          # re-entry normalizes lifecycle
    s._near_side = "LONG"; s._far_side = "SHORT"
    s._near_entry = 110.0; s._far_entry = 100.0
    s._trade_id = "mts-test-002"
    s._hvwap_last_verdict = _pass_verdict()
    sig = s._hvwap_maybe_paper_release(_bar(near_close=115.0, far_close=105.0),
                                       dt.datetime.now(tz=TW), 115.0, 105.0)
    assert sig is not None                       # sentinel cleared by _reset
    assert s._hvwap_release_sent_trade_id == "mts-test-002"


# ── 6. existing spread state ──────────────────────────────────────────────

def test_not_spread_phase_blocks(monkeypatch):
    from strategies.plugins.futures.active.mts_lifecycle_adapter import PositionLifecycle
    lc = PositionLifecycle(phase=PositionPhase.SINGLE_LEG)
    lc.release_group.status = ReleaseGroupStatus.ARMED
    s = _skeleton(_lifecycle_oca=lc)
    s._hvwap_last_verdict = _pass_verdict()
    assert s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0) is None


def test_release_group_not_armed_blocks(monkeypatch):
    for st in (ReleaseGroupStatus.TRIGGERED, ReleaseGroupStatus.SUBMITTED,
               ReleaseGroupStatus.FILLED, ReleaseGroupStatus.COMPLETED):
        s = _skeleton(_lifecycle_oca=_spread_lifecycle(rg_status=st))
        s._hvwap_last_verdict = _pass_verdict()
        assert s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0) is None


def test_released_leg_or_flat_blocks(monkeypatch):
    s = _skeleton(_released_leg="near")
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    assert s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0) is None
    s2 = _skeleton(_has_position=False)
    s2._lifecycle_oca = _spread_lifecycle()
    s2._hvwap_last_verdict = _pass_verdict()
    assert s2._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0) is None


def test_manual_exit_requested_blocks(monkeypatch):
    s = _skeleton(_manual_exit_requested=True)
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    assert s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0) is None


def test_exit_owner_present_blocks(monkeypatch):
    s = _skeleton()
    lc = _spread_lifecycle()
    lc.exit_owner = "POLICY_J"
    s._lifecycle_oca = lc
    s._hvwap_last_verdict = _pass_verdict()
    assert s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0) is None


# ── 7. fresh-bar gate (no retroactive release on startup) ─────────────────

def test_not_armed_blocks_startup_release(monkeypatch):
    events = _events(monkeypatch)
    s = _skeleton(_hvwap_armed=False)             # no completed bar yet (startup)
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    assert s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 95.0) is None
    assert not any(e == "HVWAP_RELEASE_INTENT" for e, p in events)
    assert s._lifecycle == "OPEN"


def test_armed_only_after_first_committed_bar(monkeypatch):
    """_hvwap_candidate_tick arms the release path only after the first
    completed bar is committed (bucket roll) — an old verdict cannot fire."""
    events = _events(monkeypatch)
    s = _skeleton(_hvwap_armed=False)
    s._lifecycle_oca = _spread_lifecycle()
    base = ts(2026, 8, 22, 15, 0)
    # first tick: no bar committed yet -> not armed
    s._hvwap_candidate_tick(_bar(ts=base, near_close=100.0, far_close=90.0),
                            dt.datetime.fromtimestamp(base, tz=TW))
    assert s._hvwap_armed is False
    # second bucket: first bar committed -> armed
    s._hvwap_candidate_tick(_bar(ts=base + 300, near_close=100.5, far_close=90.5),
                            dt.datetime.fromtimestamp(base + 300, tz=TW))
    assert s._hvwap_armed is True


# ── 8. defensive quote gates ──────────────────────────────────────────────

def test_stale_released_leg_quote_blocks(monkeypatch):
    events = _events(monkeypatch)
    s = _skeleton()
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    # FAR is the release leg -> far_tick_age_ms stale
    sig = s._hvwap_maybe_paper_release(
        _bar(far_tick_age_ms=99_999.0), dt.datetime.now(tz=TW), 105.0, 95.0)
    assert sig is None
    blocked = [p for e, p in events if e == "HVWAP_RELEASE_BLOCKED"]
    assert blocked and blocked[0]["reason"] == "STALE_QUOTE"


def test_invalid_or_wide_quote_blocks(monkeypatch):
    events = _events(monkeypatch)
    s = _skeleton()
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    # wide far quote (release leg FAR)
    sig = s._hvwap_maybe_paper_release(
        _bar(far_bid=90.0, far_ask=150.0), dt.datetime.now(tz=TW), 105.0, 95.0)
    assert sig is None
    # invalid (non-numeric) far quote
    sig2 = s._hvwap_maybe_paper_release(
        _bar(far_bid="bad", far_ask=95.2), dt.datetime.now(tz=TW), 105.0, 95.0)
    assert sig2 is None
    assert any(p.get("reason") == "QUOTE_INVALID_OR_WIDE"
               for e, p in events if e == "HVWAP_RELEASE_BLOCKED")


def test_zero_exit_price_blocks(monkeypatch):
    s = _skeleton()
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    assert s._hvwap_maybe_paper_release(_bar(), dt.datetime.now(tz=TW), 105.0, 0.0) is None


# ── 9. on_bar integration ─────────────────────────────────────────────────

def _noop_candidate_tick(s):
    """Prevent the real candidate tick from overwriting the injected verdict
    with an UNKNOWN (empty deque) — keeps the tests focused on the release
    wiring while the on_bar flow still exercises the seam call site."""
    s._hvwap_candidate_tick = lambda bar, now: None


def test_on_bar_baseline_signal_wins_over_candidate(monkeypatch):
    """When _manage_position returns a baseline signal, the candidate never
    fires (even with an ALIGNED_PASS verdict + paper mode)."""
    from core.signal import Signal
    from core.strategy_context import MarketData, PositionView, StrategyContext
    from strategies.plugins.futures.active import tmf_spread as T
    events = _events(monkeypatch)
    s = _skeleton()
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    _noop_candidate_tick(s)
    baseline_sig = Signal("PARTIAL_EXIT", "TMF_RELEASE_NEAR", confidence=0.4)
    s._manage_position = lambda *a, **k: baseline_sig
    ctx = StrategyContext(market=MarketData(last_bar=_bar(), ticker="TMF"),
                          position=PositionView(), config={})
    out = s.on_bar(ctx)
    assert out is baseline_sig
    assert not any(e == "HVWAP_RELEASE_INTENT" for e, p in events)


def test_on_bar_candidate_fires_when_baseline_clear(monkeypatch):
    """_manage_position returns None + baseline-clear marker -> the candidate
    paper release signal is returned."""
    from core.strategy_context import MarketData, PositionView, StrategyContext
    from strategies.plugins.futures.active import tmf_spread as T
    events = _events(monkeypatch)
    s = _skeleton()
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    _noop_candidate_tick(s)
    s._manage_position = lambda *a, **k: None      # baseline took no action
    ctx = StrategyContext(market=MarketData(last_bar=_bar(), ticker="TMF"),
                          position=PositionView(), config={})
    out = s.on_bar(ctx)
    assert out is not None
    assert out.action == "PARTIAL_EXIT"
    assert "RELEASE_FAR" in out.reason
    assert any(e == "HVWAP_RELEASE_INTENT" for e, p in events)


def test_on_bar_baseline_not_clear_blocks_candidate(monkeypatch):
    """Baseline produced a decision (marker False) -> candidate never fires."""
    from core.strategy_context import MarketData, PositionView, StrategyContext
    from strategies.plugins.futures.active import tmf_spread as T
    events = _events(monkeypatch)
    s = _skeleton(_hvwap_baseline_clear_tick=False)
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    _noop_candidate_tick(s)
    s._manage_position = lambda *a, **k: None
    ctx = StrategyContext(market=MarketData(last_bar=_bar(), ticker="TMF"),
                          position=PositionView(), config={})
    out = s.on_bar(ctx)
    assert out is None
    assert not any(e == "HVWAP_RELEASE_INTENT" for e, p in events)


def test_on_bar_candidate_exception_never_blocks(monkeypatch):
    """A candidate failure inside on_bar is swallowed (fail-closed); the
    baseline flow is intact and no release intent is emitted."""
    from core.strategy_context import MarketData, PositionView, StrategyContext
    from strategies.plugins.futures.active import tmf_spread as T
    events = _events(monkeypatch)
    s = _skeleton()
    s._lifecycle_oca = _spread_lifecycle()
    s._hvwap_last_verdict = _pass_verdict()
    _noop_candidate_tick(s)
    s._manage_position = lambda *a, **k: None
    s._hvwap_paper_gate = lambda: (_ for _ in ()).throw(RuntimeError("gate boom"))
    ctx = StrategyContext(market=MarketData(last_bar=_bar(), ticker="TMF"),
                          position=PositionView(), config={})
    out = s.on_bar(ctx)
    assert out is None                           # candidate failed closed
    assert not any(e == "HVWAP_RELEASE_INTENT" for e, p in events)
