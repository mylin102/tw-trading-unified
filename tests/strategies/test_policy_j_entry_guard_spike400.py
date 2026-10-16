# 2026-08-04 review item 3 regression: a 400+ TWD spike inside the entry
# guard window must NOT form a durable peak nor emit POLICY_J_PEAK_CONFIRMED.
# (Production incident: 5 trades exited 0-11s after entry via giveback=50,
# peak built from opening spikes. 00-030 peak 378 in the SAME second as entry.)
import pytest
import types

from tests.strategies.test_policy_j_entry_peak_guard import make_strategy, call, _net


def test_400_twd_spike_inside_guard_not_confirmed():
    s = make_strategy(_peak_confirmation_samples=2)
    # entry at t=1000; +45pts (net 358) then +52pts (net 428) — both inside 15s
    call(s, 45.0, 42830, 43075, 1001.0, entry_ts_ms=1000.0, phase="SPREAD")
    call(s, 52.0, 42830, 43082, 1100.0, entry_ts_ms=1000.0, phase="SPREAD")
    call(s, 52.5, 42831, 43082, 1200.0, entry_ts_ms=1000.0, phase="SPREAD")
    # spike well above activation (428 TWD > 200) — must stay provisional
    assert s._pj_durable_peak is None
    assert s._pj_guard_state == "QUARANTINE"
    assert s._pj_guard_provisional_peak is not None
    assert s._pj_guard_provisional_peak >= 400.0
    assert not any(e["event"] == "POLICY_J_PEAK_CONFIRMED" for e in s._pj_events)
    assert not any(e["event"] == "POLICY_J_TRIGGERED" for e in s._pj_events)
    # repeated independent marks at spike level still not confirmed inside window
    call(s, 52.4, 42832, 43082, 2000.0, entry_ts_ms=1000.0, phase="SPREAD")
    call(s, 52.6, 42833, 43082, 2500.0, entry_ts_ms=1000.0, phase="SPREAD")
    assert s._pj_durable_peak is None
    assert not any(e["event"] == "POLICY_J_PEAK_CONFIRMED" for e in s._pj_events)


def test_400_twd_spike_then_guard_expiry_baseline_is_current_not_spike():
    s = make_strategy()
    # spike 428 TWD inside window
    call(s, 52.0, 42830, 43082, 2000.0, entry_ts_ms=1000.0, phase="SPREAD")
    assert s._pj_guard_provisional_peak == pytest.approx(428.0, abs=5)
    # expiry at t=16000, current is +5pts (net -42) — baseline must NOT be 428
    call(s, 5.0, 42815, 43062, 16000.0, entry_ts_ms=1000.0, phase="SPREAD")
    assert s._pj_durable_peak == pytest.approx(_net(5.0), abs=5)
    assert s._pj_durable_peak < 0  # negative baseline, NOT the 428 spike
    assert s._pj_guard_state == "COMPLETE"


def test_spike_inside_guard_cannot_trigger_exit_through_adapter_flag():
    s = make_strategy()
    call(s, 52.0, 42830, 43082, 2000.0, entry_ts_ms=1000.0, phase="SPREAD")
    # guard not yet completed -> adapter must see no durable peak to trigger on
    assert s._pj_durable_peak is None
    # no exit decision can originate from a quarantine state
    assert not any(e["event"] in ("POLICY_J_TRIGGERED", "COMBINED_EXIT_SUBMITTED")
                   for e in s._pj_events)
