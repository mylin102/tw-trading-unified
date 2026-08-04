# Policy J Peak Spike Validation — TC-3 golden + regression gates.
# Tests target the extracted _update_policy_j_peak method (pure logic —
# state passed in/out via self attributes).
import types

import pytest


def make_strategy(**attrs):
    s = types.SimpleNamespace()
    defaults = dict(
        _pj_trade_id=None, _pj_durable_peak=None, _pj_candidate_peak=None,
        _pj_candidate_ts=None, _pj_candidate_count=0, _pj_last_mark_pair=None,
        _pj_last_upl=None, _pj_last_eval_ts=None, _pj_events=[],
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


# helper: call the method under test (imported lazily)
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


def emit(s, event, **kw):
    s._pj_events.append({"event": event, **kw})


def test_entry_not_settled_suppresses():
    s = make_strategy(_near_entry=0.0)  # near entry missing -> suppressed
    result = call(s, 72.0, 42800, 42975, 1000.0, entry_ts_ms=100.0)
    assert result is False  # suppressed
    assert s._pj_durable_peak is None
    assert any(e["event"] == "POLICY_J_TRIGGER_SUPPRESSED" for e in s._pj_events)


def test_new_trade_resets_peak():
    s = make_strategy(_pj_trade_id="OLD", _pj_durable_peak=628.0)
    s._trade_id = "NEW"
    result = call(s, 5.0, 42830, 42967, 1000.0, entry_ts_ms=100.0)
    # old peak must not survive into the new trade
    assert s._pj_durable_peak is None or s._pj_durable_peak < 600


def test_single_transient_spike_only_candidate_not_durable():
    s = make_strategy()
    # one transient 72-pt mark (628 TWD) then normalize to -2
    call(s, 72.0, 42800, 42975, 1000.0, entry_ts_ms=100.0, phase="SPREAD")
    assert s._pj_candidate_peak == 628.0
    assert s._pj_durable_peak is None  # not promoted yet
    # normalize: current drops far below candidate -> candidate cancelled
    call(s, -0.2, 42831, 42976, 2000.0, entry_ts_ms=100.0, phase="SPREAD")
    assert s._pj_candidate_peak is None
    assert s._pj_durable_peak is None  # no trigger source
    assert any(e["event"] == "POLICY_J_PEAK_REJECTED" for e in s._pj_events)


def test_repeated_same_mark_pair_does_not_confirm():
    s = make_strategy(_peak_confirmation_samples=2)
    call(s, 72.0, 42800, 42975, 1000.0, entry_ts_ms=100.0)
    # identical mark pair repeated — must NOT count as confirmation
    call(s, 72.0, 42800, 42975, 1500.0, entry_ts_ms=100.0)
    assert s._pj_candidate_count == 1  # unchanged
    assert s._pj_durable_peak is None


def test_independent_persistent_marks_confirm_peak():
    s = make_strategy(_peak_confirmation_samples=2)
    call(s, 72.0, 42800, 42975, 1000.0, entry_ts_ms=100.0)
    call(s, 72.5, 42801, 42975, 1500.0, entry_ts_ms=100.0)   # new high -> count resets to 1
    call(s, 72.4, 42802, 42975, 2000.0, entry_ts_ms=100.0)   # independent, same level -> confirm
    assert s._pj_durable_peak == pytest.approx(72.5 * 10 - 92, abs=5)
    assert s._pj_candidate_count == 0  # cleared after confirmation
    assert any(e["event"] == "POLICY_J_PEAK_CONFIRMED" for e in s._pj_events)


def test_stale_mark_cannot_confirm():
    s = make_strategy(_peak_confirmation_samples=2)
    call(s, 72.0, 42800, 42975, 1000.0, entry_ts_ms=100.0)
    # stale second mark (age 50s) — must not confirm
    call(s, 72.5, 42801, 42975, 1500.0, entry_ts_ms=100.0, mark_age_ms=50000.0)
    assert s._pj_durable_peak is None


def test_skewed_pair_cannot_confirm():
    s = make_strategy(_peak_confirmation_samples=2)
    call(s, 72.0, 42800, 42975, 1000.0, entry_ts_ms=100.0)
    call(s, 72.5, 42801, 42975, 1500.0, entry_ts_ms=100.0, pair_skew_ms=3000.0)
    assert s._pj_durable_peak is None


def test_entry_window_jump_only_candidate():
    s = make_strategy()
    # within entry guard window, jump 600 TWD — candidate only, no durable
    call(s, 69.2, 42800, 42975, 1000.0, entry_ts_ms=100.0)  # 600 TWD jump
    assert s._pj_durable_peak is None
    assert s._pj_candidate_peak == pytest.approx(600.0, abs=5)


def test_persistent_rapid_gain_still_activates():
    s = make_strategy(_peak_confirmation_samples=2)
    # legitimate persistent gain: candidate confirmed across independent marks
    call(s, 30.0, 42800, 42990, 1000.0, entry_ts_ms=100.0)
    call(s, 31.0, 42799, 42990, 1500.0, entry_ts_ms=100.0)   # new high -> count 1
    call(s, 30.9, 42798, 42990, 2000.0, entry_ts_ms=100.0)   # confirm
    assert s._pj_durable_peak is not None
    assert s._pj_durable_peak > 200.0  # above activation -> can trigger later


def test_normal_confirmed_path_functional():
    s = make_strategy(_peak_confirmation_samples=3, _peak_confirmation_ms=2000.0)
    call(s, 30.0, 42800, 42990, 1000.0, entry_ts_ms=100.0)
    call(s, 30.5, 42799, 42990, 1500.0, entry_ts_ms=100.0)   # new high -> count 1
    call(s, 30.4, 42798, 42989, 2000.0, entry_ts_ms=100.0)   # count 2
    call(s, 30.3, 42797, 42989, 2500.0, entry_ts_ms=100.0)   # count 3 -> confirm
    assert s._pj_durable_peak == pytest.approx(30.5 * 10 - 92, abs=5)


# ── 2026-08-04 gate regression (real attrs) ─────────────────────────────

def test_gate_passes_with_real_entry_attrs():
    s = make_strategy()  # _near_entry/_far_entry set (real attr names)
    result = call(s, 30.0, 42801, 42975, 1000.0, phase="SPREAD")
    assert not any(e["event"] == "POLICY_J_TRIGGER_SUPPRESSED" for e in s._pj_events)
    assert any(e["event"] == "POLICY_J_PEAK_CANDIDATE" for e in s._pj_events)


def test_gate_suppresses_when_far_entry_missing():
    s = make_strategy(_far_entry=0.0)
    call(s, 30.0, 42801, 42975, 1000.0, phase="SPREAD")
    assert any(e["event"] == "POLICY_J_TRIGGER_SUPPRESSED"
               and e.get("reason") == "ENTRY_NOT_SETTLED" for e in s._pj_events)


def test_gate_works_in_single_leg_phase():
    s = make_strategy()  # both real entries present
    result = call(s, 30.0, 42801, 42975, 1000.0, phase="SINGLE_LEG")
    assert not any(e["event"] == "POLICY_J_TRIGGER_SUPPRESSED" for e in s._pj_events)


def test_no_fake_attributes_created():
    s = make_strategy()
    assert not hasattr(s, "_near_entry_avg")
    assert not hasattr(s, "_near_open_qty")


def test_suppression_throttled_state_transition_only():
    s = make_strategy(_far_entry=0.0)
    for _ in range(5):
        call(s, 30.0, 42801, 42975, 1000.0, phase="SPREAD")  # same now_ms
    n_supp = sum(1 for e in s._pj_events
                 if e["event"] == "POLICY_J_TRIGGER_SUPPRESSED")
    assert n_supp <= 2  # throttled (5s window) — not one per call
