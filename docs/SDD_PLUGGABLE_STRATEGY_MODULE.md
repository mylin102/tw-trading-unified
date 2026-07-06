# SDD Design Document: Pluggable Strategy Module System

**Version**: 1.0  
**Date**: 2026-04-09  
**Author**: GSD Review  
**Status**: Design — Ready for Implementation

---

## 1. Problem Statement

### 1.1 Current State

The trading system has **3 monolithic strategy sources**:

| Source | Lines | Problem |
|--------|-------|---------|
| `strategies/futures/monitor.py` | ~950 | Counter-VWAP logic hardcoded inside monitor class |
| `strategies/futures/entry_strategies.py` | ~420 | 10 strategies in single file, 8 confirmed losers |
| `strategies/options/live_options_squeeze_monitor.py` | ~1900 | V2 squeeze + ThetaGang + contract management all mixed |

**Adding a new strategy requires:**
1. Writing a function in `entry_strategies.py`
2. Adding it to the `STRATEGIES` dict
3. Updating `monitor.py` if new state tracking is needed
4. Adding config keys to `futures.yaml`
5. Updating `signal_generator.py` registry merge logic

**Result**: Tightly coupled, error-prone, non-reusable.

### 1.2 Target State

**Drop-in strategy modules**: Add `.py` file + `.yaml` config → system auto-discovers and runs it.

---

## 2. Design Principles (SDD Compliance)

### SDD Rule 1: Single Source of Truth (SSOT)

**`PaperTrader.position` remains the sole authority for position state.**

Strategies receive a **read-only view** — they never mutate position directly.

```
Strategy ──reads──> StrategyContext.position (read-only view)
                  ──outputs──> Signal dict {"action", "reason", "stop_loss"}
Monitor ──executes──> PaperTrader.execute_signal()
PaperTrader ──updates──> self.position (SSOT)
```

**Anti-pattern (NEVER do):**
```python
# BAD: Strategy directly mutates position
strategy.trader.position = 1  # Violates SSOT
```

### SDD Rule 2: Side Effects After Validation

Strategy signals must pass validation **before** any state change or file write.

```
Signal received
  → Validate format (action, reason, stop_loss)
  → Validate preconditions (position==0, margin sufficient, price>0)
  → Execute PaperTrader.execute_signal()
  → [ONLY IF SUCCESS] Write audit log, CSV, notification
```

### SDD Rule 3: Defensive Programming

All external inputs are untrusted:
- Strategy plugins may return malformed signals → validate before use
- Config files may have missing/wrong keys → Pydantic validation with defaults
- Market data may have NaN/None → fallback to safe values
- Plugin imports may fail → catch ImportError, mark unavailable

### SDD Rule 4: No Namespace Pollution

- Each strategy is a separate module — no global variables
- Strategy state is encapsulated in instance attributes, not module-level
- Registry uses strategy name as namespace key

---

## 3. Architecture

### 3.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        Monitor (thin)                           │
│  FuturesMonitor / OptionsMonitor                                │
│  - Tick/bar ingestion                                           │
│  - Indicator calculation (calculate_futures_squeeze)            │
│  - Position management (TP1, SL, trailing, VWAP, EOD)          │
│  - Order execution (PaperTrader / live broker)                  │
│  - CSV logging                                                  │
└──────────────────────┬──────────────────────────────────────────┘
                       │ delegates to
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    StrategyRegistry                             │
│  - Auto-discovery from strategies/plugins/{futures,options}/    │
│  - Config loading from config/strategies/*.yaml                 │
│  - Hot-swap management (only when position==0)                  │
│  - Metadata catalog (PF, Win%, MaxDD, regime)                  │
└──────────────────────┬──────────────────────────────────────────┘
                       │ loads
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│              Strategy Plugins (drop-in .py files)                │
│  strategies/plugins/futures/counter_vwap.py                     │
│  strategies/plugins/futures/spring_upthrust.py                  │
│  strategies/plugins/options/v2_squeeze.py                       │
│  strategies/plugins/options/iron_condor.py                      │
│                                                                 │
│  Each plugin:                                                   │
│    class MyStrategy(StrategyBase):                              │
│        def init(self, ctx): ...                                 │
│        def on_bar(self, ctx) -> Signal: ...                     │
└──────────────────────┬──────────────────────────────────────────┘
                       │ receives
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    StrategyContext (immutable)                   │
│  - market: MarketData (OHLCV + all indicator columns)           │
│  - position: PositionView (read-only: size, entry_price, pnl)   │
│  - config: ReadOnlyDict (this strategy's params only)           │
│  - clock: BarTimestamp                                          │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 Module Structure

```
core/
├── strategy_base.py          # StrategyBase ABC
├── strategy_context.py       # StrategyContext + PositionView + MarketData
├── strategy_registry.py      # Auto-discovery + hot-swap manager
├── strategy_config.py        # YAML loader + Pydantic validator
└── signal.py                 # Signal dataclass + validator

strategies/
├── plugins/                   # NEW: drop-in strategy modules
│   ├── futures/
│   │   ├── __init__.py       # auto-register all
│   │   ├── counter_vwap.py
│   │   └── spring_upthrust.py
│   └── options/
│       ├── __init__.py
│       ├── v2_squeeze.py
│       └── iron_condor.py
│
├── futures/
│   ├── monitor.py            # MODIFIED: delegates to registry
│   ├── entry_strategies.py   # DEPRECATED (migrate → plugins/)
│   └── elite_strategies.py   # DEPRECATED (migrate → plugins/)
│
└── options/
    └── live_options_squeeze_monitor.py  # MODIFIED: delegates to registry

config/
├── futures.yaml              # TRIMMED: only active_strategy reference
├── options_strategy.yaml     # TRIMMED: only active_strategy reference
└── strategies/               # NEW: per-strategy configs
    ├── counter_vwap.yaml
    ├── spring_upthrust.yaml
    ├── v2_squeeze.yaml
    └── iron_condor.yaml

backtest/
├── signal_generator.py       # MODIFIED: uses registry
└── strategy_adapters.py      # BacktestContext → StrategyContext adapter

tests/
└── strategies/
    ├── test_strategy_base.py
    ├── test_strategy_context.py
    ├── test_strategy_registry.py
    ├── test_strategy_config.py
    ├── test_signal.py
    ├── test_counter_vwap_plugin.py
    ├── test_spring_upthrust_plugin.py
    └── test_hot_swap.py
```

---

## 4. Interface Definitions

### 4.1 StrategyBase (ABC)

```python
from abc import ABC, abstractmethod
from core.strategy_context import StrategyContext
from core.signal import Signal

class StrategyBase(ABC):
    """Pluggable strategy interface. All subclasses must implement init() and on_bar()."""

    # ── Required Properties ──

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier, matches plugin filename (e.g., 'counter_vwap')."""
        ...

    @property
    def metadata(self) -> dict:
        """Strategy metadata for dashboard and registry catalog."""
        return {
            "asset_class": "futures",   # "futures" or "options"
            "version": "1.0",
            "backtest_pf": 0.0,
            "backtest_wr": 0.0,
            "backtest_maxdd": 0.0,
            "market_regime": "all",     # "ranging", "trending", "all"
            "description": "",
        }

    # ── Required Lifecycle Hooks ──

    @abstractmethod
    def init(self, context: StrategyContext) -> None:
        """Called once when strategy is activated. Setup internal state here.
        
        SDD Precondition: context.position == 0 (strategy only activates when flat)
        SDD Postcondition: strategy internal state is initialized
        """
        ...

    @abstractmethod
    def on_bar(self, context: StrategyContext) -> Signal | None:
        """Called on each new bar. Return Signal or None.
        
        SDD Precondition: context.market has valid OHLCV + indicators
        SDD Postcondition: if Signal returned, it passes signal_validator
        """
        ...

    # ── Optional Lifecycle Hooks ──

    def on_tick(self, tick: dict) -> None:
        """Called on each tick. Default: no-op.
        Override for tick-level strategies (e.g., scalping).
        """
        pass

    def cleanup(self) -> None:
        """Called when strategy is deactivated. Default: no-op.
        Use to flush buffers, log summary, etc.
        """
        pass

    # ── Config Validation ──

    @property
    def config_schema(self) -> type | None:
        """Optional: Pydantic model for validating this strategy's config.
        Returns None to use default StrategyParams validation.
        """
        return None
```

### 4.2 Signal (Dataclass)

```python
from dataclasses import dataclass

@dataclass
class Signal:
    """Strategy output signal. Replaces the ad-hoc dict convention."""
    action: str          # "BUY" | "SELL" | "EXIT" | "PARTIAL_EXIT"
    reason: str          # e.g., "COUNTER_VWAP", "SPRING", "UPTHRUST"
    stop_loss: float     # Absolute price level (not points)
    target: float = 0.0  # Optional: take-profit target price
    confidence: float = 1.0  # 0.0-1.0, for strategy weighting

    def validate(self) -> tuple[bool, str]:
        """SDD Rule 2: Validate before execution."""
        if self.action not in ("BUY", "SELL", "EXIT", "PARTIAL_EXIT"):
            return False, f"Invalid action: {self.action}"
        if not self.reason:
            return False, "Missing reason"
        if self.action in ("BUY", "SELL") and self.stop_loss <= 0:
            return False, f"Invalid stop_loss: {self.stop_loss}"
        if not (0.0 <= self.confidence <= 1.0):
            return False, f"Confidence out of range: {self.confidence}"
        return True, ""

    def to_dict(self) -> dict:
        """Backward compatibility with existing dict-based code."""
        return {
            "action": self.action,
            "reason": self.reason,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "confidence": self.confidence,
        }
```

### 4.3 StrategyContext (Immutable)

```python
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class PositionView:
    """Read-only view of position state. Never mutated by strategy."""
    size: int = 0               # +N long, -N short, 0 flat
    entry_price: float = 0.0
    current_stop_loss: float | None = None
    unrealized_pnl: float = 0.0
    has_tp1_hit: bool = False

@dataclass(frozen=True)
class MarketData:
    """Current bar data with all indicator columns."""
    last_bar: dict              # Latest bar as dict (OHLCV + indicators)
    df_5m: Any | None = None    # Recent 5m DataFrame (for lookback)
    df_15m: Any | None = None   # Recent 15m DataFrame
    timestamp: str = ""
    session: int = 0            # 1=day, 2=night

@dataclass(frozen=True)
class StrategyContext:
    """Immutable context passed to strategy on each bar.
    
    SDD Rule 1: This is a VIEW, not a copy. Strategies cannot mutate SSOT.
    """
    market: MarketData
    position: PositionView
    config: dict                # This strategy's params (read-only by convention)
    bar_counter: int = 0        # Monotonic bar counter
```

### 4.4 Per-Strategy Config (YAML)

```yaml
# config/strategies/counter_vwap.yaml
name: counter_vwap
asset_class: futures
version: "2.0"
enabled: true

# Strategy-specific parameters (validated by Pydantic if schema provided)
params:
  confirm_bars: 5
  atr_sl_mult: 2.0
  exit_on_vwap: true
  auto_regime: true

# Risk management (enforced by monitor, not strategy)
risk:
  max_positions: 1
  stop_loss_type: atr    # "atr" | "fixed" | "percent"
  stop_loss_mult: 2.0

# Market regime filter
regime_filter:
  allowed: ["ranging", "squeeze"]
  min_adx: 0

# Backtest metadata (for dashboard catalog)
backtest:
  pf: 1.95
  wr: 40.7
  max_dd: -7.2
  total_trades: 86
  period: "2026-Q1"
```

---

## 5. Data Flow

### 5.1 Live Trading (Futures)

```
┌─────────────┐     tick      ┌──────────────────┐
│ Shioaji API │ ────────────> │ FuturesMonitor    │
│             │               │ on_tick()         │
└─────────────┘               │   ↓               │
                              │ Build 5m bar      │
                              │   ↓               │
                              │ Calculate         │
                              │ indicators        │
                              │   ↓               │
                              │ StrategyRegistry  │
                              │   .get(active)    │
                              │   ↓               │
                              │ Strategy.on_bar() │
                              │   ↓               │
                              │ Signal? ──No──>   │
                              │   ↓ Yes           │
                              │ signal.validate() │
                              │   ↓ Pass          │
                              │ Position==0?      │
                              │ Margin OK?        │
                              │ Price>0?          │
                              │ Not same bar?     │
                              │   ↓ All Pass      │
                              │ PaperTrader       │
                              │ .execute_signal() │
                              │   ↓               │
                              │ [SSOT Updated]    │
                              │   ↓               │
                              │ save_trade()      │
                              │ save_audit()      │
                              └──────────────────┘
```

### 5.2 Backtest

```
┌─────────────┐     OHLCV     ┌──────────────────┐
│ CSV / API   │ ────────────> │ SignalGenerator   │
│             │               │ generate_signals()│
└─────────────┘               │   ↓               │
                              │ StrategyRegistry  │
                              │   .get(name)      │
                              │   ↓               │
                              │ BacktestAdapter   │
                              │ wraps state dict  │
                              │ → StrategyContext │
                              │   ↓               │
                              │ Strategy.on_bar() │
                              │   ↓               │
                              │ Signal → bool[]   │
                              │   ↓               │
                              │ Vectorized engine │
                              │ calculates PnL    │
                              └──────────────────┘
```

---

## 6. V-Model Test Plan

### Level 1: Unit Tests

| Test | What It Verifies | File |
|------|-----------------|------|
| `test_strategy_base.py` | StrategyBase ABC enforces all abstract methods | `tests/strategies/` |
| `test_strategy_context.py` | StrategyContext is frozen (cannot mutate) | `tests/strategies/` |
| `test_signal.py` | Signal.validate() rejects invalid actions, stop_loss<=0, bad confidence | `tests/strategies/` |
| `test_strategy_registry.py` | Auto-discovers plugins from filesystem, loads config, validates schema | `tests/strategies/` |
| `test_strategy_config.py` | YAML → dict, rejects unknown keys, applies defaults | `tests/strategies/` |
| `test_counter_vwap_plugin.py` | CounterVWAP plugin returns valid Signal or None with mock context | `tests/strategies/` |
| `test_spring_upthrust_plugin.py` | SpringUpthrust plugin returns valid Signal or None | `tests/strategies/` |

**Pass criteria**: All unit tests pass, 100% coverage of `core/strategy_*.py`

### Level 2: Integration Tests

| Test | What It Verifies |
|------|-----------------|
| `test_plugin_paper_trader.py` | Plugin → Signal → PaperTrader.execute_signal() → position updated correctly |
| `test_plugin_backtest.py` | Same plugin runs in backtest engine, produces identical PF to old results |
| `test_hot_swap_flat.py` | Switch strategy when position==0, new strategy receives bars immediately |
| `test_hot_swap_reject_open.py` | Attempt swap when position!=0 → rejected with error message |
| `test_malformed_signal.py` | Plugin returns bad signal → monitor rejects, audit log written |

### Level 3: System Tests

| Test | What It Verifies |
|------|-----------------|
| `test_full_paper_cycle.py` | Monitor → Registry → Strategy → PaperTrader → CSV → correct audit trail |
| `test_backtest_parity.py` | Counter-VWAP plugin backtest PF=1.95, WR=40.7%, 86 trades (regression test) |
| `test_spring_parity.py` | Spring/Upthrust plugin backtest PF=3.36, 33 trades |
| `test_restart_recovery.py` | Kill monitor during open position → restart → position recovered from API/ledger |

### Level 4: UAT Checklist

- [ ] `python3 -m pytest tests/ -v` — all pass (no regressions)
- [ ] `python3 main.py --dry-run` — starts without error, Counter-VWAP signals visible
- [ ] Dashboard shows strategy list from registry (auto-populated)
- [ ] Hot-swap: switch from Counter-VWAP to Spring when flat → new signals appear in logs
- [ ] Paper trade: 1 entry → 1 TP1 → 1 exit → PnL matches manual calculation
- [ ] Restart with open position → position recovered, no duplicate entry
- [ ] Malformed plugin (missing on_bar) → registry marks unavailable, system continues
- [ ] Config with unknown key → Pydantic rejects at load time

---

## 7. Migration Plan

### Phase 1: Foundation (Week 1)

**Deliverables:**
- `core/strategy_base.py` — StrategyBase ABC
- `core/strategy_context.py` — StrategyContext + PositionView + MarketData
- `core/strategy_registry.py` — Auto-discovery + hot-swap
- `core/strategy_config.py` — YAML loader + Pydantic validator
- `core/signal.py` — Signal dataclass + validator
- 7 unit tests

**Exit criteria**: `pytest tests/strategies/ -v` — all pass

### Phase 2: Migrate Futures Elite Strategies (Week 2)

**Deliverables:**
- `strategies/plugins/futures/counter_vwap.py` — wraps existing `strategy_counter_vwap` logic
- `strategies/plugins/futures/spring_upthrust.py` — wraps existing `strategy_spring_upthrust`
- `config/strategies/counter_vwap.yaml` — extracted from futures.yaml
- `config/strategies/spring_upthrust.yaml` — extracted from futures.yaml
- Backtest parity tests (PF=1.95 and PF=3.36)

**Exit criteria**: Backtest results identical to current values

### Phase 3: Monitor Integration (Week 3)

**Deliverables:**
- Modify `FuturesMonitor._strategy_tick()` — replace if/elif chain with `registry.get(active).on_bar(context)`
- Add signal validation layer
- Add hot-swap method: `switch_strategy(name) -> Result`

**Exit criteria**: Monitor runs with registry, paper trading works identically

### Phase 4: Backtest Adapter (Week 3-4)

**Deliverables:**
- `backtest/strategy_adapters.py` — BacktestContext → StrategyContext
- Modify `signal_generator.py` — uses registry
- Parity tests for all strategies

**Exit criteria**: All existing backtests produce identical results

### Phase 5: Options Extraction (Week 4-5)

**Deliverables:**
- `strategies/plugins/options/v2_squeeze.py`
- `strategies/plugins/options/iron_condor.py`
- Monitor retains contract management, delegates entry/exit signals

**Exit criteria**: Options paper trading works identically

### Phase 6: Dashboard + Cleanup (Week 5-6)

**Deliverables:**
- Dashboard strategy selector (auto-populated from registry)
- Hot-swap API endpoint
- Deprecate old `entry_strategies.py` and `elite_strategies.py`

**Exit criteria**: Full system operational, old code marked deprecated

---

## 8. Risk Assessment

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| Strategy mutates position directly | HIGH | MEDIUM | Code review enforcement; StrategyContext is frozen dataclass |
| Backtest results change after migration | HIGH | LOW | Parity tests block merge if PF differs >0.01 |
| Hot-swap mid-trade causes orphan position | CRITICAL | LOW | Enforce `position==0` check with double verification |
| Plugin import crashes entire process | MEDIUM | LOW | Registry catches all exceptions; plugin marked unavailable |
| Config mismatch (backtest ≠ live) | HIGH | MEDIUM | Single YAML per strategy, loaded by both adapters |
| Options monolith too entangled to extract | MEDIUM | HIGH | Phase 5 deferred; futures-only for initial release |

---

## 9. Design Decisions

### 9.1 Why ABC instead of Protocol?

ABC provides **explicit enforcement at import time** — if a plugin misses `on_bar()`, it fails immediately. Protocol would fail at runtime when called.

### 9.2 Why not mid-trade hot-swap?

Different strategies have different:
- Exit logic (VWAP vs trailing vs fixed SL)
- State variables (fire tracking, cooldown timers)
- Position sizing rules

Transferring state between strategies is **impossible to do safely**. Enforce flat-only swaps.

### 9.3 Why frozen dataclass for StrategyContext?

Python `dataclass(frozen=True)` provides **compile-time immutability** — any mutation attempt raises `FrozenInstanceError`. This enforces SDD Rule 1 at runtime.

### 9.4 Why Signal dataclass instead of dict?

Dicts are silent on missing keys: `signal.get("stop_loss")` returns None → downstream crash. Dataclass provides **schema at type level** — missing fields caught at construction.

---

## 10. Appendix: Existing Pattern References

| Pattern | File | Description |
|---------|------|-------------|
| SSOT: PaperTrader.position | `strategies/futures/squeeze_futures/engine/simulator.py` | Position is sole authority |
| Side effects after validation | `strategies/futures/monitor.py:_execute_trade` | CSV/audit only after success |
| Signal dict convention | `strategies/futures/entry_strategies.py` | `{"action", "reason", "stop_loss"}` |
| State builder | `backtest/signal_generator.py:build_state_optimized()` | Builds state dict for strategies |
| Indicator separation | `strategies/futures/squeeze_futures/engine/indicators.py` | Pure function, no state |
| Config validation | `core/strategy_schema.py` | Pydantic with `extra="forbid"` |
| Registry merge | `backtest/signal_generator.py` | Elite + remaining + stock |
