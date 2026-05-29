# TMF Calendar Spread Strategy Specification

## 1. Strategy Purpose

`tmf_spread` is a Taiwan futures calendar spread strategy designed for near-month / far-month MXF or TMF spread trading.

The strategy trades the deviation between near and far contracts:

```text
spread = near_close - far_close
spread_z = normalized spread deviation
```

The core idea:

```text
When spread_z is extremely high:
    short near-month
    long far-month

When spread_z is extremely low:
    long near-month
    short far-month
```

After entry, the strategy manages the two legs independently:

```text
1. If one leg loses more than release_stop_points, release that leg.
2. Keep the remaining leg.
3. Apply direction-aware trailing stop to the remaining leg.
```

---

## 2. Data Pipeline

### 2.1 Calendar Spread Data

The dashboard button **更新價差資料** runs:

```text
scripts/fetch_calendar_spread_data.py
```

This script fetches near-month and far-month kbars, then rebuilds:

```text
mxf_calendar_spread_*.csv
```

The CSV contains analytics fields such as:

```text
near_close
far_close
spread
spread_z
spread_ma
spread_std
short_ma
short_std
```

### 2.2 Hot Reload

`spread_loader` checks the CSV modification time.

When CSV mtime changes:

```text
spread_loader hot-reloads the latest calendar spread dataset
```

Important contract:

```text
CSV hot-reload may affect:
1. dashboard analytics
2. the next entry signal

CSV hot-reload must not affect:
1. active position release
2. active position trail
3. active position stop
```

---

## 3. Runtime Contract

The strategy separates two worlds:

```text
Entry Z      = trade contract snapshot
Current Z    = dashboard / analytics view
```

At entry:

```python
self._entry_spread_z = current_spread_z
```

During position management:

```text
_manage_position uses _entry_spread_z snapshot
not hot-reload current_spread_z
```

Dashboard may show both:

```text
Entry Z: 3.00 | Current Z: 1.84
```

This prevents CSV hot-reload from polluting active trade logic.

---

## 4. Entry Logic

### 4.1 Direction

If:

```text
spread_z > entry_z
```

Then:

```text
SELL_NEAR_BUY_FAR
near_side = SHORT
far_side  = LONG
```

If:

```text
spread_z < -entry_z
```

Then:

```text
BUY_NEAR_SELL_FAR
near_side = LONG
far_side  = SHORT
```

### 4.2 Required State at Entry

At entry, the strategy must persist:

```text
_near_side
_far_side
_near_entry
_far_entry
_entry_spread_z
release_state = BOTH_HELD
```

Never infer leg direction from lifecycle names.

Correct:

```python
self._near_side = "SHORT"
self._far_side = "LONG"
```

Incorrect:

```python
RELEASE_NEAR -> SHORT
RELEASE_FAR -> LONG
```

---

## 5. Leg PnL Calculation

Each leg must calculate PnL according to its own side.

```python
def leg_pnl(side, entry, price):
    if side == "LONG":
        return price - entry
    if side == "SHORT":
        return entry - price
    raise ValueError(f"Invalid side: {side}")
```

Examples:

```text
LONG:
    entry = 41676
    price = 41696
    pnl = +20

SHORT:
    entry = 41576
    price = 41596
    pnl = -20
```

---

## 6. Release Logic

### 6.1 Release Stop

The release stop is controlled by:

```yaml
release_stop_points: 20
```

Meaning:

```text
If near leg PnL <= -20pt, release near leg.
If far leg PnL <= -20pt, release far leg.
```

The two legs are evaluated independently.

### 6.2 Remaining Leg Mapping

Correct contract:

```text
RELEASE_NEAR -> remaining_leg = FAR
RELEASE_FAR  -> remaining_leg = NEAR
```

Remaining side must come from the original entry side:

```python
if released_leg == "NEAR":
    self._side = self._far_side
    self._remaining_leg = "FAR"

elif released_leg == "FAR":
    self._side = self._near_side
    self._remaining_leg = "NEAR"
```

Critical bug fixed:

```text
Old behavior:
    RELEASE_NEAR hardcoded self._side = SHORT
    RELEASE_FAR hardcoded self._side = LONG

Problem:
    SELL_NEAR_BUY_FAR releases NEAR
    Remaining FAR is actually LONG
    But old logic marked it as SHORT
    Trail direction became completely wrong
```

---

## 7. Trailing Stop Logic

After one leg is released, the remaining leg uses a direction-aware trailing stop.

Controlled by:

```yaml
trail_distance_points: 30
```

### 7.1 LONG Remaining Leg

For a long remaining leg:

```text
Remember highest price after release.
If price makes new high, update peak.
Exit when price falls trail_distance_points from peak.
```

Formula:

```python
trail_peak = max(trail_peak, current_price)
trail_stop = trail_peak - trail_distance_points

if current_price <= trail_stop:
    exit_remaining_leg()
```

State:

```text
trail_mode = PEAK_MINUS_DISTANCE
```

### 7.2 SHORT Remaining Leg

For a short remaining leg:

```text
Remember lowest price after release.
If price makes new low, update nadir.
Exit when price rebounds trail_distance_points from nadir.
```

Formula:

```python
trail_nadir = min(trail_nadir, current_price)
trail_stop = trail_nadir + trail_distance_points

if current_price >= trail_stop:
    exit_remaining_leg()
```

State:

```text
trail_mode = NADIR_PLUS_DISTANCE
```

---

## 8. Example: SELL_NEAR_BUY_FAR

Entry:

```text
near_entry = 41576
far_entry  = 41676
near_side  = SHORT
far_side   = LONG
```

If near rises to 41636:

```text
near_pnl = 41576 - 41636 = -60
```

Then:

```text
RELEASE_NEAR
remaining_leg = FAR
remaining_side = LONG
```

Trail init:

```text
trail_peak = far_current_price
trail_stop = trail_peak - 30
trail_mode = PEAK_MINUS_DISTANCE
```

Correct dashboard caption:

```text
Trail: leg=FAR side=LONG peak=41696 stop=41666 last=41690 dist=6pt
```

---

## 9. State JSON Contract

Current snapshot:

```text
/tmp/mts_position_state.json
```

Recommended fields:

```json
{
  "strategy": "tmf_spread",
  "has_position": true,
  "lifecycle": "TRAILING",
  "release_state": "NEAR_RELEASED",

  "near_side": "SHORT",
  "far_side": "LONG",

  "near_status": "RELEASED",
  "far_status": "ACTIVE",

  "remaining_leg": "FAR",
  "remaining_side": "LONG",

  "entry_spread_z": 3.0,
  "current_spread_z": 1.84,

  "release_stop_points": 60,
  "trail_distance_points": 30,

  "trail_mode": "PEAK_MINUS_DISTANCE",
  "trail_peak": 41696,
  "trail_nadir": null,
  "trail_stop": 41666,

  "near_realized_pnl": -60,
  "far_unrealized_pnl": 20
}
```

Important:

```text
Dashboard must render state JSON.
Dashboard must not infer side from lifecycle text.
```

---

## 10. Event Ledger Contract

State JSON is only a current snapshot.  
Fast spread trades can enter, release, and exit before dashboard reloads.

Therefore the system should also write append-only event history:

```text
logs/mts_spread_events.jsonl
```

Example events:

```json
{"event":"ENTRY","direction":"SELL_NEAR_BUY_FAR","near_side":"SHORT","far_side":"LONG","entry_z":3.0}
{"event":"RELEASE_NEAR","remaining_leg":"FAR","remaining_side":"LONG","near_pnl":-60}
{"event":"EXIT_REMAINING","reason":"LONG_TRAIL_STOP","remaining_leg":"FAR","realized_pnl":35}
```

Dashboard should display recent events even when:

```text
has_position = false
```

This prevents missing completed trades.

---

## 11. Observability Contract

### 11.1 Heartbeat

Every `_mts_tick` should write a heartbeat state:

```text
MTS_HB
```

Purpose:

```text
1. Confirm _mts_tick is alive.
2. Confirm spread data is available.
3. Confirm state writer works.
4. Confirm dashboard reads the same file.
```

### 11.2 State Writer Logs

Required logs:

```text
[MTS_STATE_WRITE_ATTEMPT]
[MTS_STATE_WRITE_OK]
[MTS_STATE_WRITE_FAILED]
```

Use:

```python
logger.exception(...)
```

for failures, never silent `except: pass`.

### 11.3 Snapshot Drift Log

During active position:

```text
[MTS_SPREAD_SNAPSHOT] entry_spread_z=3.0000 current_spread_z=1.8400 hot_reload_safe=True
```

This proves active position uses entry snapshot while dashboard shows current analytics.

### 11.4 Release Log

Recommended:

```python
logger.info(
    "[TMF_SPREAD_RELEASE] released=%s remaining_leg=%s remaining_side=%s "
    "near_side=%s far_side=%s trail_mode=%s",
    released_leg,
    remaining_leg,
    self._side,
    self._near_side,
    self._far_side,
    self._trail_mode,
)
```

---

## 12. Dashboard Contract

Dashboard should distinguish command state from execution state.

### 12.1 Manual Trade Button

After pressing the button:

```text
1. Write flag file.
2. Show command accepted message.
3. st.rerun().
4. Dashboard reloads state JSON.
5. If has_position=True, show position table.
6. If not yet consumed, show waiting state.
```

Preferred message:

```text
手動交易指令已送出，等待 trading-system consume flag。
結果將於下方持倉 / 委託區塊更新。
```

Avoid misleading text:

```text
今日尚無委託單記錄
```

Better:

```text
尚未看到委託 / 持倉更新，可能仍在等待 trading-system 處理。
```

### 12.2 Position Display

Dashboard should show:

```text
near_side / far_side
near_status / far_status
remaining_leg
remaining_side
release_state
trail_mode
entry_z
current_z
release_stop_points
trail_distance_points
```

Dashboard must not infer:

```text
TRAILING_SHORT = near short + far short
```

Correct interpretation:

```text
TRAILING_SHORT = remaining leg is SHORT
```

---

## 13. Config

Recommended config:

```yaml
mts:
  enabled: true
  strategy: tmf_spread

  params:
    entry_z: 2.5
    release_stop_points: 20
    trail_distance_points: 30
```

Parameter meanings:

| Parameter | Meaning |
|---|---|
| `entry_z` | spread z-score threshold for entry |
| `release_stop_points` | losing leg release threshold (pt) |
| `trail_distance_points` | remaining leg trailing stop distance (pt) |

Current recommended values:

| Parameter | Value | Remark |
|---|---:|---|
| `entry_z` | 2.5 | Must be crossed for entry |
| `release_stop_points` | 20 | Fast containment of losing directional leg |
| `trail_distance_points` | 30 | Reduces premature exit on remaining leg |

---

## 14. Backtest Plan

The strategy should be backtested as an event-driven spread lifecycle, not a simple single-symbol vectorized entry/exit.

Recommended approach:

```text
1. Use loop simulation to generate event ledger and equity curve.
2. Feed equity curve into vectorbt for statistics.
3. Run parameter matrix.
```

Suggested parameter grid:

```text
entry_z = 2.0 / 2.5 / 3.0
release_stop_points = 40 / 60 / 80 / 100
trail_distance_points = 20 / 30 / 40 / 50
```

Metrics:

```text
total_pnl
profit_factor
entry_count
release_count
exit_count
avg_hold_bars
max_drawdown
washout_rate
actual_release_slippage
```

Important observation from live testing:

```text
release_stop_points = 20 was too tight.
Actual release happened around -102pt to -187pt.
This indicates fast leg movement and asynchronous near/far pricing.
```

---

## 15. Minimal Reference Implementation

```python
class SpreadPosition:
    def __init__(self, release_stop_points=20, trail_distance_points=30):
        self.release_stop_points = release_stop_points
        self.trail_distance_points = trail_distance_points
        self.reset()

    def reset(self):
        self.near_side = None
        self.far_side = None
        self.near_entry = None
        self.far_entry = None
        self.entry_spread_z = None

        self.release_state = "FLAT"
        self.remaining_leg = None
        self.remaining_side = None

        self.trail_peak = None
        self.trail_nadir = None
        self.trail_stop = None
        self.trail_mode = None

    def enter(self, spread_z, near_price, far_price, entry_z):
        if spread_z >= entry_z:
            self.near_side = "SHORT"
            self.far_side = "LONG"
            direction = "SELL_NEAR_BUY_FAR"

        elif spread_z <= -entry_z:
            self.near_side = "LONG"
            self.far_side = "SHORT"
            direction = "BUY_NEAR_SELL_FAR"

        else:
            return None

        self.near_entry = near_price
        self.far_entry = far_price
        self.entry_spread_z = spread_z
        self.release_state = "BOTH_HELD"

        return {
            "event": "ENTRY",
            "direction": direction,
            "near_side": self.near_side,
            "far_side": self.far_side,
            "entry_spread_z": self.entry_spread_z,
        }

    def leg_pnl(self, side, entry, price):
        if side == "LONG":
            return price - entry
        if side == "SHORT":
            return entry - price
        raise ValueError(side)

    def manage(self, near_price, far_price):
        assert self.near_side in ("LONG", "SHORT")
        assert self.far_side in ("LONG", "SHORT")

        near_pnl = self.leg_pnl(self.near_side, self.near_entry, near_price)
        far_pnl = self.leg_pnl(self.far_side, self.far_entry, far_price)

        if self.release_state == "BOTH_HELD":
            if near_pnl <= -self.release_stop_points:
                return self.release_leg("NEAR", far_price, near_pnl)

            if far_pnl <= -self.release_stop_points:
                return self.release_leg("FAR", near_price, far_pnl)

            return {"event": "HOLD_BOTH", "near_pnl": near_pnl, "far_pnl": far_pnl}

        price = far_price if self.remaining_leg == "FAR" else near_price
        return self.update_trailing_stop(price)

    def release_leg(self, released_leg, remaining_price, released_pnl):
        if released_leg == "NEAR":
            self.release_state = "NEAR_RELEASED"
            self.remaining_leg = "FAR"
            self.remaining_side = self.far_side

        elif released_leg == "FAR":
            self.release_state = "FAR_RELEASED"
            self.remaining_leg = "NEAR"
            self.remaining_side = self.near_side

        else:
            raise ValueError(released_leg)

        if self.remaining_side == "LONG":
            self.trail_peak = remaining_price
            self.trail_stop = self.trail_peak - self.trail_distance_points
            self.trail_mode = "PEAK_MINUS_DISTANCE"

        elif self.remaining_side == "SHORT":
            self.trail_nadir = remaining_price
            self.trail_stop = self.trail_nadir + self.trail_distance_points
            self.trail_mode = "NADIR_PLUS_DISTANCE"

        else:
            raise ValueError(self.remaining_side)

        return {
            "event": f"RELEASE_{released_leg}",
            "released_leg": released_leg,
            "released_pnl": released_pnl,
            "remaining_leg": self.remaining_leg,
            "remaining_side": self.remaining_side,
            "trail_mode": self.trail_mode,
            "trail_stop": self.trail_stop,
        }

    def update_trailing_stop(self, price):
        if self.remaining_side == "LONG":
            self.trail_peak = max(self.trail_peak, price)
            self.trail_stop = self.trail_peak - self.trail_distance_points

            if price <= self.trail_stop:
                event = {
                    "event": "EXIT_REMAINING",
                    "reason": "LONG_TRAIL_STOP",
                    "remaining_leg": self.remaining_leg,
                    "price": price,
                    "trail_peak": self.trail_peak,
                    "trail_stop": self.trail_stop,
                }
                self.reset()
                return event

        elif self.remaining_side == "SHORT":
            self.trail_nadir = min(self.trail_nadir, price)
            self.trail_stop = self.trail_nadir + self.trail_distance_points

            if price >= self.trail_stop:
                event = {
                    "event": "EXIT_REMAINING",
                    "reason": "SHORT_TRAIL_STOP",
                    "remaining_leg": self.remaining_leg,
                    "price": price,
                    "trail_nadir": self.trail_nadir,
                    "trail_stop": self.trail_stop,
                }
                self.reset()
                return event

        return {
            "event": "HOLD_REMAINING",
            "remaining_leg": self.remaining_leg,
            "remaining_side": self.remaining_side,
            "trail_mode": self.trail_mode,
            "trail_stop": self.trail_stop,
        }
```

## Future Research

### post_release_max_hold_bars (Not Implemented)

After release, the remaining leg becomes a naked directional position. A time-stop limits how long this exposure persists:

```yaml
# Proposed config (not implemented):
post_release_max_hold_bars: 6
```

Purpose: prevent directional exposure from persisting indefinitely if the trailing stop never triggers (e.g., range-bound market after release).

### Other Research Topics

See ADR-006 for full list: partial hedge, dynamic re-hedging, volatility-scaled trail, delta exposure cap, asymmetric release thresholds, mean-reversion re-entry.

---

## 16. Critical Rules

1. Do not hardcode side from release state.
2. Always store near_side and far_side at entry.
3. Release decisions use leg PnL, not spread_z.
4. Trail decisions use remaining leg price path, not spread_z.
5. Hot-reloaded CSV affects only dashboard analytics and next entry.
6. Active position must use entry_spread_z snapshot.
7. Dashboard must render state, not infer state.
8. State JSON is current snapshot only.
9. Event ledger is required for historical lifecycle visibility.
10. `release_stop_points` and `trail_distance_points` must be separate parameters.
