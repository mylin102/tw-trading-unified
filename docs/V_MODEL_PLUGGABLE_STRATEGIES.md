# V-Model Test Plan: Pluggable Strategy Module System

**Version**: 1.0  
**Date**: 2026-04-09  
**Linked SDD**: `docs/SDD_PLUGGABLE_STRATEGY_MODULE.md`

---

## Overview

This document defines the V-Model test plan for the pluggable strategy module system.  
Tests are organized in 4 levels following the V-Model: **Unit → Integration → System → UAT**.

Each test includes:
- **Precondition**: System state before test
- **Input**: What is fed into the system
- **Expected**: What must be true after
- **SDD Rule**: Which SDD principle is being verified

---

## Level 1: Unit Tests

### 1.1 StrategyBase ABC

**File**: `tests/strategies/test_strategy_base.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 1.1.1 | `test_cannot_instantiate_abstract` | StrategyBase imported | `StrategyBase()` | Raises TypeError | Interface contract |
| 1.1.2 | `test_concrete_subclass_succeeds` | Valid subclass with all methods | Instantiate | No error | Interface contract |
| 1.1.3 | `test_missing_on_bar_raises` | Subclass missing `on_bar()` | Instantiate | Raises TypeError | Interface contract |
| 1.1.4 | `test_missing_init_raises` | Subclass missing `init()` | Instantiate | Raises TypeError | Interface contract |
| 1.1.5 | `test_metadata_default` | Minimal subclass | `.metadata` | Returns dict with defaults | Defensive programming |
| 1.1.6 | `test_on_tick_default_noop` | Subclass instance | `.on_tick({})` | No error, no effect | Defensive programming |
| 1.1.7 | `test_cleanup_default_noop` | Subclass instance | `.cleanup()` | No error, no effect | Defensive programming |

### 1.2 Signal Dataclass

**File**: `tests/strategies/test_signal.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 1.2.1 | `test_valid_buy_signal` | Signal class imported | `Signal("BUY", "TEST", 35000)` | `validate() == (True, "")` | Side effects after validation |
| 1.2.2 | `test_valid_sell_signal` | Signal class imported | `Signal("SELL", "TEST", 35200)` | `validate() == (True, "")` | Side effects after validation |
| 1.2.3 | `test_invalid_action` | Signal class imported | `Signal("HOLD", "TEST", 35000)` | `validate() == (False, "...Invalid action...")` | Defensive programming |
| 1.2.4 | `test_missing_reason` | Signal class imported | `Signal("BUY", "", 35000)` | `validate() == (False, "...Missing reason...")` | Defensive programming |
| 1.2.5 | `test_zero_stop_loss` | Signal class imported | `Signal("BUY", "TEST", 0)` | `validate() == (False, "...Invalid stop_loss...")` | Defensive programming |
| 1.2.6 | `test_negative_stop_loss` | Signal class imported | `Signal("BUY", "TEST", -100)` | `validate() == (False, "...Invalid stop_loss...")` | Defensive programming |
| 1.2.7 | `test_bad_confidence_high` | Signal class imported | `Signal("BUY", "TEST", 35000, confidence=1.5)` | `validate() == (False, "...out of range...")` | Defensive programming |
| 1.2.8 | `test_bad_confidence_negative` | Signal class imported | `Signal("BUY", "TEST", 35000, confidence=-0.1)` | `validate() == (False, "...out of range...")` | Defensive programming |
| 1.2.9 | `test_to_dict_backward_compat` | Valid signal | `.to_dict()` | Returns dict with action, reason, stop_loss keys | Interface contract |
| 1.2.10 | `test_exit_signal_no_stop_loss_needed` | Signal class imported | `Signal("EXIT", "SL", 0)` | `validate() == (True, "")` | Defensive programming |

### 1.3 StrategyContext (Immutability)

**File**: `tests/strategies/test_strategy_context.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 1.3.1 | `test_context_is_frozen` | Valid StrategyContext | `ctx.position = PositionView(1)` | Raises FrozenInstanceError | SSOT |
| 1.3.2 | `test_position_view_isolated` | Context created from PaperTrader | Change PaperTrader.position | Context reflects new value (view, not copy) | SSOT |
| 1.3.3 | `test_market_data_readonly` | Valid context | `ctx.market.last_bar["Close"] = 99999` | Raises FrozenInstanceError | SSOT |
| 1.3.4 | `test_config_is_dict` | Context with config | `ctx.config["foo"] = "bar"` | Works (dict, but by convention read-only) | Defensive programming |
| 1.3.5 | `test_all_fields_present` | Context constructed | Access all fields | No AttributeError | Interface contract |

### 1.4 StrategyRegistry

**File**: `tests/strategies/test_strategy_registry.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 1.4.1 | `test_auto_discovers_plugins` | plugins/ directory with .py files | `Registry.discover()` | Returns list of strategy classes | Interface contract |
| 1.4.2 | `test_import_error_handled` | plugins/ with file that raises ImportError | `Registry.discover()` | Catches error, marks unavailable, no crash | Defensive programming |
| 1.4.3 | `test_get_existing_strategy` | Registry populated | `registry.get("counter_vwap")` | Returns strategy instance | Interface contract |
| 1.4.4 | `test_get_unknown_returns_none` | Registry populated | `registry.get("nonexistent")` | Returns None | Defensive programming |
| 1.4.5 | `test_list_strategies` | Registry with 2 strategies | `registry.list_all()` | Returns list of metadata dicts | Interface contract |
| 1.4.6 | `test_duplicate_names_rejected` | Two plugins with same name | `registry.register()` twice | Second replaces first, logs warning | No namespace pollution |

### 1.5 StrategyConfig

**File**: `tests/strategies/test_strategy_config.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 1.5.1 | `test_loads_valid_yaml` | Valid counter_vwap.yaml | `ConfigLoader.load(path)` | Returns dict with params | Interface contract |
| 1.5.2 | `test_rejects_unknown_keys` | YAML with `unknown_key: foo` | `ConfigLoader.load(path)` | Raises ValidationError | Defensive programming |
| 1.5.3 | `test_applies_defaults` | YAML with only `params:` | `ConfigLoader.load(path)` | Missing fields get defaults | Defensive programming |
| 1.5.4 | `test_validates_atr_mult` | YAML with `atr_sl_mult: -1` | `ConfigLoader.load(path)` | Raises ValidationError (must be >0) | Defensive programming |
| 1.5.5 | `test_validates_confirm_bars` | YAML with `confirm_bars: 0` | `ConfigLoader.load(path)` | Raises ValidationError (must be >=1) | Defensive programming |
| 1.5.6 | `test_file_not_found` | Non-existent path | `ConfigLoader.load(path)` | Returns default config, logs warning | Defensive programming |

### 1.6 Migrated Strategy Plugins

**File**: `tests/strategies/test_counter_vwap_plugin.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 1.6.1 | `test_returns_none_when_no_signal` | Ranging market, no squeeze fire | Mock context (no setup) | `on_bar()` returns None | Side effects after validation |
| 1.6.2 | `test_returns_buy_on_bullish_fire` | Squeeze fire bullish setup | Mock context with fire state | Returns Signal("BUY", ..., stop_loss>0) | Interface contract |
| 1.6.3 | `test_returns_sell_on_bearish_fire` | Squeeze fire bearish setup | Mock context with fire state | Returns Signal("SELL", ..., stop_loss>0) | Interface contract |
| 1.6.4 | `test_stop_loss_is_absolute` | Any signal | `signal.stop_loss` | Value > 10000 (price level, not points) | Defensive programming |
| 1.6.5 | `test_init_sets_internal_state` | Fresh plugin | `plugin.init(ctx)` | Internal counters initialized | Interface contract |
| 1.6.6 | `test_metadata_correct` | Plugin instance | `plugin.metadata` | asset_class="futures", pf>0 | Interface contract |

**File**: `tests/strategies/test_spring_upthrust_plugin.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 1.7.1 | `test_returns_spring_on_bullish_setup` | Spring setup conditions met | Mock context | Returns Signal("BUY", reason="SPRING") | Interface contract |
| 1.7.2 | `test_returns_upthrust_on_bearish_setup` | Upthrust setup conditions met | Mock context | Returns Signal("SELL", reason="UPTHRUST") | Interface contract |
| 1.7.3 | `test_returns_none_when_no_setup` | No spring/upthrust pattern | Mock context | Returns None | Side effects after validation |

---

## Level 2: Integration Tests

### 2.1 Plugin → PaperTrader Integration

**File**: `tests/strategies/test_plugin_paper_trader.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 2.1.1 | `test_buy_increases_position` | PaperTrader.position==0 | Valid BUY Signal → execute_signal() | position==1, entry_price set | SSOT |
| 2.1.2 | `test_sell_opENS_short` | PaperTrader.position==0 | Valid SELL Signal → execute_signal() | position==-1, entry_price set | SSOT |
| 2.1.3 | `test_exit_zeroes_position` | PaperTrader.position==1 | EXIT Signal → execute_signal() | position==0, PnL recorded | SSOT |
| 2.1.4 | `test_reject_duplicate_buy` | PaperTrader.position==1, max==1 | Another BUY Signal → execute_signal() | Returns None, position unchanged | Side effects after validation |
| 2.1.5 | `test_reject_exit_when_flat` | PaperTrader.position==0 | EXIT Signal → execute_signal() | Returns None | Side effects after validation |
| 2.1.6 | `test_pnl_includes_fees` | Long position, exit at lower price | EXIT Signal | PnL = gross - fees - tax | Defensive programming |

### 2.2 Plugin → Backtest Integration

**File**: `tests/strategies/test_plugin_backtest.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 2.2.1 | `test_counter_vwap_parity` | Same CSV data as original backtest | Backtest with plugin | PF=1.95±0.01, WR=40.7±1%, 86±2 trades | SSOT (same results) |
| 2.2.2 | `test_spring_upthrust_parity` | Same CSV data as original backtest | Backtest with plugin | PF=3.36±0.01, 33±2 trades | SSOT (same results) |
| 2.2.3 | `test_no_signal_bars_skipped` | Data with no squeeze fire | Backtest run | 0 trades, no errors | Side effects after validation |

### 2.3 Hot-Swap

**File**: `tests/strategies/test_hot_swap.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 2.3.1 | `test_swap_when_flat` | Position==0, strategy A active | `swap("strategy_b")` | Returns OK, next bar uses B | Interface contract |
| 2.3.2 | `test_reject_when_long` | Position==1 | `swap("strategy_b")` | Returns error, strategy unchanged | SSOT |
| 2.3.3 | `test_reject_when_short` | Position==-1 | `swap("strategy_b")` | Returns error, strategy unchanged | SSOT |
| 2.3.4 | `test_cleanup_called_on_old` | Strategy A active with state | `swap("strategy_b")` | A.cleanup() called | Defensive programming |
| 2.3.5 | `test_init_called_on_new` | Swap to strategy B | First bar after swap | B.init() called before B.on_bar() | Interface contract |
| 2.3.6 | `test_unknown_strategy_error` | Any state | `swap("nonexistent")` | Returns error, no crash | Defensive programming |

### 2.4 Malformed Signal Handling

**File**: `tests/strategies/test_malformed_signal.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 2.4.1 | `test_no_action_key` | Monitor receives dict without "action" | Signal dict | Rejected, audit log written | Side effects after validation |
| 2.4.2 | `test_stop_loss_is_none` | Monitor receives stop_loss=None | Signal dict | Rejected, audit log written | Defensive programming |
| 2.4.3 | `test_reason_is_empty` | Monitor receives reason="" | Signal dict | Rejected, audit log written | Defensive programming |

---

## Level 3: System Tests

### 3.1 Full Paper Trading Cycle

**File**: `tests/strategies/test_full_paper_cycle.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 3.1.1 | `test_entry_tp1_exit_cycle` | Monitor running, position==0 | Bars that trigger entry → TP1 → SL exit | Position goes 0→1→0, PnL correct, CSV has 3 rows | SSOT |
| 3.1.2 | `test_vwap_exit_cycle` | Monitor running, long position | Bars where price crosses below VWAP | Position goes 1→0, reason="VWAP" | SSOT |
| 3.1.3 | `test_eod_exit_cycle` | Monitor running, long position | Time advances past EOD panic time | Position goes 1→0, reason="EOD" | SSOT |

### 3.2 Restart Recovery

**File**: `tests/strategies/test_restart_recovery.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 3.2.1 | `test_paper_position_recovered` | Position==1 from ledger CSV | Restart monitor | position==1, entry_price matches | SSOT |
| 3.2.2 | `test_no_duplicate_entry_after_restart` | Position==1 from ledger CSV | Restart → same signal fires | No second entry, audit log shows "already_holding" | Side effects after validation |
| 3.2.3 | `test_tp1_state_recovered` | Position==1, TP1 hit, stop at breakeven | Restart monitor | stop_loss==entry_price, has_tp1_hit==True | SSOT |

### 3.3 Stress Testing

**File**: `tests/strategies/test_stress.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 3.3.1 | `test_rapid_ticks_no_duplicate` | Position==0 | 100 ticks in 1 second | Max 1 entry | Side effects after validation |
| 3.3.2 | `test_overnight_date_rollover` | Position==1 at 23:55 | Bars crossing midnight | No crash, position preserved | Defensive programming |
| 3.3.3 | `test_nan_in_market_data` | Market data has NaN in Close | Strategy on_bar() | No crash, returns None or uses fallback | Defensive programming |

---

## Level 4: UAT Checklist

**File**: `docs/UAT_PLUGGABLE_STRATEGIES.md` (manual checklist)

### 4.1 Pre-Flight

- [ ] `python3 -m pytest tests/ -v` — all tests pass
- [ ] `python3 -m pytest tests/strategies/ -v` — all new tests pass
- [ ] No import errors: `python3 -c "from core.strategy_base import StrategyBase"`

### 4.2 Startup

- [ ] `python3 main.py --dry-run` starts without error
- [ ] Log shows: `[FuturesMonitor] Using strategy: counter_vwap`
- [ ] Log shows: `StrategyRegistry: discovered 2 futures plugins`

### 4.3 Strategy Switching

- [ ] Dashboard shows strategy list (auto-populated from registry)
- [ ] Switch from Counter-VWAP to Spring when flat → log confirms switch
- [ ] Attempt switch when position open → error message in log

### 4.4 Paper Trading

- [ ] 1 entry → position becomes 1, CSV row written
- [ ] TP1 hit → position becomes 0 (or reduced), CSV row written
- [ ] SL hit → position becomes 0, PnL = entry - SL - fees

### 4.5 Restart

### 3.4 Attribution System Tests

**File**: `tests/core/test_attribution_recorder.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 3.4.1 | `test_attribution_recorder_init` | AttributionRecorder imported | `AttributionRecorder()` | Instance created with empty buffers | Interface contract |
| 3.4.2 | `test_log_router_row` | Recorder instance | `log_router_row(...)` | Row added to buffer | Data integrity |
| 3.4.3 | `test_log_signal` | Recorder instance | `log_signal(...)` | Signal row added | Data integrity |
| 3.4.4 | `test_log_trade` | Recorder instance | `log_trade(...)` | Trade row added | Data integrity |
| 3.4.5 | `test_export_csv` | Recorder with data | `export_csv("./tmp")` | CSV files created | Side effects after validation |
| 3.4.6 | `test_buffer_flush_logic` | Recorder with buffer_size=3 | Add 4 rows | Auto-flush triggered | Defensive programming |
| 3.4.7 | `test_summarize_router` | Sample router data | `summarize_router(df)` | Correct summary stats | Data integrity |
| 3.4.8 | `test_starvation_report` | Simulated shadowing data | `build_starvation_report(df)` | Correct starvation levels | Business logic |
| 3.4.9 | `test_priority_impact_calculation` | Shadowed strategy data | `summarize_router(df)` | Correct priority_impact | Business logic |
| 3.4.10 | `test_merge_router_and_trade` | Router + trade data | `merge_router_and_trade_summary()` | Combined metrics | Data integrity |

**File**: `tests/core/test_attribution_recorder_integration.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 3.4.11 | `test_csv_export_creates_files` | Recorder with output_dir | Add data + export | CSV files with headers | Integration |
| 3.4.12 | `test_csv_append_mode` | Existing CSV file | Append more data | Combined data preserved | Integration |
| 3.4.13 | `test_multiple_csv_files` | Recorder with all data types | Export | Router, signal, trade files | Integration |
| 3.4.14 | `test_buffer_size_respected` | Recorder buffer_size=3 | Add 2 rows, then 3rd | Flush on 3rd row | Integration |
| 3.4.15 | `test_clear_buffers_after_export` | Recorder with data | Export | Buffers empty after | Integration |
| 3.4.16 | `test_export_with_no_data` | Empty recorder | Force export | Empty files created | Defensive programming |

### 3.5 Router Attribution Integration Tests

**File**: `tests/core/test_futures_strategy_router.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 3.5.1 | `test_router_with_attribution` | Router + AttributionRecorder | Route signal | Attribution logged | Integration |
| 3.5.2 | `test_attribution_backward_compat` | Router without recorder | Route signal | No error, normal operation | Backward compatibility |
| 3.5.3 | `test_attribution_logs_all_candidates` | Router with recorder | Route signal | All candidates logged | Data integrity |
| 3.5.4 | `test_attribution_shadowed_status` | Multiple candidates, first wins | Route signal | Lower priority marked "shadowed" | Business logic |

---

## Level 4: User Acceptance Tests (UAT)

### 4.1 Attribution Report Generation

**Script**: `scripts/attribution_report.py`

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 4.1.1 | `test_report_generation` | Sample attribution data | Run script | All report files created | End-to-end |
| 4.1.2 | `test_strategy_detail_report` | Specific strategy filter | `--strategy kbar_feature` | Detailed JSON/CSV created | User workflow |
| 4.1.3 | `test_regime_filtered_report` | Regime filter | `--regime WEAK` | Filtered analysis | User workflow |
| 4.1.4 | `test_visualization_generation` | Matplotlib available | Run without `--summary-only` | PNG charts created | User experience |

### 4.2 Production Simulation

**Manual Test**: Run router with attribution in backtest

| # | Test Name | Precondition | Input | Expected | SDD Rule |
|---|-----------|-------------|-------|----------|----------|
| 4.2.1 | `test_attribution_in_backtest` | Full backtest dataset | Enable attribution | CSV files with real data | Production readiness |
| 4.2.2 | `test_starvation_analysis` | Generated attribution data | Run report script | Actionable insights | Business value |
| 4.2.3 | `test_priority_adjustment` | High starvation detected | Adjust priority order | Improved evaluation rate | Continuous improvement |

---

## Implementation Priority

| Priority | Tests | Rationale |
|----------|-------|-----------|
| **P0** | 1.1, 1.2, 1.3, 1.4, 1.5 | Foundation — without these, nothing else works |
| **P0** | 1.6, 1.7 | Migrated strategies must work before monitor integration |
| **P1** | 2.1, 2.2, 2.3, 2.4 | Integration — ensure plugins work with PaperTrader and backtest |
| **P1** | 3.1, 3.2 | System — full cycle and restart are critical for production |
| **P1** | 3.4, 3.5 | Attribution — monitoring and optimization capability |
| **P2** | 3.3 | Stress — important but not blocking |
| **P2** | Level 4 UAT | Manual verification before deployment |

---

## Test Execution Commands

```bash
# Level 1: Unit tests (fast)
python3 -m pytest tests/strategies/test_strategy_base.py -v
python3 -m pytest tests/strategies/test_signal.py -v
python3 -m pytest tests/strategies/test_strategy_context.py -v
python3 -m pytest tests/strategies/test_strategy_registry.py -v
python3 -m pytest tests/strategies/test_strategy_config.py -v
python3 -m pytest tests/strategies/test_counter_vwap_plugin.py -v
python3 -m pytest tests/strategies/test_spring_upthrust_plugin.py -v
python3 -m pytest tests/strategies/test_KbarFeature.py -v

# Level 2: Integration tests
python3 -m pytest tests/strategies/test_plugin_paper_trader.py -v
python3 -m pytest tests/strategies/test_plugin_backtest.py -v
python3 -m pytest tests/strategies/test_hot_swap.py -v
python3 -m pytest tests/strategies/test_malformed_signal.py -v

# Level 3: System tests
python3 -m pytest tests/strategies/test_full_paper_cycle.py -v
python3 -m pytest tests/strategies/test_restart_recovery.py -v
python3 -m pytest tests/strategies/test_stress.py -v

# Level 3.4: Attribution system tests
python3 -m pytest tests/core/test_attribution_recorder.py -v
python3 -m pytest tests/core/test_attribution_recorder_integration.py -v

# Level 3.5: Router attribution tests
python3 -m pytest tests/core/test_futures_strategy_router.py -v

# All strategy tests
python3 -m pytest tests/strategies/ -v

# Full test suite (including existing tests — regression guard)
python3 -m pytest tests/ -v

# Attribution report script test
python3 scripts/test_attribution_report.py
```

---

## Exit Criteria

**Phase 1** (Foundation): All Level 1 tests pass (7 files, ~30 tests)  
**Phase 2** (Migrate): Level 1 + Level 2.2 (backtest parity) pass  
**Phase 3** (Monitor): Level 1 + Level 2.1 + Level 2.3 pass  
**Phase 4** (Attribution): Level 3.4 + 3.5 tests pass  
**Phase 5** (Backtest): All Level 2 tests pass  
**Phase 6** (Options): Level 1-3 pass for options plugins  
**Full Release**: All Level 1-4 + Level 4 UAT pass, 0 regressions in existing tests
