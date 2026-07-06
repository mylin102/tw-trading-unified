# SDD: Squeeze Pattern Integration into Stock Backtest

| Field | Value |
|-------|-------|
| **Branch** | `feat/squeeze-stock-strategies` |
| **Author** | AI |
| **Date** | 2026-04-06 |
| **Status** | Draft |

---

## 1. Requirements

### 1.1 Goal
Add squeeze-backtest's 3 pattern classifications (squeeze / houyi / whale) and 6 TW-optimized strategy presets into the existing Streamlit stock backtest page.

### 1.2 In Scope
- Pattern classification functions that run on existing 5min OHLCV data
- Pydantic strategy parameter schema for validation
- 6 strategy presets (TW only) exposed as dropdown in `stock_optimizer.py`
- Filter layer in `signal_generator.py` that applies StrategyParams before signal generation

### 1.3 Out of Scope
- US / CN markets
- Options backtesting
- Live execution changes
- Modifying `stock_engine.py` (Numba simulator)

---

## 2. Architecture

### 2.1 Data Flow

```
5min CSV → calculate_futures_squeeze() → existing DataFrame
                                           ↓
                              squeeze_patterns.py (NEW)
                              ├── classify_houyi(df)
                              └── classify_whale(df)
                                           ↓
                              signal_generator.py (MODIFIED)
                              └── apply_strategy_filters(df, StrategyParams)
                                           ↓
                              stock_engine.py (UNCHANGED)
                              └── Numba bar-by-bar simulation
```

### 2.2 New Files

| File | Purpose | Lines |
|------|---------|-------|
| `core/strategy_schema.py` | Pydantic StrategyParams model | ~50 |
| `strategies/stocks/squeeze_patterns.py` | houyi/whale classification | ~80 |
| `tests/test_squeeze_patterns.py` | Unit tests for pattern classification | ~60 |
| `tests/test_strategy_schema.py` | Unit tests for schema validation | ~40 |

### 2.3 Modified Files

| File | Change | Lines |
|------|--------|-------|
| `backtest/signal_generator.py` | Add `apply_strategy_filters()` function | ~40 |
| `ui/backtest_pages/stock_optimizer.py` | Add strategy preset dropdown | ~60 |

---

## 3. Interface Specifications

### 3.1 StrategyParams Schema

```python
class StrategyParams(BaseModel):
    # Signal filters
    min_momentum: Optional[float] = None
    max_momentum: Optional[float] = None
    min_energy_level: Optional[int] = None
    require_squeeze_on: bool = False
    require_fired: bool = False
    min_value_score: Optional[float] = None

    # Pattern selection
    patterns: List[str] = []  # ["squeeze", "houyi", "whale"]
    allowed_regimes: Optional[List[str]] = None  # ["bull_trend", "bear_trend", "range_bound"]

    # Exit
    holding_days: int = 14
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
```

### 3.2 Pattern Classification API

```python
def classify_houyi(df: pd.DataFrame) -> pd.Series:
    """Returns boolean Series: True where houyi pattern detected."""

def classify_whale(df: pd.DataFrame) -> pd.Series:
    """Returns boolean Series: True where whale pattern detected."""

def apply_squeeze_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Add 'pattern' column with values: 'squeeze', 'houyi', 'whale', or None."""
```

### 3.3 Strategy Presets (TW Only)

| Name | patterns | min_momentum | require_squeeze_on | holding_days |
|------|----------|-------------|-------------------|-------------|
| `baseline` | [squeeze, houyi, whale] | - | - | 14 |
| `squeeze_only` | [squeeze] | - | True | 14 |
| `whale_alignment` | [whale] | - | - | 10 |
| `conservative` | [squeeze, whale] | 0.02 | True | 14 |
| `scalping` | [squeeze] | 0.15 | True | 3 |
| `custom` | - | - | - | 14 (manual) |

---

## 4. V-Model Test Plan

### 4.1 Unit Tests (left side of V)

| Test | What It Verifies |
|------|-----------------|
| `test_strategy_schema_valid` | Pydantic rejects invalid params |
| `test_strategy_schema_defaults` | Default values match spec |
| `test_classify_houyi_basic` | houyi detection on known pattern |
| `test_classify_whale_basic` | whale detection on known pattern |
| `test_apply_squeeze_patterns_columns` | Output DataFrame has 'pattern' column |
| `test_apply_strategy_filters` | Filters correctly remove rows |

### 4.2 Integration Tests

| Test | What It Verifies |
|------|-----------------|
| `test_signal_generator_with_squeeze_params` | signal_generator accepts StrategyParams |
| `test_stock_optimizer_preset_loads` | UI dropdown loads all 6 presets |

### 4.3 Acceptance Criteria (right side of V)

| Criterion | Pass Condition |
|-----------|---------------|
| All 6 strategy presets appear in Streamlit dropdown | UI renders without error |
| Selecting a preset runs backtest with correct filters | Backtest completes, results differ by preset |
| No regression in existing 4 stock strategies | mean_reversion, arbitrage_lite, momentum_breakout, scout_strategy still work |
| No new dependencies beyond pydantic (already in squeeze-backtest) | `pip list` unchanged except pydantic |

---

## 5. Implementation Order (V-model left-to-right)

```
Phase 1: Schema + Tests
  1.1 Write core/strategy_schema.py
  1.2 Write tests/test_strategy_schema.py
  1.3 Run tests → green

Phase 2: Pattern Classification + Tests
  2.1 Write strategies/stocks/squeeze_patterns.py
  2.2 Write tests/test_squeeze_patterns.py
  2.3 Run tests → green

Phase 3: Signal Generator Integration + Tests
  3.1 Modify backtest/signal_generator.py
  3.2 Write integration test
  3.3 Run tests → green

Phase 4: UI Integration
  4.1 Modify ui/backtest_pages/stock_optimizer.py
  4.2 Manual test: select each preset, verify backtest runs
  4.3 Regression test: existing strategies still work
```

---

## 6. Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| houyi/whale patterns depend on data not in 5min CSV | High | Define patterns from existing columns (momentum, EMA alignment, volume) |
| Pydantic not installed in tw-trading-unified | Medium | Add to requirements.txt, or use dataclass instead |
| Streamlit UI becomes slow with extra filtering | Low | Filters are pandas vectorized, O(n) per strategy |
