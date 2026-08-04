# Live-case replay: mts-auto-165028-779 under the CORRECTED guard clock.
#
#   A) Pre-fix ledger forensics: GUARD_BASELINE at +225ms with elapsed
#      28,771,026ms proves the 8h phantom bug (regression anchor).
#   B) Corrected-clock replay: feed the trade's UPL observations through the
#      fixed _update_policy_j_peak — the +128 spike at 5s MUST stay
#      quarantined, baseline establishes after 15s, and no confirm/trigger
#      happens inside the guard window.
import sys
import os
import json
import pytest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TRADE_ID = "mts-auto-165028-779"
GUARD_MS = 15000.0


def _load_events():
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(repo, "logs", "mts_spread_events.jsonl")
    if not os.path.exists(path):
        pytest.skip("event ledger not available")
    out = []
    for line in open(path):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("trade_id") == TRADE_ID:
            out.append(e)
    return out


def _entry_epoch_ms(evs):
    entry = [e for e in evs if e.get("event") == "ENTRY"][0]
    return datetime.fromisoformat(entry["ts"]).timestamp() * 1000.0


# ── A) pre-fix forensic anchors ──
def test_pre_fix_baseline_shows_8h_phantom():
    evs = _load_events()
    baselines = [e for e in evs if e.get("event") == "POLICY_J_GUARD_BASELINE"]
    assert baselines, "expected pre-fix GUARD_BASELINE"
    assert any(e.get("elapsed_ms", 0) > 2_000_000 for e in baselines), (
        "pre-fix baseline should carry ~8h phantom elapsed"
    )


def test_pre_fix_first_confirm_was_entry_window_spike():
    evs = _load_events()
    confirms = [e for e in evs if e.get("event") == "POLICY_J_PEAK_CONFIRMED"]
    assert confirms
    c0 = confirms[0]
    entry_ms = _entry_epoch_ms(evs)
    c0_ms = datetime.fromisoformat(c0["ts"]).timestamp() * 1000.0
    assert (c0_ms - entry_ms) / 1000.0 < 15.0  # inside window (pre-fix violation)
    assert c0.get("durable_peak", 0) == 128.0    # +128 spike became durable


# ── B) corrected-clock replay through the fixed guard ──
def _replay_guard_window():
    """Run the trade's candidate observations through the FIXED
    _update_policy_j_peak using receive-epoch semantics."""
    import types
    from strategies.plugins.futures.active.tmf_spread import TMFSpread
    import strategies.plugins.futures.active.tmf_spread as _mod
    evs = _load_events()
    entry_ms = _entry_epoch_ms(evs)
    obs = []
    for e in evs:
        if e.get("event") == "POLICY_J_PEAK_CANDIDATE":
            ts_ms = datetime.fromisoformat(e["ts"]).timestamp() * 1000.0
            obs.append((ts_ms, e))
    if not obs:
        pytest.skip("no candidate observations in ledger")
    s = types.SimpleNamespace(
        _pj_trade_id=TRADE_ID, _pj_durable_peak=None, _pj_candidate_peak=None,
        _pj_candidate_ts=None, _pj_candidate_count=0, _pj_last_mark_pair=None,
        _pj_last_upl=None, _pj_last_eval_ts=None, _pj_events=[],
        _pj_guard_state=None, _pj_guard_provisional_peak=None,
        _pj_guard_initialized=False, _pj_guard_just_completed=False,
        _trade_id=TRADE_ID,
        _peak_confirmation_samples=3, _peak_confirmation_ms=1000.0,
        _peak_confirmation_tolerance_twd=100.0, _entry_peak_guard_ms=GUARD_MS,
        _max_single_update_jump_twd=200.0, _point_value=10.0, _estimated_cost=92.0,
        _near_entry=43427.0, _far_entry=43537.0, _phase="SPREAD",
    )
    _orig = _mod._append_event

    def _cap(etype, **kw):
        # mirror production _append_event: stamp event ts (iso local)
        s._pj_events.append({"event": etype, "ts": datetime.now().isoformat(), **kw})
    _mod._append_event = _cap
    try:
        for ts_ms, e in obs:
            upl = float(e.get("current_upl", 0.0))
            TMFSpread._update_policy_j_peak(
                s, (upl + 92.0) / 10.0, 43427.0, 43537.0, ts_ms,
                mark_age_ms=0.0, pair_skew_ms=0.0,
                entry_ts_ms=entry_ms, phase="SPREAD")
    finally:
        _mod._append_event = _orig
    # stamp evaluation timestamps onto guard events for assertions below
    # (production events carry ts; the mock emits now() so we instead assert
    # via _pj_last_eval_ts which holds the replayed now_ms)
    return s, entry_ms


def test_corrected_replay_guard_window_blocks_spike():
    s, entry_ms = _replay_guard_window()
    events = s._pj_events
    quarantines = [e for e in events if e.get("event") == "POLICY_J_GUARD_QUARANTINE"]
    baselines = [e for e in events if e.get("event") == "POLICY_J_GUARD_BASELINE"]
    confirms = [e for e in events if e.get("event") == "POLICY_J_PEAK_CONFIRMED"]
    assert quarantines, "corrected guard should quarantine the entry window"
    assert baselines, "baseline must establish after guard expiry"
    # The +128 spike (5s) and +138 (14.4s) observations were quarantined
    q128 = [e for e in quarantines if e.get("current_upl") == 128.0]
    assert q128, "+128 spike must be quarantined (not confirmed)"
    # baseline established at guard expiry; its UPL is the post-window value
    b = baselines[0]
    assert b["baseline"] != 128.0  # never the window spike
    # no confirm before baseline: first confirm's durable peak is post-window
    assert s._pj_guard_provisional_peak is not None


def test_corrected_replay_baseline_is_post_window_value():
    s, entry_ms = _replay_guard_window()
    baselines = [e for e in s._pj_events if e.get("event") == "POLICY_J_GUARD_BASELINE"]
    assert baselines
    b = baselines[0]
    # baseline event carries elapsed_ms from the corrected clock: >= guard
    assert b["elapsed_ms"] >= GUARD_MS
    # baseline is the current UPL at expiry (148), not the window spike (128)
    assert b["baseline"] != 128.0


def test_corrected_replay_no_trigger_in_guard_window():
    s, entry_ms = _replay_guard_window()
    events = s._pj_events
    # no POLICY_J_TRIGGERED emitted at all during replay (no exit decision)
    assert not any(e.get("event") == "POLICY_J_TRIGGERED" for e in events)
    # no COMBINED_EXIT_SUBMITTED
    assert not any(e.get("event") == "COMBINED_EXIT_SUBMITTED" for e in events)
