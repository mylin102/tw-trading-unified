# How to Create a Pluggable Strategy

**Audience**: Strategy developers (no framework knowledge needed)  
**Time**: 15 minutes to first working plugin

---

## Overview

A strategy plugin is **one `.py` file + one `.yaml` file**. Drop them in the right directories and the system auto-discovers and runs them.

```
strategies/plugins/futures/my_new_strategy.py   ← your code
config/strategies/my_new_strategy.yaml           ← your config
```

That's it. No code changes to monitors, no registry edits, no imports.

---

## Step 1: Create the Plugin File

### Location

- Futures: `strategies/plugins/futures/<name>.py`
- Options: `strategies/plugins/options/<name>.py`

The filename becomes your strategy's unique name.

### Template

Copy this template and fill in your logic:

```python
"""<name> — <one-line description>."""
from __future__ import annotations

from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext
from core.signal import Signal


class MyStrategy(StrategyBase):
    """Your strategy class.  Must subclass StrategyBase."""

    # ── Required ─────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        """Must match the filename (without .py)."""
        return "<name>"

    @property
    def metadata(self) -> dict:
        return {
            "asset_class": "futures",       # "futures" or "options"
            "version": "1.0",
            "backtest_pf": 0.0,
            "backtest_wr": 0.0,
            "backtest_maxdd": 0.0,
            "market_regime": "all",         # "ranging" | "trending" | "all"
            "description": "<describe strategy in one sentence>",
        }

    def init(self, context: StrategyContext) -> None:
        """Called ONCE when strategy is activated.
        Initialize internal state here."""
        # Example:
        # self._fire_high = 0.0
        # self._bar_counter = 0
        pass

    def on_bar(self, context: StrategyContext) -> Signal | None:
        """Called on EVERY new bar.  Return Signal or None."""

        # ── Read market data (read-only) ─────────────────────────────
        close = context.market.last_bar.get("Close", 0.0)
        vwap  = context.market.last_bar.get("vwap", close)
        atr   = context.market.last_bar.get("atr", 200.0)
        score = context.market.last_bar.get("score", 0.0)

        # ── Read position (read-only) ────────────────────────────────
        pos_size     = context.position.size            # 0, +1, -1
        entry_price  = context.position.entry_price
        current_sl   = context.position.current_stop_loss

        # ── Read config ──────────────────────────────────────────────
        confirm_bars = context.config.get("params", {}).get("confirm_bars", 5)
        atr_mult     = context.config.get("params", {}).get("atr_sl_mult", 2.0)

        # ── Your signal logic ────────────────────────────────────────
        if self._should_buy(close, vwap, atr, score):
            sl_price = close - atr * atr_mult
            return Signal(
                action="BUY",
                reason="MY_STRATEGY",
                stop_loss=sl_price,
                target=close + atr * atr_mult * 2,
                confidence=0.8,
            )

        if self._should_sell(close, vwap, atr, score):
            sl_price = close + atr * atr_mult
            return Signal(
                action="SELL",
                reason="MY_STRATEGY",
                stop_loss=sl_price,
                target=close - atr * atr_mult * 2,
            )

        return None  # No signal this bar

    # ── Optional hooks ──────────────────────────────────────────────────
    # def on_tick(self, tick: dict) -> None: ...
    # def cleanup(self) -> None: ...

    # ── Private helpers ─────────────────────────────────────────────────
    def _should_buy(self, close, vwap, atr, score) -> bool:
        return False  # replace with real logic

    def _should_sell(self, close, vwap, atr, score) -> bool:
        return False  # replace with real logic
```

---

## Step 2: Create the Config File

### Location

`config/strategies/<name>.yaml`

### Template

```yaml
name: <name>
asset_class: futures
version: "1.0"
enabled: true

# ── Strategy-specific parameters ─────────────────────────────────────
params:
  confirm_bars: 5
  atr_sl_mult: 2.0
  exit_on_vwap: true

# ── Risk limits (enforced by monitor, NOT the strategy) ──────────────
risk:
  max_positions: 1
  stop_loss_type: atr       # "atr" | "fixed" | "percent"
  stop_loss_mult: 2.0

# ── Market regime filter ─────────────────────────────────────────────
regime_filter:
  allowed: ["ranging", "squeeze"]
  min_adx: 0

# ── Backtest metadata (for dashboard catalog) ────────────────────────
backtest:
  pf: 0.0
  wr: 0.0
  max_dd: 0.0
  total_trades: 0
  period: ""
```

**Validation rules** (enforced at load time):
- `risk.max_positions >= 0`
- `risk.stop_loss_mult > 0`
- `backtest.pf >= 0`
- `backtest.wr` in 0–100
- `backtest.max_dd <= 0`

---

## Step 3: Understand the Signal Contract

Every strategy outputs a **Signal** — a typed object, not a dict.

### Required Fields

| Field | Type | Meaning |
|-------|------|---------|
| `action` | str | `"BUY"` · `"SELL"` · `"EXIT"` · `"PARTIAL_EXIT"` |
| `reason` | str | Why the signal fired, e.g. `"COUNTER_VWAP"` |
| `stop_loss` | float | Absolute price level (e.g. `34900.0`, NOT `200` points) |

### Optional Fields

| Field | Default | Meaning |
|-------|---------|---------|
| `target` | `0.0` | Take-profit target price |
| `confidence` | `1.0` | 0.0–1.0 for strategy weighting |

### Validation (automatic)

```python
Signal("BUY", "TEST", 34900.0).validate()   # → (True, "")
Signal("HOLD", "X", 34900.0).validate()     # → (False, "Invalid action")
Signal("BUY", "", 34900.0).validate()        # → (False, "Missing reason")
Signal("BUY", "TEST", 0.0).validate()        # → (False, "Invalid stop_loss")
```

Invalid signals are **rejected before execution** — no state change, no file write, audit log records the rejection.

---

## Step 4: Understand StrategyContext

`StrategyContext` is passed to `on_bar()` every bar. It is **frozen** — attempts to mutate raise `FrozenInstanceError`.

### Available Data

```python
# Market data (read-only)
context.market.last_bar["Close"]        # Current close
context.market.last_bar["vwap"]         # VWAP
context.market.last_bar["atr"]          # ATR
context.market.last_bar["score"]        # MTF alignment score
context.market.df_5m                    # Recent 5m DataFrame
context.market.df_15m                   # Recent 15m DataFrame
context.market.timestamp                # e.g. "2026-04-09 15:00:00"
context.market.session                  # 1=day, 2=night

# Position (read-only — SSoT is PaperTrader)
context.position.size                   # 0, +N, -N
context.position.entry_price
context.position.current_stop_loss
context.position.unrealized_pnl
context.position.has_tp1_hit

# This strategy's config
context.config["params"]["confirm_bars"]
context.config["risk"]["max_positions"]

# Bar counter
context.bar_counter                     # Monotonic, increments each bar
```

### What You CANNOT Do

```python
# ❌ Never mutate position — monitor/PaperTrader owns it
context.position.size = 1               # FrozenInstanceError

# ❌ Never mutate market data
context.market.last_bar["Close"] = 0    # FrozenInstanceError

# ❌ Never mutate context itself
context = new_context                   # FrozenInstanceError
```

---

## Step 5: Real Example — Counter-VWAP (Simplified)

```python
"""counter_vwap — Buy squeeze fire + VWAP bounce in ranging market."""
from __future__ import annotations

from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext
from core.signal import Signal


class CounterVWAP(StrategyBase):
    @property
    def name(self) -> str:
        return "counter_vwap"

    @property
    def metadata(self) -> dict:
        return {
            "asset_class": "futures",
            "version": "2.0",
            "backtest_pf": 1.95,
            "backtest_wr": 40.7,
            "backtest_maxdd": -7.2,
            "market_regime": "ranging",
            "description": "Buy after squeeze fire when price bounces off VWAP",
        }

    def init(self, context: StrategyContext) -> None:
        self._fire_high = 0.0
        self._fire_low = 0.0
        self._fire_pending_dir = 0  # 0=none, +1=long, -1=short

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        close = bar.get("Close", 0.0)
        high  = bar.get("High", 0.0)
        low   = bar.get("Low", 0.0)
        vwap  = bar.get("vwap", close)
        atr   = bar.get("atr", 200.0)
        fired = bar.get("fired", False)       # Squeeze just fired
        bull  = bar.get("bullish_align", False)
        bear  = bar.get("bearish_align", False)

        params = context.config.get("params", {})
        confirm = params.get("confirm_bars", 5)
        atr_mult = params.get("atr_sl_mult", 2.0)

        # Detect squeeze fire
        if fired and bull:
            self._fire_pending_dir = +1
            self._fire_high = high
            self._fire_low = low
        elif fired and bear:
            self._fire_pending_dir = -1
            self._fire_high = high
            self._fire_low = low

        if self._fire_pending_dir == 0:
            return None

        # Wait for confirmation bars
        self._fire_high = max(self._fire_high, high)
        self._fire_low = min(self._fire_low, low)

        # BUY: price bounces above VWAP after bullish fire
        if self._fire_pending_dir == +1 and close > vwap > self._fire_low:
            sl = close - atr * atr_mult
            return Signal("BUY", "COUNTER_VWAP", sl, target=vwap + atr)

        # SELL: price drops below VWAP after bearish fire
        if self._fire_pending_dir == -1 and close < vwap < self._fire_high:
            sl = close + atr * atr_mult
            return Signal("SELL", "COUNTER_VWAP", sl, target=vwap - atr)

        return None

    def cleanup(self) -> None:
        self._fire_pending_dir = 0
        self._fire_high = 0.0
        self._fire_low = 0.0
```

---

## Step 6: Test Your Plugin

### Quick Unit Test

Create `tests/strategies/test_<name>.py`:

```python
import pytest
from strategies.plugins.futures.<name> import MyStrategy
from core.strategy_context import StrategyContext, PositionView, MarketData


def _make_ctx(close=35000.0, **overrides) -> StrategyContext:
    bar = {"Close": close, "High": close + 50, "Low": close - 50, "Volume": 1000}
    bar.update(overrides)
    return StrategyContext(
        market=MarketData(last_bar=bar),
        position=PositionView(),
        config={"params": {}},
    )


class TestMyStrategy:
    def setup_method(self):
        self.strategy = MyStrategy()
        self.strategy.init(_make_ctx())

    def test_returns_none_on_flat_market(self):
        assert self.strategy.on_bar(_make_ctx()) is None

    def test_returns_valid_signal_when_conditions_met(self):
        # Set up conditions that trigger entry
        ctx = _make_ctx(fired=True, bullish_align=True, vwap=34950.0)
        sig = self.strategy.on_bar(ctx)
        if sig is not None:
            ok, msg = sig.validate()
            assert ok is True, msg
            assert sig.stop_loss > 10000  # absolute price
```

### Run Tests

```bash
python3 -m pytest tests/strategies/test_<name>.py -v
```

---

## Step 7: Activate the Strategy

Edit `config/futures.yaml`:

```yaml
strategy:
  active_strategy: <name>   # ← change this to your strategy name
```

Restart the monitor — your strategy is now live.

---

## Checklist

- [ ] Plugin file in `strategies/plugins/{futures,options}/<name>.py`
- [ ] Class subclasses `StrategyBase`
- [ ] `name` property matches filename
- [ ] `init()` and `on_bar()` implemented
- [ ] Returns `Signal` objects (not dicts) or `None`
- [ ] Config file in `config/strategies/<name>.yaml`
- [ ] Unit test in `tests/strategies/test_<name>.py`
- [ ] `pytest tests/strategies/test_<name>.py -v` passes

---

## Common Mistakes

| Mistake | Symptom | Fix |
|---------|---------|-----|
| `name` ≠ filename | Registry registers wrong name | Make `@property def name` match the file |
| Returning dict instead of Signal | Monitor rejects, audit log shows rejection | Return `Signal(...)` |
| `stop_loss` is points (200) instead of price (34900) | SL placed at wrong level | `stop_loss = close - atr * mult` |
| Mutating `context.position` | `FrozenInstanceError` crash | Read only, never write |
| Missing `init()` | `TypeError` at import (ABC) | Implement all abstract methods |
| Config missing `risk:` block | Validation error at load | Include required sections or use defaults |

---

## Migration from Old Style

If you have a legacy function in `entry_strategies.py`:

```python
# OLD
def strategy_my_thing(state, cfg):
    if state["last_5m"]["fired"]:
        return {"action": "BUY", "reason": "THING", "stop_loss": 34900}
    return None
```

Migration steps:

1. Create `strategies/plugins/futures/my_thing.py`
2. Wrap the function:

```python
class MyThing(StrategyBase):
    @property
    def name(self): return "my_thing"

    def init(self, ctx): pass

    def on_bar(self, ctx):
        # Map old state dict → new context
        bar = ctx.market.last_bar
        if bar.get("fired"):
            return Signal("BUY", "THING", 34900.0)
        return None
```

3. Create `config/strategies/my_thing.yaml` (copy from old YAML keys)
4. Update `active_strategy: my_thing`
5. Test → old and new produce identical signals

---

## Need Help?

- **SDD Design Doc**: `docs/SDD_PLUGGABLE_STRATEGY_MODULE.md` — full architecture
- **V-Model Test Plan**: `docs/V_MODEL_PLUGGABLE_STRATEGIES.md` — test requirements
- **Existing plugins**: `strategies/plugins/futures/counter_vwap.py` — reference implementation
