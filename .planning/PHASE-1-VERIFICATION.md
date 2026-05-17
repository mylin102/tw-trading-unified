---
phase: 1.2-adaptive-verification
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - config/futures.yaml
  - config/futures_day.yaml
  - tests/strategies/test_tmf_spread_atr.py
  - strategies/plugins/futures/active/tmf_spread.py
autonomous: true
requirements: [ADAPT-01, ADAPT-02, ADAPT-03, VMDL-01, VIEW-02]
must_haves:
  truths:
    - "Strategy skips entry when ATR is below min_atr threshold"
    - "Release stop and trailing distance scale with ATR using specified multipliers"
    - "Dynamic thresholds are written to /tmp/mts_position_state.json for dashboard visibility"
    - "Unit tests cover ATR scaling, floors, and filtering logic"
  artifacts:
    - path: "tests/strategies/test_tmf_spread_atr.py"
      provides: "Unit tests for adaptive ATR logic"
  key_links:
    - from: "strategies/plugins/futures/active/tmf_spread.py"
      to: "/tmp/mts_position_state.json"
      via: "_write_mts_state"
---

<objective>
Finalize and verify the ATR-based improvements for the `tmf_spread` strategy to ensure dynamic volatility adaptation and dashboard visibility.

Purpose: Reduce transaction friction in low-volatility environments and improve capture quality during trending releases.
Output: Locked production configuration, comprehensive unit tests, and verified dashboard data flow.
</objective>

<execution_context>
@$HOME/.gemini/get-shit-done/workflows/execute-plan.md
</execution_context>

<context>
@.planning/MAP-ATR-OPTIMIZATION.md
@strategies/plugins/futures/active/tmf_spread.py
@config/futures.yaml
@config/futures_day.yaml
</context>

<tasks>

<task type="auto">
  <name>Task 1: Verify and Lock Production Configuration</name>
  <files>config/futures.yaml, config/futures_day.yaml</files>
  <action>
    Update the `mts.params` in `config/futures.yaml` and ensure `config/futures_day.yaml` is aligned for the next session.
    Target parameters:
    - `min_atr`: 10.0
    - `atr_multiplier_stop`: 2.0
    - `atr_multiplier_trail`: 3.5
    - `release_stop_points`: 20 (fallback)
    - `trail_distance_points`: 30 (fallback)
  </action>
  <verify>
    <automated>grep -A 10 "mts:" config/futures.yaml | grep "min_atr: 10.0"</automated>
  </verify>
  <done>Production configuration reflects the optimized adaptive parameters.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Implement ATR-Scaled Logic Unit Tests</name>
  <files>tests/strategies/test_tmf_spread_atr.py</files>
  <behavior>
    - ATR Filter: If bar.atr < min_atr, on_bar returns None with skip_reason "ATR_TOO_LOW".
    - Scaling: If ATR=12.0, multiplier=2.0, release_stop should be 24.0.
    - Floors: If ATR=1.0, multiplier=2.0, release_stop should be 5.0 (hard floor).
    - Trailing Floor: If ATR=1.0, multiplier=3.5, trail_dist should be 10.0 (hard floor).
    - State: _write_mts_state must receive the calculated dynamic thresholds.
  </behavior>
  <action>
    Create `tests/strategies/test_tmf_spread_atr.py` using `pytest`. Mock `StrategyContext` and `MarketData`.
    Verify the `_get_thresholds` internal logic and the `on_bar` entry/management paths.
  </action>
  <verify>
    <automated>python3 -m pytest tests/strategies/test_tmf_spread_atr.py -v</automated>
  </verify>
  <done>Comprehensive unit tests verify all ATR-adaptive behaviors and safety floors.</done>
</task>

<task type="auto">
  <name>Task 3: Verify Dashboard Threshold Visibility</name>
  <files>strategies/plugins/futures/active/tmf_spread.py, tests/strategies/test_tmf_spread_atr.py</files>
  <action>
    Ensure `_write_mts_state` in `tmf_spread.py` correctly handles the dynamic thresholds.
    Add a test case to `test_tmf_spread_atr.py` that specifically checks if `release_stop_points` and `trail_distance_points` are correctly populated in the MTS state.
  </action>
  <verify>
    <automated>python3 -m pytest tests/strategies/test_tmf_spread_atr.py -k "test_mts_state_thresholds"</automated>
  </verify>
  <done>Dashboard visibility of dynamic thresholds is confirmed through automated state inspection.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries
| Boundary | Description |
|----------|-------------|
| Config -> Strategy | Strategy must handle missing or invalid config values gracefully |
| Market Data -> Strategy | Strategy must handle missing or NaN ATR values |

## STRIDE Threat Register
| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-1.2-01 | DOS | ATR Calculation | mitigate | Hard floors (5pt/10pt) prevent zero/negative stops if ATR is anomalously low or missing |
| T-1.2-02 | Info Leak | MTS State | accept | /tmp/mts_position_state.json contains trading state, local access required |
</threat_model>

<success_criteria>
1. `config/futures.yaml` locked with verified adaptive parameters.
2. `tests/strategies/test_tmf_spread_atr.py` passes with 100% coverage of ATR logic.
3. MTS state file confirmed to contain dynamic thresholds for operator visibility.
</success_criteria>

<output>
After completion, create `.planning/phases/1.2-adaptive-verification/1.2-01-SUMMARY.md`
</output>
