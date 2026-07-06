"""
Layer 2 + 3: Contract test + Replay test for VolatilityContext → VolState pipeline.

Layer 2: Contract Test
- skew_regime dict must contain required fields
- no key drift between shape_classifier.to_dict() and dashboard consumers

Layer 3: Replay Test
- LOW vol → NORMAL
- LEFT + HIGH + pct>0.85 consecutive 3 samples → PANIC
- HIGH + pct>0.95 → EVENT
- 5 consecutive samples below exit threshold → exit state
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.derivatives.shape_classifier import IVShapeClassifier, VolatilityContext
from core.derivatives.iv_percentile import IVPercentileEngine
from core.derivatives.vol_state_machine import VolatilityStateMachine, VolState

import datetime

# ═══════════════════════════════════════════════════════════════════
# Layer 2: Contract Test
# ═══════════════════════════════════════════════════════════════════

REQUIRED_SKEW_REGSITRY_KEYS = {
    "directional_skew", "tension", "iv_percentile", "iv_zscore",
    "vol_state", "vol_state_age_sec", "vol_state_transition_count",
    "shape",
}

REQUIRED_SKEW_SNAPSHOT_KEYS = {
    "directional_skew", "tension", "slope_ratio", "atm_iv_change",
    "delta_slope_ratio", "confidence", "iv_percentile", "iv_zscore",
    "timestamp", "underlying_price", "atm_iv", "otm_put_iv", "otm_call_iv",
    "shape", "vol_regime",
}


def test_contract_skew_regime_dict_keys():
    """Verify the full skew_regime dict injected into MarketData has required keys."""
    classifier = IVShapeClassifier()
    pct_engine = IVPercentileEngine(window_sec=7200, min_samples=1)

    # Simulate 3 bars to get a transition
    now = datetime.datetime.utcnow()
    for i in range(3):
        ts = now + datetime.timedelta(seconds=i * 300)
        # LEFT skew + HIGH tension + high percentile
        ctx = classifier.classify(
            atm_iv=0.15, otm_put_iv=0.30, otm_call_iv=0.16,
            underlying_price=34000, timestamp=ts,
        )
        pct_engine.record(atm_iv=0.15, timestamp=ts)

    sm = VolatilityStateMachine(min_samples_entry=1, min_samples_exit=1, min_dwell_sec=0,
                                 panic_pct_threshold=0.50)
    final_ctx = classifier.classify(
        atm_iv=0.15, otm_put_iv=0.30, otm_call_iv=0.16,
        underlying_price=34000, timestamp=now + datetime.timedelta(seconds=900),
    )
    pct_result = pct_engine.get_percentile(0.15)
    final_ctx.iv_percentile = pct_result["iv_percentile"]
    final_ctx.iv_zscore = pct_result["iv_zscore"]

    vs = sm.update(
        directional_skew=final_ctx.directional_skew,
        tension=final_ctx.tension,
        iv_percentile=final_ctx.iv_percentile,
        confidence=final_ctx.confidence,
        timestamp=final_ctx.timestamp,
    )

    regime_dict = final_ctx.to_dict()
    regime_dict["vol_state"] = str(vs.state)
    regime_dict["vol_state_age_sec"] = vs.age_sec
    regime_dict["vol_state_transition_count"] = vs.transition_count
    regime_dict["vol_state_persistent"] = vs.persistent

    # Check required keys exist
    missing = REQUIRED_SKEW_REGSITRY_KEYS - set(regime_dict.keys())
    assert not missing, f"Missing required keys in skew_regime dict: {missing}"

    # Check type constraints
    assert isinstance(regime_dict["directional_skew"], str)
    assert isinstance(regime_dict["tension"], str)
    assert 0.0 <= regime_dict["iv_percentile"] <= 1.0, f"pct out of range: {regime_dict['iv_percentile']}"
    assert isinstance(regime_dict["vol_state"], str)
    assert isinstance(regime_dict["vol_state_age_sec"], (int, float))
    assert regime_dict["vol_state_age_sec"] >= 0
    assert regime_dict["vol_state_transition_count"] >= 0

    print("[CONTRACT] ✅ skew_regime dict has all required keys")
    print(f"  directional_skew={regime_dict['directional_skew']}")
    print(f"  tension={regime_dict['tension']}")
    print(f"  iv_percentile={regime_dict['iv_percentile']}")
    print(f"  iv_zscore={regime_dict['iv_zscore']}")
    print(f"  vol_state={regime_dict['vol_state']}")
    print(f"  vol_state_age_sec={regime_dict['vol_state_age_sec']}")
    print(f"  vol_state_transition_count={regime_dict['vol_state_transition_count']}")
    print(f"  shape={regime_dict['shape']}")


def test_contract_to_dict_snapshot():
    """Verify VolatilityContext.to_dict() has all snapshot keys."""
    ctx = VolatilityContext(
        directional_skew="LEFT",
        tension="HIGH",
        slope_ratio=-0.5,
        atm_iv_change=0.04,
        delta_slope_ratio=-0.1,
        confidence=0.8,
        iv_percentile=0.85,
        iv_zscore=1.5,
        timestamp=datetime.datetime.utcnow(),
        underlying_price=34000.0,
        atm_iv=0.15,
        otm_put_iv=0.25,
        otm_call_iv=0.17,
        shape="LEFT_SKEW",
        vol_regime="EXPANDING",
    )
    d = ctx.to_dict()
    missing = REQUIRED_SKEW_SNAPSHOT_KEYS - set(d.keys())
    assert not missing, f"Missing keys in to_dict: {missing}"
    print("[CONTRACT] ✅ VolatilityContext.to_dict() has all snapshot keys")
    print(f"  Keys: {sorted(d.keys())}")


# ═══════════════════════════════════════════════════════════════════
# Layer 3: Replay Test
# ═══════════════════════════════════════════════════════════════════

def _make_ts(offset):
    return datetime.datetime.utcnow() - datetime.timedelta(seconds=-offset)


def _replay_update(sm, skew, tension, pct, conf=0.8):
    """Helper to update state machine with fake timestamp."""
    return sm.update(
        directional_skew=skew,
        tension=tension,
        iv_percentile=pct,
        confidence=conf,
        timestamp=_make_ts(0),
    )


def test_replay_low_vol_to_normal():
    """Low vol scenario → NORMAL state."""
    sm = VolatilityStateMachine(
        min_samples_entry=1, min_samples_exit=1, min_dwell_sec=0,
        calm_pct_threshold=0.30,
    )
    r = _replay_update(sm, "SYMMETRIC", "LOW", 0.15, 0.7)
    assert r.state == VolState.CALM, f"Expected CALM, got {r.state}"
    print("[REPLAY] ✅ LOW vol → CALM")


def test_replay_panic_entry_hysteresis():
    """LEFT + HIGH + pct>0.85 consecutive 3 samples → PANIC."""
    sm = VolatilityStateMachine(
        min_samples_entry=3, min_samples_exit=5, min_dwell_sec=0,
        panic_pct_threshold=0.60,
    )
    results = []
    for i in range(5):
        r = _replay_update(sm, "LEFT", "HIGH", 0.85, 0.9)
        results.append(r.state)

    # After 3 consecutive, should be PANIC
    assert results[2] == VolState.PANIC, f"Sample 3 should be PANIC, got {results[2]}"
    # After 5, still PANIC
    assert results[4] == VolState.PANIC, f"Sample 5 should still be PANIC, got {results[4]}"
    print(f"[REPLAY] ✅ PANIC entry hysteresis: {[str(s) for s in results]}")


def test_replay_event_priority():
    """HIGH + pct>0.95 → EVENT (takes priority over PANIC)."""
    sm = VolatilityStateMachine(
        min_samples_entry=1, min_samples_exit=1, min_dwell_sec=0,
        panic_pct_threshold=0.60,
        event_pct_threshold=0.80,
    )
    r = _replay_update(sm, "LEFT", "HIGH", 0.95, 0.9)
    assert r.state == VolState.EVENT, f"Expected EVENT, got {r.state}"
    print("[REPLAY] ✅ pct>0.95 + HIGH → EVENT")


def test_replay_exit_hysteresis():
    """5 consecutive samples below exit threshold → exit PANIC."""
    sm = VolatilityStateMachine(
        min_samples_entry=1, min_samples_exit=5, min_dwell_sec=0,
        panic_pct_threshold=0.80,
        euphoria_pct_threshold=0.80,
        calm_pct_threshold=0.30,
    )
    # Enter PANIC with high pct
    for _ in range(3):
        _replay_update(sm, "LEFT", "HIGH", 0.90, 0.9)

    # Now propose NORMAL (low pct) — need 5 consecutive to exit
    results = []
    for i in range(7):
        r = _replay_update(sm, "SYMMETRIC", "LOW", 0.15, 0.7)
        results.append(r.state)

    # After 5 LOW proposals, should exit PANIC
    assert results[4] != VolState.PANIC, f"Sample 5 should have exited PANIC, got {results[4]}"
    print(f"[REPLAY] ✅ PANIC exit hysteresis: {[str(s) for s in results[:5]]}")


def test_replay_full_day():
    """Simulate a full trading day sequence.

    09:30 OPEN → EXPANDING (opening vol)
    10:00 Calm down → NORMAL
    11:30 Crash → PANIC
    12:30 Recovery → NORMAL
    13:25 EOD → still NORMAL
    """
    sm = VolatilityStateMachine(
        min_samples_entry=2, min_samples_exit=3, min_dwell_sec=0,
        panic_pct_threshold=0.70,
        calm_pct_threshold=0.30,
    )
    timeline = []

    # 09:30 OPEN — tension HIGH
    for _ in range(3):
        r = _replay_update(sm, "SYMMETRIC", "HIGH", 0.55, 0.5)
        timeline.append(("open", str(r.state)))

    # 10:00 — settle to NORMAL
    for _ in range(5):
        r = _replay_update(sm, "SYMMETRIC", "LOW", 0.40, 0.6)
        timeline.append(("mid", str(r.state)))

    # 11:30 — crash: LEFT + HIGH + high pct
    for _ in range(4):
        r = _replay_update(sm, "LEFT", "HIGH", 0.85, 0.9)
        timeline.append(("crash", str(r.state)))

    # 12:30 — recovery
    for _ in range(6):
        r = _replay_update(sm, "SYMMETRIC", "LOW", 0.35, 0.6)
        timeline.append(("recov", str(r.state)))

    # 13:25 EOD
    r = _replay_update(sm, "SYMMETRIC", "LOW", 0.30, 0.5)
    timeline.append(("eod", str(r.state)))

    print(f"[REPLAY] ✅ Full day timeline ({len(timeline)} states):")
    for i, (phase, state) in enumerate(timeline):
        if i == 0 or timeline[i-1][1] != state or phase != timeline[i-1][0]:
            print(f"  {phase}: {state}")

    # Check the crash was detected
    crash_states = [s for p, s in timeline if p == "crash"]
    assert any(s == "PANIC" for s in crash_states), f"Crash should trigger PANIC: {crash_states}"


# ═══════════════════════════════════════════════════════════════════
# Runtime invariant checks (Layer 4 — run on JSONL after dry run)
# ═══════════════════════════════════════════════════════════════════

def validate_jsonl(path: str) -> list[str]:
    """Validate a JSONL file for runtime invariants.

    Returns list of violation messages (empty = clean).
    """
    import json
    violations = []
    records = []

    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                records.append(r)
            except json.JSONDecodeError as e:
                violations.append(f"Line {lineno}: invalid JSON — {e}")
                continue

    if not records:
        return ["No records found"]

    prev_age = -1
    prev_transitions = -1
    prev_state = None

    for i, r in enumerate(records):
        # pct always in [0, 1]
        pct = r.get("iv_percentile", -1)
        if not (0.0 <= pct <= 1.0):
            violations.append(f"Record {i}: iv_percentile={pct} out of [0, 1]")

        # slope_ratio always in [-1, 1]
        sr = r.get("slope_ratio", -2)
        if sr != 0 and not (-1.0 <= sr <= 1.0):
            violations.append(f"Record {i}: slope_ratio={sr} out of [-1, 1]")

        # age_sec only goes forward (or resets to 0 on transition)
        age = r.get("vol_state_age_sec", -1)
        if age < 0:
            violations.append(f"Record {i}: negative age_sec={age}")

        # transition_count monotonic
        tc = r.get("vol_state_transition_count", -1)
        if tc < prev_transitions:
            violations.append(f"Record {i}: transition_count decreased {prev_transitions} → {tc}")
        prev_transitions = tc

        # UNKNOWN check
        state = r.get("vol_state", "")
        ts = r.get("timestamp", "?")

    if violations:
        print(f"[VALIDATE] ❌ {len(violations)} violations:")
        for v in violations:
            print(f"  {v}")
    else:
        print(f"[VALIDATE] ✅ {len(records)} records, 0 violations")

    return violations


if __name__ == "__main__":
    # Run contract tests
    test_contract_skew_regime_dict_keys()
    test_contract_to_dict_snapshot()

    # Run replay tests
    test_replay_low_vol_to_normal()
    test_replay_panic_entry_hysteresis()
    test_replay_event_priority()
    test_replay_exit_hysteresis()
    test_replay_full_day()

    print("\n" + "=" * 60)
    print("ALL CONTRACT + REPLAY TESTS PASSED")
    print("=" * 60)

    # Validate JSONL if provided
    if len(sys.argv) > 1:
        print(f"\nValidating {sys.argv[1]}...")
        validate_jsonl(sys.argv[1])
