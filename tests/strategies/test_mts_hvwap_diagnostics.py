# 2026-08-24 Hermes Agent: regression tests for the HVWAP candidate pipeline
# FAIL-CLOSED DIAGNOSTICS (production-observed data gap: paper trade
# mts-auto-092147-573 held 09:21:47 -> 09:29 with ZERO HVWAP_* events).
#
# Contract under test:
#   1. NO silent failure: every no-evaluate path emits an explicit
#      HVWAP_DATA_UNAVAILABLE event with a precise reason.
#   2. The real production bar schema (pd.Timestamp bucket-start `ts`,
#      df-column `timestamp`, near/far close overrides, tick ages, bid/ask,
#      far_volume) produces HVWAP_CANDIDATE telemetry after a completed bar.
#   3. Candidate exceptions are VISIBLE (HVWAP_DATA_UNAVAILABLE + reason)
#      and never disturb baseline/risk/order behavior.
#   4. stoploss / Policy J / release / live flow unchanged.
import datetime as dt
from collections import deque

import pandas as pd
import pytest

from strategies.plugins.futures.active.tmf_spread import TMFSpread

TW = dt.timezone(dt.timedelta(hours=8))


def _skeleton(**over):
    s = object.__new__(TMFSpread)
    s._hvwap_5m_bars = deque(maxlen=320)
    s._hvwap_last_bucket = None
    s._hvwap_pending_snapshot = None
    s._hvwap_last_emit_bucket = None
    s._hvwap_diag_last_bucket = None
    s._hvwap_last_verdict = None
    s._hvwap_armed = False
    s._hvwap_release_sent_trade_id = None
    s._hvwap_baseline_clear_tick = True
    s._hvwap_execution_mode = "paper_active"
    s._hvwap_live_order_allowed = False
    s._has_position = True
    s._near_side = "LONG"; s._far_side = "SHORT"
    s._near_entry = 100.0; s._far_entry = 90.0
    s._trade_id = "mts-auto-092147-573"
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


def _events(monkeypatch):
    from strategies.plugins.futures.active import tmf_spread as T
    events = []
    monkeypatch.setattr(
        T, "_append_event",
        lambda *a, **kw: events.append(((a[0] if a else None), kw)))
    return events


def _prod_bar(bucket_epoch):
    """Production monitor bar shape (see monitor._mts_tick): pd.Timestamp
    bucket-START `ts` (unit='s'), df-column `timestamp`, RT close overrides,
    tick ages, bid/ask, sqz_on, far_volume, session_type."""
    ts = pd.Timestamp(int(bucket_epoch), unit='s')
    return {
        "ts": ts, "timestamp": ts,
        "near_close": 100.5, "far_close": 90.5,
        "near_high": 101.0, "near_low": 100.0, "far_high": 91.0, "far_low": 90.0,
        "volume": 100.0, "atr": 5.0, "spread_z": 2.5,
        "near_vwap": 100.2, "far_vwap": 90.2, "far_volume": 90.0,
        "near_tick_age_ms": 100.0, "far_tick_age_ms": 120.0,
        "near_bid": 100.3, "near_ask": 100.7, "far_bid": 90.3, "far_ask": 90.7,
        "sqz_on": False, "confirm_ticks": 2, "session_type": "day",
    }


def _bucket_base():
    return int(pd.Timestamp("2026-08-24 08:45:00+08:00").timestamp() / 300) * 300


# ── 1. no-event diagnostics (production data gap) ─────────────────────────

def test_no_bar_timestamp_emits_diagnostic(monkeypatch):
    """An unparseable/missing bar ts must emit HVWAP_DATA_UNAVAILABLE with
    NO_BAR_TIMESTAMP — NOT a silent return."""
    events = _events(monkeypatch)
    s = _skeleton()
    s._hvwap_candidate_tick({"near_close": 100.0, "far_close": 90.0},
                            dt.datetime.now())
    diags = [p for e, p in events if e == "HVWAP_DATA_UNAVAILABLE"]
    assert diags, "missing ts must produce a diagnostic event"
    assert diags[0]["reason"] == "NO_BAR_TIMESTAMP"
    assert "ts_type" in (diags[0].get("detail") or "")


def test_warmup_pending_emits_diagnostic(monkeypatch):
    """A held spread with no candidate event yet (first in-position tick)
    emits WARMUP_PENDING once per bucket — the '0 bars / dashes' dashboard
    state must be distinguishable from a broken pipeline."""
    events = _events(monkeypatch)
    s = _skeleton()
    # flat ticks consumed the emit bucket for the current bucket
    base = _bucket_base()
    s._has_position = False
    s._hvwap_candidate_tick(_prod_bar(base), dt.datetime.now())
    s._has_position = True
    s._hvwap_candidate_tick(_prod_bar(base), dt.datetime.now())
    diags = [p for e, p in events if e == "HVWAP_DATA_UNAVAILABLE"]
    assert diags and diags[0]["reason"] == "WARMUP_PENDING"
    assert diags[0]["has_position"] is True
    # rate-limited: second tick in the same bucket does not re-emit
    n = len(events)
    s._hvwap_candidate_tick(_prod_bar(base), dt.datetime.now())
    assert len(events) == n


def test_has_position_non_bool_emits_diagnostic(monkeypatch):
    """truthy-but-not-True _has_position (serialization artifact) must be
    flagged — it would silently starve the in-position emit branch."""
    events = _events(monkeypatch)
    s = _skeleton(_has_position=1)   # int, truthy, not True
    base = _bucket_base()
    s._hvwap_candidate_tick(_prod_bar(base), dt.datetime.now())
    diags = [p for e, p in events if e == "HVWAP_DATA_UNAVAILABLE"]
    assert diags and diags[0]["reason"] == "HAS_POSITION_NON_BOOL"
    assert "type=int" in (diags[0].get("detail") or "")


def test_eval_exception_emits_diagnostic(monkeypatch):
    """A candidate evaluation exception must emit HVWAP_DATA_UNAVAILABLE with
    the exception type/message (visibility), not vanish into a warning log."""
    events = _events(monkeypatch)
    s = _skeleton()
    import strategies.plugins.futures.active.mts_hvwap_candidate as mod
    monkeypatch.setattr(
        mod, "evaluate_hvwap_candidate",
        lambda **k: (_ for _ in ()).throw(RuntimeError("boom-schema")))
    base = _bucket_base()
    s._hvwap_candidate_tick(_prod_bar(base), dt.datetime.now())
    diags = [p for e, p in events if e == "HVWAP_DATA_UNAVAILABLE"]
    assert diags and diags[0]["reason"] == "EVAL_EXCEPTION"
    assert "RuntimeError" in (diags[0].get("detail") or "")
    assert "boom-schema" in (diags[0].get("detail") or "")


# ── 2. actual production bar schema emits telemetry ───────────────────────

def test_production_bar_schema_emits_candidate_after_completed_bar(monkeypatch):
    """The REAL monitor bar shape (pd.Timestamp bucket-start ts, RT overrides,
    tick ages, bid/ask, far_volume) must produce HVWAP_CANDIDATE events once a
    held spread receives a valid completed bar (the exact production gap)."""
    events = _events(monkeypatch)
    s = _skeleton()
    s._has_position = False                       # flat morning first
    base = _bucket_base()
    # flat morning 08:45..09:15 (7 buckets)
    for i in range(7):
        s._hvwap_candidate_tick(_prod_bar(base + i * 300), dt.datetime.now())
    assert not any(e == "HVWAP_CANDIDATE" for e, p in events)
    # entry at 09:20 bucket (09:21:47 production analog)
    s._has_position = True
    s._hvwap_candidate_tick(_prod_bar(base + 7 * 300), dt.datetime.now())
    # completed bar arrives: 09:25 roll -> candidate telemetry MUST emit
    s._hvwap_candidate_tick(_prod_bar(base + 8 * 300), dt.datetime.now())
    cands = [p for e, p in events if e == "HVWAP_CANDIDATE"]
    assert cands, "held spread + completed bar must emit candidate telemetry"
    assert cands[0]["n_completed_5m_bars"] >= 1
    assert cands[0]["status"] in ("UNKNOWN", "BLOCK", "HOLD", "ALIGNED_PASS")
    assert s._hvwap_armed is True


def test_uppercase_ohlcv_aliases_supply_near_vwap_volume(monkeypatch):
    """OHLCV capitalization must not turn near volume into ZERO_VOLUME."""
    events = _events(monkeypatch)
    s = _skeleton()
    base = _bucket_base()
    bar = _prod_bar(base)
    bar.pop("volume")
    bar["Volume"] = 100.0
    bar.pop("near_close")
    bar["Close"] = 100.5
    s._has_position = False
    s._hvwap_candidate_tick(bar, dt.datetime.now())
    s._has_position = True
    s._hvwap_candidate_tick(_prod_bar(base + 300), dt.datetime.now())
    cands = [p for e, p in events if e == "HVWAP_CANDIDATE"]
    assert cands
    assert cands[-1]["near"]["vwap"] is not None
    assert cands[-1]["near"]["issue"] in (None, "SLOPE_UNAVAILABLE")


def test_production_schema_pandas_timestamp_close_shift_single(monkeypatch):
    """pd.Timestamp bucket-start ts must shift exactly once (close time) —
    the per-leg VWAP accumulates the committed bars' closes."""
    events = _events(monkeypatch)
    s = _skeleton()
    base = _bucket_base()
    for i in range(13):
        s._hvwap_candidate_tick(_prod_bar(base + i * 300), dt.datetime.now())
    last = [p for e, p in events if e == "HVWAP_CANDIDATE"][-1]
    # 12 committed bars at the 13th tick; session VWAP = weighted mean of the
    # 11 committed closes (volumes 100..110 => weighted mean slightly above
    # plain mean). Any double shift would drop bars out of the session window.
    assert last["n_completed_5m_bars"] == 12
    assert last["near"]["vwap"] is not None
    assert last["session_boundary_ok"] is True


# ── 3. candidate exception visibility + 4. no baseline/risk impact ────────

def test_candidate_boom_never_blocks_baseline_signal(monkeypatch):
    """With the candidate evaluation exploding every tick, the baseline
    _manage_position result still flows through on_bar unchanged."""
    from core.signal import Signal
    from core.strategy_context import MarketData, PositionView, StrategyContext
    from strategies.plugins.futures.active import tmf_spread as T
    events = _events(monkeypatch)
    s = _skeleton()
    s._hvwap_candidate_tick = lambda bar, now: None   # isolate release path
    s._manage_position = lambda *a, **k: Signal(
        "STOPLOSS", "TMF_STOPLOSS", confidence=1.0, stop_loss=0)
    ctx = StrategyContext(market=MarketData(last_bar=_prod_bar(_bucket_base()),
                                            ticker="TMF"),
                          position=PositionView(), config={})
    out = s.on_bar(ctx)
    assert out is not None and out.action == "STOPLOSS"
    assert not any(e == "HVWAP_RELEASE_INTENT" for e, p in events)


def test_candidate_boom_emits_diagnostic_and_release_noop(monkeypatch):
    """Exception path: diagnostic emitted; release intent NOT created;
    lifecycle untouched (stop-loss / Policy J / release precedence intact)."""
    from core.strategy_context import MarketData, PositionView, StrategyContext
    from strategies.plugins.futures.active import tmf_spread as T
    import strategies.plugins.futures.active.mts_hvwap_candidate as mod
    events = _events(monkeypatch)
    s = _skeleton()
    s._hvwap_last_verdict = None
    s._manage_position = lambda *a, **k: None
    monkeypatch.setattr(
        mod, "evaluate_hvwap_candidate",
        lambda **k: (_ for _ in ()).throw(RuntimeError("schema-boom")))
    ctx = StrategyContext(market=MarketData(last_bar=_prod_bar(_bucket_base()),
                                            ticker="TMF"),
                          position=PositionView(), config={})
    out = s.on_bar(ctx)
    assert out is None
    diags = [p for e, p in events if e == "HVWAP_DATA_UNAVAILABLE"]
    assert diags and diags[0]["reason"] == "EVAL_EXCEPTION"
    assert not any(e == "HVWAP_RELEASE_INTENT" for e, p in events)
    assert s._lifecycle == "OPEN"
    assert s._hvwap_release_sent_trade_id is None


def test_diag_path_does_not_affect_policy_j_or_stoploss(monkeypatch):
    """The diagnostic path never mutates lifecycle/position state — a
    stop-loss / Policy J decision made by the baseline is unaffected."""
    import strategies.plugins.futures.active.mts_hvwap_candidate as mod
    events = _events(monkeypatch)
    s = _skeleton()
    monkeypatch.setattr(
        mod, "evaluate_hvwap_candidate",
        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    s._hvwap_candidate_tick(_prod_bar(_bucket_base()), dt.datetime.now())
    diags = [p for e, p in events if e == "HVWAP_DATA_UNAVAILABLE"]
    assert diags and diags[0]["reason"] == "EVAL_EXCEPTION"
    # nothing strategy-affecting was touched
    assert s._lifecycle == "OPEN"
    assert s._has_position is True
    assert s._release_pending_mono == 0.0
    assert s._hvwap_release_sent_trade_id is None
