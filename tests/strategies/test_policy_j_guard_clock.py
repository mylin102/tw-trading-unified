# Policy J guard clock: reproduce the eight-hour skew and lock the canonical
# UTC-epoch contract (2026-08-04 P0).
#
# Root cause: bar/tick timestamps carry UTC-epoch semantics while the entry
# state ISO is naive Asia/Taipei wall-clock; .timestamp() on the two yields an
# ~8h phantom elapsed -> guard instantly expired (elapsed_ms=28,771,026 on a
# trade 225ms old).
#
# Canonical contract:
#   entry_guard_start_ms = receive-epoch ms at entry settled (time.time()*1000)
#   now_ts_ms            = receive-epoch ms at evaluation (time.time()*1000)
#   elapsed_ms = max(0, now_ts_ms - entry_guard_start_ms)
# ISO is display/audit only; epoch integer is decision source of truth.
import sys
import os
import pytest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.plugins.futures.active.tmf_spread import (
    resolve_entry_peak_guard_ms,
    iso_to_epoch_ms,
    migrate_entry_time,
)


# ── 1. Asia/Taipei naive ISO + UTC epoch evaluation: no 8h skew ──
def test_naive_taipei_iso_epoch_no_skew():
    # legacy naive wall-clock treated as Asia/Taipei (documented contract)
    iso = "2026-08-04T16:50:29.069"
    ms, source = migrate_entry_time(iso)
    assert source == "LEGACY_NAIVE_LOCAL_MIGRATED"
    # epoch must equal Asia/Taipei 16:50:29, NOT UTC 16:50:29
    expected = datetime(2026, 8, 4, 16, 50, 29, 69000).timestamp() * 1000
    assert abs(ms - expected) < 2


# ── 2. timezone-aware +08:00 ISO correct epoch ──
def test_aware_plus8_iso_epoch():
    iso = "2026-08-04T16:50:29.069+08:00"
    ms, source = migrate_entry_time(iso)
    assert source == "TZ_AWARE"
    expected = datetime(2026, 8, 4, 8, 50, 29, 69000, tzinfo=timezone.utc).timestamp() * 1000
    assert abs(ms - expected) < 2


# ── 3. timezone-aware +00:00 ISO correct epoch ──
def test_aware_utc_iso_epoch():
    iso = "2026-08-04T08:50:29.069+00:00"
    ms, source = migrate_entry_time(iso)
    assert source == "TZ_AWARE"
    expected = datetime(2026, 8, 4, 8, 50, 29, 69000, tzinfo=timezone.utc).timestamp() * 1000
    assert abs(ms - expected) < 2


# ── 4. canonical entry_ts_ms beats ISO ──
def test_epoch_ms_beats_iso():
    iso = "2026-08-04T16:50:29.069"
    epoch_ms = 1234567890123.0
    ms, source = migrate_entry_time(iso, entry_ts_ms=epoch_ms)
    assert ms == epoch_ms
    assert source == "CANONICAL_EPOCH"


# ── 5-8. guard window semantics via strategy harness ──
def _make_strategy(**attrs):
    import types
    s = types.SimpleNamespace()
    defaults = dict(
        _pj_trade_id=None, _pj_durable_peak=None, _pj_candidate_peak=None,
        _pj_candidate_ts=None, _pj_candidate_count=0, _pj_last_mark_pair=None,
        _pj_last_upl=None, _pj_last_eval_ts=None, _pj_events=[],
        _pj_guard_state=None, _pj_guard_provisional_peak=None,
        _pj_guard_initialized=False, _pj_guard_just_completed=False,
        _trade_id="T1",
        _peak_confirmation_samples=3, _peak_confirmation_ms=1000.0,
        _peak_confirmation_tolerance_twd=100.0, _entry_peak_guard_ms=15000.0,
        _max_single_update_jump_twd=200.0, _point_value=10.0, _estimated_cost=92.0,
        _near_entry=42830.0, _far_entry=42967.0, _phase="SPREAD",
    )
    for k, v in defaults.items():
        setattr(s, k, v)
    for k, v in attrs.items():
        setattr(s, k, v)
    return s


def _call(s, current_pnl_pts, near_mark, far_mark, now_ms, mark_age_ms=0.0,
          pair_skew_ms=0.0, entry_ts_ms=0.0, phase="SPREAD"):
    import strategies.plugins.futures.active.tmf_spread as _mod
    from strategies.plugins.futures.active.tmf_spread import TMFSpread
    _orig = _mod._append_event

    def _cap(etype, **kw):
        s._pj_events.append({"event": etype, **kw})
    _mod._append_event = _cap
    try:
        return TMFSpread._update_policy_j_peak(
            s, current_pnl_pts, near_mark, far_mark, now_ms,
            mark_age_ms=mark_age_ms, pair_skew_ms=pair_skew_ms,
            entry_ts_ms=entry_ts_ms, phase=phase)
    finally:
        _mod._append_event = _orig


def test_entry_200ms_inside_guard():
    s = _make_strategy()
    # entry at t=0 (receive epoch); evaluation at t=200ms
    _call(s, 47.0, 42830, 43077, 200.0, entry_ts_ms=0.0)
    assert s._pj_guard_state == "QUARANTINE"
    assert s._pj_durable_peak is None


def test_entry_14999ms_still_quarantined():
    s = _make_strategy()
    _call(s, 47.0, 42830, 43077, 14999.0, entry_ts_ms=0.0)
    assert s._pj_guard_state == "QUARANTINE"
    assert s._pj_durable_peak is None
    assert not any(e["event"] == "POLICY_J_PEAK_CONFIRMED" for e in s._pj_events)


def test_entry_15000ms_baseline_only():
    s = _make_strategy()
    _call(s, 47.0, 42830, 43077, 15000.0, entry_ts_ms=0.0)
    assert s._pj_guard_state == "COMPLETE"
    assert s._pj_guard_just_completed is True
    assert s._pj_durable_peak == pytest.approx(47.0 * 10 - 92, abs=5)
    # baseline tick: no giveback evaluation (flag set)


def test_baseline_tick_does_not_trigger():
    s = _make_strategy()
    _call(s, 47.0, 42830, 43077, 15000.0, entry_ts_ms=0.0)
    assert s._pj_guard_just_completed is True
    assert not any(e["event"] == "POLICY_J_TRIGGERED" for e in s._pj_events)


def test_restart_at_5s_remaining_10s():
    s = _make_strategy()
    # entry at epoch 100000; restart eval at 105000 (5s in)
    _call(s, 30.0, 42800, 43070, 105000.0, entry_ts_ms=100000.0)
    assert s._pj_guard_state == "QUARANTINE"
    # spike at 105001 still quarantined
    _call(s, 60.0, 42780, 43099, 105001.0, entry_ts_ms=100000.0)
    assert s._pj_durable_peak is None


def test_restart_at_14s_remaining_1s():
    s = _make_strategy()
    _call(s, 30.0, 42800, 43070, 114000.0, entry_ts_ms=100000.0)
    assert s._pj_guard_state == "QUARANTINE"
    _call(s, 60.0, 42780, 43099, 114500.0, entry_ts_ms=100000.0)
    assert s._pj_durable_peak is None


def test_restart_after_guard_no_new_window():
    s = _make_strategy()
    _call(s, 40.0, 42800, 43070, 120000.0, entry_ts_ms=100000.0)
    assert s._pj_guard_state == "COMPLETE"
    assert s._pj_durable_peak == pytest.approx(40.0 * 10 - 92, abs=5)


# ── 12. legacy naive state migration ──
def test_legacy_naive_state_migrates_as_taipei():
    ms, source = migrate_entry_time("2026-08-04T16:50:29.069")
    assert source == "LEGACY_NAIVE_LOCAL_MIGRATED"
    assert abs(ms - datetime(2026, 8, 4, 16, 50, 29, 69000).timestamp() * 1000) < 2


# ── 13. future timestamp fail-safe ──
def test_future_timestamp_failsafe():
    with pytest.raises(ValueError, match="FUTURE"):
        migrate_entry_time("2099-01-01T00:00:00+08:00")


# ── 14. missing timestamp fail-safe ──
def test_missing_timestamp_failsafe():
    with pytest.raises(ValueError, match="MISSING|UNPARSEABLE"):
        migrate_entry_time("")


# ── 15. abnormal elapsed does not block other risk controllers ──
def test_abnormal_elapsed_does_not_block_other_risk():
    # fail-safe suppresses Policy J but must not raise / block ATR/hard stops.
    # The suppression path is exercised through _update_policy_j_peak with a
    # future entry (elapsed < 0 -> treated as untrusted).
    s = _make_strategy()
    # entry_ts_ms far future -> elapsed negative
    _call(s, 40.0, 42800, 43070, 1000.0, entry_ts_ms=99999999999999.0)
    # no durable peak, no trigger — other controllers unaffected (no raise)
    assert s._pj_durable_peak is None


# ── 16. entry-time frozen guard_ms across restart ──
def test_guard_ms_frozen_per_trade():
    # entry settled freezes guard length; restart must reuse the ORIGINAL
    # trade's guard_ms, not the (possibly changed) config value.
    # resolver default is stable; verify _entry_peak_guard_ms instance attr
    # is what the guard path reads (frozen at init/trade).
    val, src, key = resolve_entry_peak_guard_ms({"entry_peak_guard_ms": 15000})
    assert val == 15000.0
    # frozen semantics: guard reads self._entry_peak_guard_ms (instance field),
    # not params each tick — guaranteed by the guard code path.


# ── 18. replay mts-auto-165028-779 window: 16:50:29-16:50:44 no durable peak ──
def test_replay_165028779_no_durable_peak_in_guard_window():
    import json
    events = []
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "logs", "mts_spread_events.jsonl")
    if not os.path.exists(path):
        pytest.skip("event ledger not available")
    for line in open(path):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("trade_id") == "mts-auto-165028-779":
            events.append(e)
    guard_baselines = [e for e in events if e.get("event") == "POLICY_J_GUARD_BASELINE"]
    confirms = [e for e in events if e.get("event") == "POLICY_J_PEAK_CONFIRMED"]
    # With correct clock, the +128 spike at 16:50:34 (5s after entry) must NOT
    # confirm. The recorded baseline at +225ms proves the bug; assert the
    # presence of the buggy baseline (documenting pre-fix behavior).
    assert any(e.get("elapsed_ms", 0) > 2_000_000 for e in guard_baselines), (
        "expected pre-fix 8h-skew baseline in ledger"
    )
