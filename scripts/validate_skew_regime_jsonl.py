"""
Layer 4: Runtime invariant validation for skew_regime JSONL.

Usage:
    python3 scripts/validate_skew_regime_jsonl.py logs/skew_regime/20260518.jsonl

Checks:
    - Each line is valid JSON
    - iv_percentile ∈ [0, 1]
    - slope_ratio ∈ [-1, 1]
    - age_sec monotonic non-decreasing (except on state transitions to 0)
    - transition_count monotonic non-decreasing
    - no duplicate timestamps
"""

import json
import sys
from pathlib import Path


def validate(path: str) -> int:
    """Validate a JSONL file. Returns 0 = clean, 1 = violations found."""
    p = Path(path)
    if not p.exists():
        print(f"[VALIDATE] ❌ File not found: {path}")
        return 1

    violations = []
    records = []

    with open(p) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                records.append((lineno, r))
            except json.JSONDecodeError as e:
                violations.append(f"Line {lineno}: invalid JSON — {e}")

    total = len(records)
    if total == 0:
        print(f"[VALIDATE] ❌ No valid records found in {path}")
        return 1

    print(f"[VALIDATE] Checking {total} records in {path}...")

    prev_age = -1
    prev_transitions = -1
    seen_ts = set()
    has_option_data = False

    for lineno, r in records:
        # iv_percentile ∈ [0, 1]
        pct = r.get("iv_percentile", -1)
        if not (0.0 <= pct <= 1.0):
            violations.append(f"Line {lineno}: iv_percentile={pct} ∉ [0, 1]")

        # slope_ratio ∈ [-1, 1]
        sr = r.get("slope_ratio", -2)
        if sr != 0 and not (-1.0 <= sr <= 1.0):
            violations.append(f"Line {lineno}: slope_ratio={sr} ∉ [-1, 1]")

        # age_sec monotonic (can reset to 0 on transition)
        age = r.get("vol_state_age_sec", -1)
        if age < 0:
            violations.append(f"Line {lineno}: negative age_sec={age}")
        elif prev_age >= 0 and age > 0 and age < prev_age:
            # Allow reset to 0, but not partial decrease
            violations.append(f"Line {lineno}: age_sec decreased {prev_age} → {age}")
        prev_age = age

        # transition_count monotonic
        tc = r.get("vol_state_transition_count", -1)
        if tc < prev_transitions:
            violations.append(f"Line {lineno}: transition_count decreased {prev_transitions} → {tc}")
        prev_transitions = tc

        # UNKNOWN check
        state = r.get("vol_state", "")
        if state != "UNKNOWN":
            has_option_data = True

        # Timestamp uniqueness
        ts = r.get("timestamp", "")
        if ts and ts in seen_ts:
            violations.append(f"Line {lineno}: duplicate timestamp {ts}")
        if ts:
            seen_ts.add(ts)

    if not has_option_data:
        violations.append("⚠️ All records are UNKNOWN — no option data in this file (expected off-hours)")

    # Summary
    print(f"  State distribution:")
    state_counts = {}
    for _, r in records:
        s = r.get("vol_state", "?")
        state_counts[s] = state_counts.get(s, 0) + 1
    for s, c in sorted(state_counts.items(), key=lambda x: -x[1]):
        print(f"    {s}: {c}")

    # age range
    ages = [r.get("vol_state_age_sec", 0) for _, r in records]
    max_age = max(ages) if ages else 0
    print(f"  Max age_sec: {max_age}s ({max_age/60:.1f} min)")

    # transitions
    max_tc = max(r.get("vol_state_transition_count", 0) for _, r in records)
    print(f"  Total transitions: {max_tc}")

    if violations:
        print(f"[VALIDATE] ❌ {len(violations)} violation(s):")
        for v in violations:
            print(f"  {v}")
        return 1
    else:
        print(f"[VALIDATE] ✅ All invariants passed — {total} records clean")
        return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 validate_skew_regime_jsonl.py <path_to_jsonl>")
        sys.exit(1)
    sys.exit(validate(sys.argv[1]))
