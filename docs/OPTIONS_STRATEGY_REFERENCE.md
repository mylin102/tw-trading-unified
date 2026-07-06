# Options Trading Strategy Reference

## tw-trading-unified — TXO Options Strategy Document

Last updated: 2026-04-28

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Entry Conditions](#2-entry-conditions)
3. [Exit Conditions](#3-exit-conditions)
4. [Guard / Protection Layers](#4-guard--protection-layers)
5. [Config Reference](#5-config-reference)
6. [Known Issues and Limits](#6-known-issues-and-limits)

---

## 1. Architecture Overview

The system implements a **dual strategy architecture** running inside a single `OptionsMonitor` loop (`live_options_squeeze_monitor.py`). Both strategies share the same signal pipeline (squeeze detection + score) but make mutually exclusive trades.

### 1.1 Dual Strategy Model

```
                    ┌──────────────────────────────────────┐
                    │       Signal Pipeline (TTF Squeeze)   │
                    │  score, mid_trend, squeeze_on, side   │
                    └──────┬───────────────────────┬───────┘
                           │                       │
                 ┌─────────▼──────────┐    ┌──────▼──────────┐
                 │   Directional      │    │   ThetaGang      │
                 │   (V1/V2/V3 mode)  │    │   (Iron Condor   │
                 │   Buy long calls   │    │    / Credit       │
                 │   or long puts     │    │    Spreads)       │
                 │   Swing / hold     │    │   Sell premium   │
                 └────────────────────┘    └─────────────────┘
```

**Decision priority**: Directional signals always take precedence over ThetaGang. If `signal.side` is non-null (directional entry), ThetaGang entry is skipped for that bar.

### 1.2 Strategy A — Directional (V1 / V2 / V3 modes)

Defined in `config/options_strategy.yaml` under `modes`. Three profiles:

| Mode    | Holding    | Delivery    | Force Close | TP1  | Use Case                |
|---------|------------|-------------|-------------|------|-------------------------|
| V1      | daytrade   | near        | true        | 1%   | Intraday scalping       |
| V2      | swing      | monthly     | false       | 3%   | Multi-day trend swing   |
| V3      | night      | near        | true        | 1%   | Night session only      |

- **Entry**: Buy a single long call (C) or long put (P) option.
- **Exit**: Stop-loss, take-profit (TP1), score reversal, trailing stop, time constraints, EOD force close.

### 1.3 Strategy B — ThetaGang (Premium Selling)

Defined in `config/options_strategy.yaml` under `theta_gang`. Uses `ThetaGangManager` from `theta_gang.py`.

- **Strategy**: Default is `iron_condor` (4 legs: sell put spread + sell call spread).
- **Also supported**: `bull_put_spread`, `bear_call_spread`, `short_strangle`.
- **Entry condition**: Squeeze ON (low vol compression) → sell premium.
- **Exit**: Take-profit (% of credit), max-loss, DTE floor, squeeze-release detection.
- **Live trading**: Only 2-leg vertical spreads (`bull_put_spread`, `bear_call_spread`) are supported for live broker execution. Iron condors and short strangles run in paper-only mode.

### 1.4 Shared State (Position Tracking)

Both strategies update the same `OptionsMonitor` state attributes:
- `self.position` — integer position size (lots).
- `self.active_side` — `"C"`, `"P"`, or `"THETA"` (for ThetaGang).
- `self.entry_price` — premium at entry. **Warning**: ThetaGang and Directional shares this field, restore logic must be careful.
- `self.entry_time` — timestamp of entry.
- `self.has_tp1_hit` — whether partial profit has been taken.

---

## 2. Entry Conditions

### 2.1 Directional Entry

#### 2.1.1 Signal Resolution (`resolve_entry_side` in `options_strategy.py`)

```python
def resolve_entry_side(row, score, price_mtx, score_thresh, mid_trend=None, require_mid_trend=False):
```

**Logic**:

1. **Squeeze gating**: If `row["sqz_on"]` is True → return `None` (no entry). The system waits for **squeeze release** (firing) before entering directional.
2. **Score threshold**: `|score| >= entry_score` (default 30).
3. **VWAP confirmation**: 
   - C (bullish): `score >= threshold` AND `price_mtx >= vwap`
   - P (bearish): `score <= -threshold` AND `price_mtx <= vwap`
4. **Mid-trend alignment** (optional, controlled by `require_mid_trend`):
   - C requires `mid_trend == "BULL"`
   - P requires `mid_trend == "BEAR"`

#### 2.1.2 Complete Entry Flow (`run_strategy_logic` in monitor)

```
Signal arrives → resolve_entry_side() → side resolved?
  ├─ NO → try ThetaGang (if enabled)
  └─ YES → Gate checks:
       ├─ Trading Readiness Gate (is_trading_ready)
       ├─ Edge Model Gate (edge_model.evaluate)
       ├─ Directional Release Confirmation (squeeze release confirmed 2+ bars)
       ├─ Cooldown check (cooldown_until > 0? skip; strong signal can break cooldown)
       ├─ Open position slot (position < max_positions)
       └─ enter_paper_position() / enter_live_position()
```

#### 2.1.3 Paper Entry Gate (`enter_paper_position`, line 2267)

Sequential checks:
1. Signal side matches requested side.
2. `entry_lots > 0`.
3. `position < max_positions`.
4. `max_daily_entries` not exceeded (default 3).
5. DTE allows entry (`_dte_allows_entry`).
6. Spread is tradeable (`spread_is_tradeable` → spread/mid < max_spread_pct).
7. **[DirectionLock]** Regime-direction compatibility (Section 4).
8. Entry premium ≤ `entry_premium_limit` (default 1500 pts).
9. Margin check (paper).
10. Record paper order → update state.

### 2.2 ThetaGang Entry

#### 2.2.1 `should_enter_theta` (theta_gang.py:153)

```python
def should_enter_theta(squeeze_on, iv, iv_rank_pct=None, min_iv=0.18, min_dte=5):
```

Conditions:
1. **Squeeze ON** — low vol compression means IV is elevated relative to realized vol.
2. **IV >= min_iv** — enough premium to collect (default 0.12).
3. **Optional IV rank** > 30th percentile.

#### 2.2.2 `ThetaGangManager.evaluate_entry` (theta_gang.py:230)

1. No existing open position.
2. `should_enter_theta` passes.
3. Optional directional score floor filter (`has_directional_entry_bias`).
4. DTE `>= min_dte_entry` (default 7 days).
5. Strike selection (`select_strikes`):
   - OTM offset: 200 pts from spot.
   - Wing width: 200 pts (for spreads).
6. Minimum credit filter (`min_credit`: default 30 pts).
7. `net_credit > 0` (GSD fix).

#### 2.2.3 Strike Selection Diagram

```
Iron Condor (spot=22000, otm_offset=200, wing_width=200):
  PUT side:     SELL P 21800   BUY P 21600    (credit spread)
  CALL side:    SELL C 22200   BUY C 22400    (credit spread)
  ───────────────────────────────────────────────────
  Net credit collected = put_credit + call_credit
  Max loss = wider wing - net_credit
```

---

## 3. Exit Conditions

### 3.1 Directional Exit (managed in `manage_open_position`, line 3337)

The exit decision runs every bar when a position is open. The sequence is:

```
manage_open_position():
  1. [QuoteGuard] Validate quote quality → fail = skip
  2. [SessionGuard] Market open check → fail = skip
  3. Time constraints (max_holding_days, min_dte_to_exit)
  4. Trailing stop (peak_premium × (1 - trailing_stop_pct))
  5. Score reversal exit (threshold = entry_score × 1.5)
  6. TP1 partial profit
  7. Stop-loss / hard-stop / score-decay
```

#### 3.1.1 Time Constraints (`should_exit_by_time_constraints`)

- `max_holding_days` (default 7): exit if position held longer than N days.
- `min_dte_to_exit` (default 7.0 DTEs): exit if days-to-expiry falls below threshold.

#### 3.1.2 Trailing Stop

- Activates **after TP1 hit** OR when **unrealized PnL >= 8%**.
- Trigger: `exit_price <= peak_premium × (1 - trailing_stop_pct)`.
- V2 default `trailing_stop_pct = 0.02` (2% pullback from peak triggers exit).

#### 3.1.3 Score Reversal Exit

- Threshold = `entry_score × 1.5` (e.g., 15 → 22.5).
- Long PUT (bearish): score reverses from negative to **>= +threshold** → exit.
- Long CALL (bullish): score reverses from positive to **<= -threshold** → exit.
- **Gated by**: Opening grace period (first N mins of session) and directional release confirmation.

#### 3.1.4 TP1 Partial Profit (`should_take_partial_profit`)

- Take 1 lot profit when premium appreciation >= `tp1_pct`.
- V1: 1.0%, V2: 3.0%, V3: 1.0%.
- Only triggers if `has_tp1_hit == False` and `position >= 1`.

#### 3.1.5 Stop-Loss / Hard-Stop / Score Decay

From `should_exit_position()` and `classify_exit_reason()` (`backtest_engine.py`):

| Exit Reason | Condition | Config Key | Default |
|-------------|-----------|------------|---------|
| `hard_stop` | `current_premium <= entry_premium × (1 - hard_stop_pct)` | `hard_stop_pct` | 0.15 (15%) |
| `stop_loss` | `current_premium <= entry_premium × (1 - stop_loss_pct)` (or entry_premium if TP1 hit) | `stop_loss_pct` | 0.25 (25%) |
| `score_decay` | `|score| < score_floor` | `score_floor` | 10 |
| `force_close` | Session time exceeds EOD panic time + mode `force_close_at_end` | per mode | V1/V3: true |

**Score decay exit is gated** by directional release confirmation (requires 2+ bars of confirmed release).

#### 3.1.6 EOD Optimization (V1/V3 modes with `force_close_at_end: true`)

- **Passive phase** (20 min before panic time): hang sell order `eod_passive_ticks` above the ask.
- **Panic phase** (at `eod_panic_time`, default 13:30): market exit at bid.

### 3.2 ThetaGang Exit

From `should_exit_theta()` and `ThetaGangManager.evaluate_exit()`:

| Condition | Trigger | Config Key | Default |
|-----------|---------|------------|---------|
| Take profit | `profit_pct >= take_profit_pct` | `take_profit_pct` | 0.20 (20%) |
| Max loss | `loss_pct >= max_loss_pct` | `max_loss_pct` | 1.0 (100% of max loss) |
| DTE floor | `dte_days <= min_dte_exit` | `min_dte_exit` | 3 days |
| Squeeze release | Squeeze OFF × confirmed 2+ bars | `exit_on_squeeze_release` | true |

**Squeeze release gating**: Requires `squeeze_release_confirm_bars` (default 2) consecutive bars with:
- Squeeze OFF in signal.
- Bar quality PASS.
- No futures squeeze conflict.

**Minimum holding bars**: Theta positions cannot exit (except stop-loss) before `min_holding_bars` (default 10 bars).

**PnL computation at exit**: 
```
gross_pnl = net_credit - current_value (in points)
net_pnl = gross_pnl × 50 - broker_fee - exchange_fee - tax  (NZD)
```

---

## 4. Guard / Protection Layers

### 4.1 DirectionLock (in `enter_paper_position`, line 2292)

Regime-direction incompatibility guard. Blocks directional entries when:

| Regime     | Score Bias     | Blocked Side | Rationale                 |
|------------|----------------|--------------|---------------------------|
| BEAR/STRETCHED | Positive (bearish) | CALL (long) | Buying calls into bearish regime |
| BULL/STRETCHED  | Negative (bullish)| PUT (long)  | Buying puts into bullish regime  |
| STRETCHED   | Any            | C or P      | Stretched = theta only, no directional |

Logic:
```python
_regime = self.latest_mid_trend        # "BULL" / "BEAR" / "STRETCHED"
_score = self.latest_score              # positive = bearish, negative = bullish
_side = "C" or "P"                      # requested entry side
```

### 4.2 SessionGuard (in `manage_open_position`, line 3356)

Skips exit decisions when the market is closed, preventing stale quote exits.

```python
_market_open, _session = self._is_market_open(now)
if not _market_open:
    return False  # skip exit, mark pending
```

Uses `core.date_utils.is_day_session()` and `is_night_session()`.

### 4.3 QuoteGuard (in `manage_open_position`, line 3346)

Validates quote quality before exit logic runs:

```python
_quote_valid = bid > 0 and ask > bid and mid > 0 and (ask - bid) / mid < 0.3
```

Rejects exit if:
- Bid is 0.
- Spread ratio > 30% of mid.
- Mid is 0 or negative.

### 4.4 Directional Release Confirmation (in `_update_theta_release_confirmation`, line 1682)

Gates both directional entries and score-decay exits. Requires squeeze release to be confirmed across multiple bars:

```python
conditions for "release_bar_confirmed":
  - squeeze_on == False (raw release)
  - bar_quality == "PASS"  (no excessive price deviation, sufficient volume)
  - futures_sqz_on is not True  (futures squeeze not conflicting)

confirmed = release_bar_confirmed AND confirm_count >= confirm_bars (default 2)
```

### 4.5 Edge Model Guard (in `run_strategy_logic`, line 3502)

Before any directional entry, `core.edge_model.evaluate()` is called to assess whether the signal has statistical edge. If not, the signal is nullified.

### 4.6 Opening Grace Period (in `manage_open_position`, line 3409)

Score reversal exits are suppressed during the first N minutes of any trading session (default 5 min, configurable via `risk_mgmt.opening_grace_mins`). Prevents whipsaw exits from data spikes or gap opens.

### 4.7 Premium Cap Protection (in `enter_paper_position`, line 2322)

Entry is blocked if the ask premium exceeds `entry_premium_limit` (default 1500 pts).

### 4.8 Daily Entry Limit (in `enter_paper_position`, line 2280)

Prevents rapid re-entry on the same side: max `risk_mgmt.max_daily_entries` (default 3) per day.

### 4.9 Bar Quality Filter (in `_evaluate_signal_bar_quality`)

All directional and ThetaGang entries/exits are filtered by bar quality:
- Price deviation from reference < `max_bar_price_deviation_pts` (default 250).
- Sufficient volume/intra-bar range.

---

## 5. Config Reference

### 5.1 `config/options_strategy.yaml` — Top-Level Structure

```yaml
active_mode: V2              # Active mode profile: V1 | V2 | V3
entry_score: 30              # Minimum |score| threshold for directional entry
live_trading: false          # Global live/paper toggle
mode: V2                     # (legacy alias for active_mode)

# ── Execution ──
execution:
  aggressive_ticks: 1        # Tick offset for aggressive orders
  broker_fee_per_side: 20.0  # Broker commission per leg (TWD)
  exchange_fee_per_side: 5.0 # Exchange fee per leg (TWD)
  max_order_retries: 1       # Max order submission retries
  max_spread_pct: 0.1        # Max bid-ask spread ratio (10%)
  order_timeout_secs: 15     # Live order timeout
  order_type: Limit          # Order type: Limit | Market

# ── Exit Optimization ──
exit_optimization:
  eod_panic_time: '13:30'    # Force-close time for day session
  eod_passive_ticks: 1       # Tick offset above ask for passive EOD exit
  eod_passive_window_mins: 20 # Min before panic to start passive phase
  opening_spike_capture: true # Try to capture opening spike
  opening_target_pct: 0.25   # Target % for opening spike
  shutdown_grace_mins: 1     # Grace period after panic time

# ── Exit Strategy ──
exit_strategy:
  lots_per_trade: 2          # Lots per exit order
  tp1_pct: 3.0               # TP1 threshold (3%)

# ── Monitoring ──
monitoring:
  use_order_manager: true    # Enable Order Lifecycle Manager

# ── IV Filters ──
max_iv: 0.6                  # Max IV for entry filter
min_iv: 0.15                 # Min IV for entry filter

# ── Mode Profiles ──
modes:
  V1:
    delivery_pref: near       # Near-month delivery
    force_close_at_end: true  # Force close at end of session
    holding_mode: daytrade    # Day-trade holding
    tp1_pct: 1.0              # 1% TP1
  V2:
    bear_boost_pct: 0.6      # Boost bear IV by 60%
    delivery_pref: monthly    # Monthly delivery
    force_close_at_end: false # No force close (hold overnight)
    holding_mode: swing       # Swing holding
    tp1_pct: 3.0              # 3% TP1
    trailing_stop_pct: 2.0   # 2% trailing stop from peak
  V3:
    bear_boost_pct: 0.6
    delivery_pref: near
    force_close_at_end: true
    holding_mode: night
    tp1_pct: 1.0

# ── Night Trading ──
night_trading:
  enabled: true
  force_close: 04:45         # Night session force-close time
  session_end: 05:00
  session_start: '15:00'
  us_open_summer: '21:30'
  us_open_winter: '20:30'

# ── Pricing ──
pricing:
  bear_put_iv_mult: 1.05     # IV multiplier for bearish put pricing
  bull_call_iv_mult: 0.95    # IV multiplier for bullish call pricing
  default_iv: 0.25
  entry_premium_mode: model   # 'model' = BS pricing, 'profile' = fixed
  expiry_dte_floor_days: 7.0 # Minimum DTE for pricing floor
  max_iv: 0.4
  min_iv: 0.12
  near_dte_days: 3.0         # DTE for near-month contracts
  neutral_iv_mult: 1.0
  pricing_model: quantlib     # quantlib | black_scholes | linear
  risk_free_rate: 0.02
  strike_rounding: 100        # TXO strike interval

# ── Risk Management ──
risk_mgmt:
  entry_premium_limit: 1500  # Max premium allowed for entry (pts)
  initial_capital: 100000
  lots_per_trade: 1
  max_daily_loss: 0.03       # 3% max daily loss
  max_holding_days: 7        # Max days to hold a directional position
  max_position_size: 0.08    # Max 8% of capital per position
  max_positions: 3           # Max concurrent positions
  min_dte_to_exit: 7.0       # Exit when DTE < 7 days
  opening_grace_mins: 5      # Grace period at session start
  stop_loss_pct: 0.25        # 25% premium drawdown stop
  hard_stop_pct: 0.15        # 15% hard stop (unconditional)

# ── Strategy ──
strategy:
  cooldown_bars: 2           # Cooldown after exit (bars)
  entry_score: 15            # Per-strategy entry score threshold
  fallback_underlying_price: 33500
  fire_score_threshold: 40   # Min |score| to accept unfired squeeze
  length: 20                 # Indicator lookback
  monthly_delivery_min_days: 14 # Min days for monthly delivery selection
  regime_filter: mid         # Mid-trend regime filter
  require_align: false       # Require MTF alignment for entry
  require_fire: false        # Require squeeze fire for entry
  score_floor: 10            # Score floor (below = exit)
  tsm_strategy:
    enabled: true
    nvda_threshold: 0.0
    sox_threshold: 0.0
    tsm_threshold: 0.0
  use_opening_logic: true
  weights:                   # MTF score weights
    15m: 0.4
    1h: 0.4
    5m: 0.2

# ── ThetaGang ──
theta_gang:
  enabled: true
  auto_regime: true          # Auto-detect ranging/squeeze regime
  exit_on_squeeze_release: true # Exit when squeeze releases
  strategy: iron_condor      # Default strategy
  otm_offset: 200            # Short strike OTM offset (pts)
  wing_width: 200            # Wing width for spreads (pts)
  quantity: 1                # Lots
  min_credit: 30             # Minimum credit to accept (pts)
  min_dte_entry: 7           # Min DTE for entry
  min_dte_exit: 3            # Exit when DTE reaches this
  min_iv: 0.12               # Min IV for theta entry
  take_profit_pct: 0.20      # 20% of max credit = TP
  max_loss_pct: 1.0          # 100% of max loss = stop
  risk_free_rate: 0.02
  cooldown_bars: 20          # Cooldown after theta exit
  min_holding_bars: 10       # Min bars to hold before exiting (except SL)
  squeeze_release_confirm_bars: 2 # Confirmation bars for release
  max_bar_price_deviation_pts: 250 # Max bar deviation from reference
  exit_on_theta: true
  stop_loss_pct: 0.05        # 5% premium stop loss
```

### 5.2 `config/strategies/v2_squeeze.yaml` — Signal Plugin Config

```yaml
strategy:
  entry_score: 60            # Minimum |score| for the plugin
  score_floor: 20            # Floor for exits
  require_fire: true         # Require squeeze fire
  fire_score_threshold: 60   # Min |score| for unfired squeeze
  require_align: true        # Require MTF alignment
  require_mid_trend: true    # Require mid-trend alignment
  weights:
    1h: 0.4
    15m: 0.4
    5m: 0.2
  atr_sl_mult: 2.0           # ATR multiplier for stop-loss distance
  trailing_stop_pct: 0.15    # 15% trailing stop (after 8% gain)
  tp1_pct: 0.5               # TP1 at 50% gain
  hold_mode: swing
  delivery_pref: monthly
  cooldown_bars: 3
```

---

## 6. Known Issues and Limits

### 6.1 Shared `entry_price` Between Strategies (Critical)

**Problem**: `ThetaGang` and `Directional` both write to `self.entry_price`. If a ThetaGang position is open and the system restarts, `restore_position()` recovers the ThetaGang position but writes its `net_credit` value to `self.entry_price`. When the directional exit logic runs (e.g., `manage_open_position`), it uses this ThetaGang entry_price for stop-loss/trailing calculations on directional positions.

**Impact**: Incorrect stop-loss and PnL calculations on mixed strategy restarts. Mitigated by the fact that only one strategy is active at a time (mutual exclusion through `active_side`), but `log_trade()` and `exit_paper_position()` use `entry_price_override` snapshots to avoid this issue.

### 6.2 No Score Smoothing

**Problem**: The raw score from the TTF squeeze signal is used directly without any EMA or smoothing filter. This makes the system sensitive to bar-to-bar noise, especially in the 5-minute timeframe.

**Impact**: Reversal exits and score-decay exits can trigger on single-bar spikes. Partially mitigated by directional release confirmation (2-bar requirement), opening grace period, and the `1.5×` reversal threshold buffer.

### 6.3 No Time-Based Stop-Loss for Directional Trades

**Problem**: The "stop-loss" for directional trades is premium-based (% of entry premium). There is no mechanism to exit based on holding time or theta decay for directional positions (only available for ThetaGang via `min_dte_exit`).

**Impact**: If the underlying doesn't move, directional positions slowly lose value through theta decay with no automatic time exit. The only protection is `max_holding_days` (default 7), but this is a calendar-day check, not theta-based.

### 6.4 ThetaGang Live Execution Limited to 2-Leg Spreads

**Problem**: `iron_condor` (4 legs) and `short_strangle` (2 legs, both naked) are not supported for live broker execution. Validation in `validate_live_combo()` requires exactly 2 legs with `{SELL, BUY}` actions on a single option side (vertical spread).

**Impact**: Iron condors and short strangles can only be run in paper-trading mode. This is a constraint of the broker API (Shioaji) which only supports combo orders for simple vertical spreads.

### 6.5 PnL Calculation: Entry Price Mapping for Short Strategies

**Problem**: The `log_trade()` PnL side mapping has explicit whitelist logic:
```python
is_long = side in ("C", "P", "CALL", "PUT", "BUY", "LONG")
is_short = side in ("SELL", "THETA", "SHORT", "IRON_CONDOR")
```
If a new side string is introduced (e.g., "BULL_PUT_SPREAD"), PnL will be 0 with a warning.

**Impact**: Any new strategy type must update this mapping or PnL will not be computed correctly.

### 6.6 Balance Derived from Last Row, Not Accumulation

**Problem**: `log_trade()` computes balance by reading the last `Balance` value from the ledger CSV and adding the current PnL. If the ledger CSV has gaps or manual edits, the balance chain breaks.

**Impact**: Balance can desync. This is a deliberate design choice (previously attempted summation of PnL column had its own issues).

### 6.7 ThetaGang Stop Loss Uses Different Convention

**Problem**: ThetaGang `stop_loss_pct: 0.05` operates on the **short premium value**, not on the entry premium. In `evaluate_exit`, the stop loss is checked via `max_loss_pct` (100% of max loss), which is a very wide stop. The `stop_loss_pct` in `theta_gang` config is actually used for the directional-style stop if applicable, but the primary theta exit logic uses `max_loss_pct`.

**Impact**: ThetaGang positions can lose up to 100% of the spread width before forced exit (minus collected credit). This is intentional for spread strategies but worth noting.

### 6.8 Restore Logic Not Fully Tested for Concurrent Strategies

**Problem**: `_startup_recover_live_order_state()` and `restore_position()` can recover orders from the ledger, but the interaction between restoring a ThetaGang position and a directional position is not fully tested. The `active_side` field is used to disambiguate, but a restored ThetaGang position sets `active_side = "THETA"`, which the directional exit logic does not expect.

**Impact**: If a ThetaGang position is restored on startup and `active_side` is "THETA", `manage_open_position` may be called but will skip because `self.active_side not in self.market_data` → it won't try to manage the ThetaGang position (which is correct—theta exit is handled separately).

---

## Diagram: Complete Strategy Decision Flow

```
                   ┌──────────────────────┐
                   │   Signal Arrives      │
                   │  (score, squeeze_on,  │
                   │   side, mid_trend)    │
                   └────────┬─────────────┘
                            │
                    ┌───────▼────────┐
                    │  squeeze_on?   │
                    └───┬────┬───────┘
                   YES  │    │  NO
        (theta zone)    │    │  (directional zone)
                        │    │
              ┌─────────▼┐  ┌▼──────────┐
              │           │  │ resolve_  │
              │ ThetaGang │  │ entry_    │
              │ evaluate  │  │ side()    │
              │ entry()   │  └──┬────┬───┘
              └─────┬─────┘     │    │
              YES   │  NO       │  NO│  YES
          ┌─────────▼┐  │     ┌─▼───▼──┐
          │ Open     │  │     │ side    │
          │ Theta    │  │     │ C or P  │
          │ position │  │     └──┬──────┘
          └──────────┘  │        │
                        │  ┌─────▼─────────┐
                        │  │ Guards (4.1-   │
                        │  │ 4.9)          │
                        │  │ PASS?         │
                        │  └─────┬─────────┘
                        │   YES  │     NO
                        │  ┌─────▼──┐  │  (skip)
                        │  │ Enter  │  │
                        │  │ Position│  │
                        │  └────────┘  │
                        │              │
                    ────┴────┬─────────┘
                             │
                   ┌─────────▼─────────┐
                   │   Position Open   │
                   │  (manage_open_    │
                   │   position)       │
                   └─────────┬─────────┘
                             │
              ┌──────────────▼──────────────┐
              │  QuoteGuard → SessionGuard  │
              │  → Time Constraint → Trail  │
              │  → Reversal → TP1 → SL     │
              │  → EOD                      │
              └──────────────┬──────────────┘
                             │
                     YES     │      NO
                ┌────────────▼────┐
                │  Exit Position  │   Hold
                └─────────────────┘
```

---

*End of Options Strategy Reference Document*
