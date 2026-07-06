# Market Regime Engine — Test Spec

## Contract Tests

### Contract 1: `data/market_regime.json` schema

File: `tests/contracts/test_market_regime_schema.py`

```python
REQUIRED_KEYS = {
    "schema_version", "generated_at", "expires_at", "age_ms",
    "previous_regime", "regime", "score", "confidence",
    "transition_count", "inputs", "features", "degraded",
}
VALID_REGIMES = {"BULL", "STRONG", "CHOP", "WEAK", "BEAR", "CRASH", "UNKNOWN"}
SCORE_RANGE = (-100, 100)
CONFIDENCE_RANGE = (0.0, 1.0)
```

Checks:
- File exists or engine produces valid output
- All REQUIRED_KEYS present
- `regime` in VALID_REGIMES
- `score` in SCORE_RANGE
- `confidence` in CONFIDENCE_RANGE
- `inputs.<name>.available` and `inputs.<name>.fresh` are booleans
- `age_ms` >= 0

### Contract 2: Fail-closed (no fallback to skew_signal.json)

File: `tests/contracts/test_market_regime_fail_closed.py`

Checks:
- market_regime.json missing → market_gate returns BLOCK_LONG, source="market_regime_missing"
- market_regime.json stale (expires_at passed) → market_gate returns BLOCK_LONG
- market_regime.json invalid JSON → market_gate returns BLOCK_LONG
- market_regime.json missing REQUIRED_KEYS → market_gate returns BLOCK_LONG
- market_regime.json valid + fresh → gate uses it (regime from file)

No fallback to skew_signal.json. skew_signal.json is an engine input, not a gate source.

### Contract 3: No existing system change

File: `tests/contracts/test_regime_no_side_effects.py`

Checks:
- FuturesBarRegimeResult still works without market_regime.json
- surface_engine still produces skew signals independently
- surface_engine still writes skew_regime JSONL for audit
- etf_regime_consumer still works independently
- `core/market_regime.py` `classify_regime()` unchanged

### Contract 4: Single Writer Rule

File: `tests/contracts/test_market_regime_single_writer.py`

Checks:
- Only `core/regime_engine.py` may write `data/market_regime.json`
- Import scan: no other file imports `market_regime` as a writer path
- `stocks/monitor.py` must not write regime file
- `stocks/market_gate.py` must not write regime file

### Contract 5: Consumer read-only behavior

File: `tests/contracts/test_regime_consumer_read_only.py`

Checks:
- `read_market_regime()` does not mutate file mtime
- Failed read returns None, does not create replacement file
- `get_current_regime()` with missing file returns "UNKNOWN", not exception

## Unit Tests

### `test_regime_engine.py`

| Test | Description |
|---|---|
| `test_compute_regime_bull` | score=80 → BULL |
| `test_compute_regime_strong` | score=45 → STRONG |
| `test_compute_regime_chop` | score=0 → CHOP |
| `test_compute_regime_weak` | score=-30 → WEAK |
| `test_compute_regime_bear` | score=-70 → BEAR |
| `test_compute_regime_crash` | score=-90 → CRASH, transitions from BEAR |
| `test_hysteresis_uphill` | prev=CHOP, score=38 → CHOP (needs 40) |
| `test_hysteresis_downhill` | prev=STRONG, score=28 → STRONG (needs 25) |
| `test_hysteresis_break_uphill` | prev=CHOP, score=42 → STRONG |
| `test_hysteresis_break_downhill` | prev=STRONG, score=22 → CHOP |
| `test_hysteresis_across_restart` | write file, simulate restart by re-reading, verify previous_regime |
| `test_degraded_one_source_fails` | futures missing → still produces score with degraded=True |
| `test_degraded_all_sources_fail` | both fail → score=0, confidence=0, regime=UNKNOWN |
| `test_write_and_read` | engine writes, consumer reads, asserts schema |
| `test_expires_at_correct` | engine writes, expires_at = generated_at + max_age_secs |
| `test_transition_count` | 3 regime changes → transition_count=3 |

### `test_regime_consumer.py`

| Test | Description |
|---|---|
| `test_read_market_regime` | valid file → returns dict with all REQUIRED_KEYS |
| `test_read_market_regime_missing` | no file → returns None |
| `test_get_current_regime` | valid file → correct regime string |
| `test_get_current_regime_missing` | no file → returns "UNKNOWN" |
| `test_get_input_status` | valid file → returns per-source dict |
| `test_get_input_status_missing` | no file → returns {"futures": {"available": False}, "index": {"available": False}} |
| `test_read_only_no_mtime_change` | read file → mtime unchanged |
| `test_read_only_no_replacement_file` | read non-existent path → no file created |

### `test_market_gate_migration.py`

| Test | Description |
|---|---|
| `test_market_regime_preferred` | both market_regime.json + skew_signal.json exist → gate uses market_regime |
| `test_market_regime_only_source` | only market_regime.json exists → gate works normally |
| `test_no_fallback_to_skew_signal` | market_regime.json missing, skew_signal.json exists → gate BLOCK_LONG |
| `test_market_regime_stale` | market_regime.json expired → gate BLOCK_LONG |
| `test_market_regime_invalid_schema` | market_regime.json has wrong keys → gate BLOCK_LONG |

### `test_skew_state_writer.py`

| Test | Description |
|---|---|
| `test_surface_engine_writes_skew_state` | after compute(), skew_state.json exists |
| `test_skew_state_jsonl_unchanged` | JSONL log still written alongside state file |
| `test_skew_state_minimal_schema` | skew_state.json has directional_skew, confidence, iv_zscore |

## Run

```bash
# Contract tests (always run)
python -m pytest tests/contracts/test_market_regime_*.py -v

# Unit tests
python -m pytest tests/test_regime_*.py -v
python -m pytest tests/test_market_gate_migration.py -v
python -m pytest tests/test_skew_state_writer.py -v

# Full suite (should not break existing tests)
python -m pytest tests/contracts/ -v --tb=short
```
