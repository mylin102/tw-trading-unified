# Policy J Entry Peak Guard — provisional quarantine semantics.
# Reproduces the dead `_entry_peak_guard_ms` (param accepted at 2112, never
# consumed — 2026-08-04 production incident: 5 night-session trades exited
# 0-11s after entry via Policy J with giveback=50, peak formed from opening
# spike). Locks the user-approved contract:
#   guard window = provisional quarantine: record, never promote, no trigger.
#   guard expiry = baseline from current synchronized mark, NOT the spike.
#   no trigger on the expiry tick itself; next valid evaluation starts.
#   other risk controllers (ATR stop / hard stop) keep running during guard.
import types

import pytest


def make_strategy(**attrs):
    s = types.SimpleNamespace()
    defaults = dict(
        _pj_trade_id=None, _pj_durable_peak=None, _pj_candidate_peak=None,
        _pj_candidate_ts=None, _pj_candidate_count=0, _pj_last_mark_pair=None,
        _pj_last_upl=None, _pj_last_eval_ts=None, _pj_events=[],
        _pj_guard_state=None,        # None | "QUARANTINE" | "COMPLETE"
        _pj_guard_provisional_peak=None,
        _pj_guard_initialized=False,
        _pj_guard_just_completed=False,
        _trade_id="T1",
        _peak_confirmation_samples=3, _peak_confirmation_ms=1000.0,
        _peak_confirmation_tolerance_twd=100.0, _entry_peak_guard_ms=15000.0,
        _max_single_update_jump_twd=200.0, _point_value=10.0, _estimated_cost=92.0,
        _near_entry=42830.0, _far_entry=42967.0,
        _phase="SPREAD",
    )
    for k, v in defaults.items():
        setattr(s, k, v)
    for k, v in attrs.items():
        setattr(s, k, v)
    return s


def call(s, current_pnl_pts, near_mark, far_mark, now_ms, mark_age_ms=0.0,
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


def _net(pts):
    return pts * 10.0 - 92.0


# ── 1. Same-tick entry spike must never form a durable peak ────────────
def test_same_tick_spike_inside_guard_not_durable():
    s = make_strategy()
    # entry at t=1000; first evaluation 1ms later shows +47pts (net 378)
    call(s, 47.0, 42830, 43077, 1001.0, entry_ts_ms=1000.0, phase="SPREAD")
    # repeated independent marks inside the guard window — still no durable peak
    call(s, 47.5, 42831, 43077, 1200.0, entry_ts_ms=1000.0, phase="SPREAD")
    call(s, 47.4, 42832, 43077, 1400.0, entry_ts_ms=1000.0, phase="SPREAD")
    assert s._pj_durable_peak is None, "entry-window spike promoted to durable peak"
    assert s._pj_guard_state == "QUARANTINE"
    assert s._pj_guard_provisional_peak is not None


# ── 2. Guard expiry baseline = current value, NOT window spike ─────────
def test_guard_baseline_is_expiry_value_not_spike():
    s = make_strategy()
    # spike +70pts (net 608) inside window at t=5000
    call(s, 70.0, 42800, 43099, 5000.0, entry_ts_ms=1000.0, phase="SPREAD")
    assert s._pj_durable_peak is None
    # window expires at t=16000; current value is +10pts (net 8) — baseline must be 8, not 608
    call(s, 10.0, 42810, 43067, 16000.0, entry_ts_ms=1000.0, phase="SPREAD")
    assert s._pj_durable_peak == pytest.approx(_net(10.0), abs=5)
    assert s._pj_guard_state == "COMPLETE"
    assert s._pj_guard_initialized is True


# ── 3. No giveback trigger on the guard-completion tick ───────────────
def test_no_trigger_on_guard_completion_tick():
    s = make_strategy()
    call(s, 47.0, 42830, 43077, 1001.0, entry_ts_ms=1000.0, phase="SPREAD")
    # expiry tick: current == baseline -> no giveback should be evaluated this tick
    call(s, 47.0, 42830, 43077, 16000.0, entry_ts_ms=1000.0, phase="SPREAD")
    assert s._pj_guard_just_completed is True
    assert s._pj_guard_initialized is True
    # caller must skip giveback evaluation on this tick (flag consumed downstream)


# ── 4. Normal peak rise + confirmation AFTER guard ─────────────────────
def test_peak_rise_and_confirm_after_guard():
    s = make_strategy(_peak_confirmation_samples=2)
    # complete the guard first
    call(s, 10.0, 42810, 43067, 16000.0, entry_ts_ms=1000.0, phase="SPREAD")
    assert s._pj_guard_state == "COMPLETE"
    # post-guard legitimate rise
    call(s, 30.0, 42800, 43090, 17000.0, entry_ts_ms=1000.0, phase="SPREAD")
    call(s, 31.0, 42799, 43090, 17500.0, entry_ts_ms=1000.0, phase="SPREAD")
    call(s, 30.9, 42798, 43090, 18000.0, entry_ts_ms=1000.0, phase="SPREAD")
    assert s._pj_durable_peak is not None
    assert s._pj_durable_peak >= _net(30.0) - 1


# ── 5. Giveback trigger path works AFTER guard ─────────────────────────
def test_giveback_condition_after_guard():
    s = make_strategy(_peak_confirmation_samples=2)
    call(s, 10.0, 42810, 43067, 16000.0, entry_ts_ms=1000.0, phase="SPREAD")
    # peak rises to +40pts then gives back to +20pts (net 108): giveback = peak - current
    call(s, 40.0, 42790, 43090, 17000.0, entry_ts_ms=1000.0, phase="SPREAD")
    call(s, 41.0, 42789, 43090, 17500.0, entry_ts_ms=1000.0, phase="SPREAD")
    call(s, 40.9, 42788, 43090, 18000.0, entry_ts_ms=1000.0, phase="SPREAD")
    peak = s._pj_durable_peak
    assert peak is not None and peak >= _net(40.0) - 1
    # giveback below threshold -> no rejection of candidate (durable survives)
    call(s, 20.0, 42810, 43070, 18500.0, entry_ts_ms=1000.0, phase="SPREAD")
    assert s._pj_durable_peak is not None  # Policy J trigger decision happens in adapter


# ── 6. Guard must NOT block other risk controllers ────────────────────
def test_guard_does_not_touch_other_risk_state():
    s = make_strategy()
    s._release_stop_triggered = False
    call(s, 47.0, 42830, 43077, 1001.0, entry_ts_ms=1000.0, phase="SPREAD")
    # Policy J guard only affects peak bookkeeping; nothing else is mutated
    assert s._release_stop_triggered is False
    # no POLICY_J_TRIGGERED / COMBINED_EXIT emission from peak update during guard
    assert not any(e["event"] == "POLICY_J_TRIGGERED" for e in s._pj_events)


# ── 7. Entry-loss during guard: Policy J stays silent, risk exits intact ─
def test_entry_loss_during_guard_no_policy_j_noise():
    s = make_strategy()
    call(s, -30.0, 42890, 42947, 1001.0, entry_ts_ms=1000.0, phase="SPREAD")
    assert s._pj_durable_peak is None
    assert not any(e["event"] == "POLICY_J_TRIGGERED" for e in s._pj_events)
    # candidate state must not be poisoned by negative values
    assert s._pj_candidate_peak is None or s._pj_candidate_peak <= 0


# ── 8. Restart mid-guard: remaining time continues, no fresh 15s ───────
def test_restart_mid_guard_continues_remaining_time():
    s = make_strategy()
    # trade entered at t=0; restart at t=10000 (5s of guard left)
    call(s, 40.0, 42800, 43070, 10000.0, entry_ts_ms=0.0, phase="SPREAD")
    assert s._pj_guard_state == "QUARANTINE"
    # spike at t=10001 must still be quarantined (guard not reset)
    call(s, 60.0, 42780, 43099, 10001.0, entry_ts_ms=0.0, phase="SPREAD")
    assert s._pj_durable_peak is None


# ── 9. Restart after guard: no new window, baseline immediate ─────────
def test_restart_after_guard_no_new_window():
    s = make_strategy()
    # trade entered long ago (entry_ts=0, now=20000 > 15s)
    call(s, 40.0, 42800, 43070, 20000.0, entry_ts_ms=0.0, phase="SPREAD")
    assert s._pj_guard_state == "COMPLETE"
    assert s._pj_durable_peak == pytest.approx(_net(40.0), abs=5)


# ── 10. SINGLE_LEG phase guard behaves identically ────────────────────
def test_guard_in_single_leg_phase():
    s = make_strategy()
    call(s, 47.0, 42830, 43077, 1001.0, entry_ts_ms=1000.0, phase="SINGLE_LEG")
    assert s._pj_durable_peak is None
    assert s._pj_guard_state == "QUARANTINE"
    call(s, 10.0, 42810, 43067, 16000.0, entry_ts_ms=1000.0, phase="SINGLE_LEG")
    assert s._pj_durable_peak == pytest.approx(_net(10.0), abs=5)
    assert s._pj_guard_state == "COMPLETE"


# ── 11. Missing/invalid entry timestamp: fail-safe, never disable PJ ──
def test_missing_entry_ts_failsafe_not_disabled():
    s = make_strategy()
    # entry_ts_ms=0.0 means "unknown" — must not permanently disable Policy J
    call(s, 40.0, 42800, 43070, 1001.0, entry_ts_ms=0.0, phase="SPREAD")
    # failsafe: no quarantine lock; normal candidate path may proceed
    assert s._pj_guard_state in (None, "COMPLETE")
    # and Policy J bookkeeping is not broken
    call(s, 41.0, 42799, 43070, 1500.0, entry_ts_ms=0.0, phase="SPREAD")
    assert s._pj_last_upl is not None


# ── 12. Asymmetric / stale marks at expiry: baseline NOT from bad mark ─
def test_expiry_baseline_rejects_stale_skewed_mark():
    s = make_strategy()
    call(s, 40.0, 42800, 43070, 5000.0, entry_ts_ms=1000.0, phase="SPREAD")
    # expiry tick arrives with stale (age 50s) mark — baseline must NOT be set
    call(s, 10.0, 42810, 43067, 16000.0, entry_ts_ms=1000.0, phase="SPREAD",
         mark_age_ms=50000.0)
    assert s._pj_durable_peak is None
    assert s._pj_guard_state == "QUARANTINE"  # still waiting for a valid mark
    # next valid (fresh, synchronized) mark establishes baseline
    call(s, 12.0, 42812, 43069, 16100.0, entry_ts_ms=1000.0, phase="SPREAD")
    assert s._pj_durable_peak == pytest.approx(_net(12.0), abs=5)
    assert s._pj_guard_state == "COMPLETE"
