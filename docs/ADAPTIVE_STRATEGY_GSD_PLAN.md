# 自適應策略系統 — GSD 開發計劃

## 問題

Consecutive loss → 換策略是盲目切換，沒有診斷「為什麼虧」。
更根本的問題：系統**沒有追蹤 consecutive losses，也沒有記錄進場時的診斷數據**。

## Root Cause（Boil the Lake 追蹤）

| 什麼 | 現況 | 缺口 |
|------|------|------|
| 連虧次數計數 | ❌ 不存在 | `FuturesMonitor` 只有 `cooldown_until`（不分盈虧） |
| 進場診斷快照 | ❌ 不存在 | 知道「虧 60pts」，不知道「進場 momentum=15, VWAP dist=180pts」 |
| 停損原因 | ✅ 有 | `exit_reason` (STOP_LOSS/VWAP/ATR_TRAIL/TP1) 已有記錄 |
| 進場指標 | ✅ 有 | `last_bar["momentum"]`, `vwap`, `atr`, `regime` 都存在 |
| Signal.validate() | ❌ 不存在 | `monitor.py:1037` 呼叫不存在的 method → `AttributeError` |

**結論**: 90% 的數據已存在於 `last_bar`，只差兩個東西：(1) counter (2) 進場快照。

---

## Phase 0: 修現有 Bug（不做 Phase 1 會出事）— ✅ COMPLETE

### 0a. Signal.validate() — ✅ DONE (15 min)

**File**: `core/signal.py`

```python
def validate(self) -> str | None:
    """Returns error message or None if valid"""
    if self.action not in ("BUY", "SELL", "EXIT", "PARTIAL_EXIT"):
        return f"Invalid action: {self.action}"
    if not self.reason:
        return "Missing reason"
    if self.action in ("BUY", "SELL") and self.stop_loss <= 0:
        return "Stop loss must be > 0 for entry"
    return None
```

**Why**: `monitor.py:1037` 呼叫 `signal.validate()` 但不存在 → latent `AttributeError`。
**Test**: `tests/strategies/test_signal.py` — 11 tests already exist, just need implementation.

### 0b. Consecutive Losses Counter — ✅ DONE (30 min)

**File**: `strategies/futures/monitor.py`

Add to `FuturesMonitor.__init__`:
```python
self.consecutive_losses = 0
self.session_losses = []  # List[(timestamp, pnl, exit_reason)]
```

Update in `_strategy_tick` after trade exit:
```python
# After trade closes (pnl known)
if pnl < 0:
    self.consecutive_losses += 1
else:
    self.consecutive_losses = 0
self.session_losses.append((timestamp, pnl, exit_reason))
```

**Why**: 沒有這個 counter，L3 盤中自適應根本觸發不了。
**Test**: Add `test_consecutive_losses_counter` to `tests/futures/test_monitor.py`.

### 0c. Entry Diagnostic Snapshot — ✅ DONE (45 min)

**File**: `strategies/futures/monitor.py` (entry point, ~line 1000-1050)

At trade entry, snapshot `last_bar` state:
```python
entry_diag = {
    "momentum": last_bar.get("momentum", 0),
    "mom_velo": last_bar.get("mom_velo", 0),
    "vwap_distance_pts": abs(close - last_bar.get("vwap", close)),
    "atr": last_bar.get("atr", 0),
    "squeeze_on_recent": bool(df_5m["sqz_on"].iloc[-12:-2].any()),
    "regime": str(current_regime),
    "session": last_bar.get("session", 0),
    "score": score,
    "bars_since_fire": bars_since,
}

# Store with trade record
self.trades[-1]["entry_diag"] = entry_diag
```

**Why**: 沒有 entry diagnostic，診斷規則引擎（Phase 2）沒有數據。
**Test**: Verify snapshot is saved alongside trade record in CSV.

---

## Phase 1: 基礎設施（依賴 Phase 0）— ✅ COMPLETE

### 1. Decision Logger — ✅ DONE (30 min)

**New file**: `core/decision_logger.py`

```python
# logs/decisions.csv
# timestamp,type,session,action,detail,author
# 2026-04-12T13:50:00,post_session,day,tighten_entry,confirm_bars 7→10,system
# 2026-04-12T05:05:00,post_session,night,switch_strategy,counter_vwap→spring_upthrust,system
```

Append-only CSV。不讀取、不修改、不刪除。只寫入。

**Test**: `tests/test_decision_logger.py` — 4 tests (write, read, append-only, concurrent safety).

### 2. Strategy Registry — 45 min

**New file**: `core/strategy_registry.py`

```python
STRATEGY_PERF = {
    "counter_vwap":    {"day_pf": 2.1, "night_pf": 1.4},
    "spring_upthrust": {"day_pf": 1.6, "night_pf": 1.3},
    "vol_squeeze":     {"day_pf": 1.5, "night_pf": 1.2},
}

def select_best(session_type: str, regime: str) -> str:
    """Return best strategy for session + regime combo"""
    ...
```

Hardcoded for now. Future: auto-populated from backtest results.

**Test**: `tests/test_strategy_registry.py` — 6 tests (best strategy, fallback, invalid input).

### 3. Circuit Breaker — 45 min

**New file**: `core/circuit_breaker.py`

```python
class CircuitBreaker:
    def __init__(self, daily_loss_pct=0.02, weekly_loss_pct=0.08):
        ...

    def check(self, session_pnl: float, consecutive_losses: int) -> Action:
        # L3: intra-session
        if consecutive_losses >= 3:
            return Action("DIAGNOSE")  # Not SWITCH — diagnose first

        # L2: post-session
        if session_pnl < -5000:
            return Action("HALT")

        return Action("CONTINUE")
```

Two independent instances: `day_breaker`, `night_breaker`.

**Test**: `tests/test_circuit_breaker.py` — 10 tests (threshold boundaries, day/night independence).

---

## Phase 2: Root Cause Diagnostic（取代機械式換策略）— ✅ COMPLETE

### 4. Diagnostic Rule Engine — ✅ DONE (90 min)

**New file**: `core/diagnostic_engine.py`

```python
def diagnose_losing_streak(
    trades: list[Trade],
    entry_diags: list[dict],
) -> Action:
    """
    Not: "3 losses → switch"
    But: "3 losses, all STOP_LOSS, avg momentum=15 → tighten min_momentum"
    """

    # Pattern 1: All stopped out → entry quality problem
    if all(t.exit_reason == "STOP_LOSS" for t in trades):
        avg_momentum = mean(d["momentum"] for d in entry_diags)
        avg_vwap_dist = mean(d["vwap_distance_pts"] for d in entry_diags)
        avg_atr = mean(d["atr"] for d in entry_diags)

        if avg_vwap_dist > 2 * avg_atr:
            return Action("TIGHTEN_ENTRY", param="confirm_bars", delta=+3,
                         reason="Entry too far from VWAP (chasing)")

        if avg_momentum < 30:
            return Action("TIGHTEN_ENTRY", param="min_momentum", delta=+20,
                         reason="Entry momentum too weak")

    # Pattern 2: All VWAP exits → trend strength problem
    if all(t.exit_reason == "VWAP_EXIT" for t in trades):
        return Action("TIGHTEN_ENTRY", param="min_momentum", delta=+20,
                     reason="VWAP exits frequent → need stronger trend")

    # Pattern 3: SHOCK regime → stop trading
    if any(d["regime"] == "SHOCK" for d in entry_diags):
        return Action("HALT", reason="SHOCK regime detected")

    # Pattern 4: Mixed exits, < 5 trades → normal variance
    if len(trades) < 5:
        return Action("COOLDOWN", duration_mins=15,
                     reason="Normal variance (PF=2.1 has 40% loss rate)")

    # Pattern 5: 5+ losses with rolling PF < 1.0 → possible decay
    rolling_pf = calc_rolling_pf(trades, window=30)
    if rolling_pf < 1.0:
        return Action("SWITCH_STRATEGY",
                     new_strategy=select_best_for_regime(current_regime),
                     reason=f"Rolling PF={rolling_pf:.2f} < 1.0, possible decay")

    return Action("CONTINUE")
```

**Decision tree**:
```
3 losses
  → Check exit pattern
    → All STOP_LOSS
      → Check entry quality
        → High VWAP distance → tighten confirm_bars (stop chasing)
        → Low momentum → raise min_momentum (filter weak signals)
    → All VWAP_EXIT
      → Raise min_momentum (need stronger trend)
    → Mixed exits
      → < 5 trades → COOLDOWN 15min (normal variance)
      → 5+ trades → Check rolling PF
        → PF < 1.0 → SWITCH (genuine decay)
        → PF >= 1.0 → CONTINUE (still profitable)
```

**Test**: `tests/test_diagnostic_engine.py` — 12 tests (each pattern + boundary cases).

### 5. Post-Session Review — 1 hr

**New file**: `scripts/daily_review.py`

Runs at 13:50 (day close) and 05:05 (night close). Reads trade journal, computes session PnL/WR/PF, calls diagnostic engine, writes config update.

```
日盤收盤 → 讀取 trades + entry_diag → 診斷 → 寫入 config/futures_day.yaml
夜盤收盤 → 讀取 trades + entry_diag → 診斷 → 寫入 config/futures_night.yaml

互不影響。
```

Output: `logs/session_reviews/review_2026-04-12_day.json`

**Test**: `tests/test_daily_review.py` — 6 tests (day/night separation, config write-back, diagnostic action).

---

## Phase 3: 整合

### 6. Monitor Integration — 45 min

**Modify**: `strategies/futures/monitor.py`

- Import `CircuitBreaker`, `diagnose_losing_streak`
- On trade close: update counter → call breaker.check() → if DIAGNOSE → call diagnostic engine
- On session close: call `daily_review.py` logic

Hook point: `_strategy_tick` after exit (line ~860).

**Test**: Integration test in `tests/futures/test_monitor.py`.

### 7. Dashboard Pipeline View — 1 hr

**Modify**: `ui/dashboard.py`

New tab showing:
- Day leaderboard (PF, WR, MaxDD — day-only history)
- Night leaderboard (separate)
- Pipeline: [Idea → Backtest → Paper → Live → Retired]
- Current circuit breaker status

**Test**: Manual UI test. No unit test needed (pure rendering).

---

## Test Plan

| Phase | Test File | Tests |
|-------|-----------|-------|
| 0a | `tests/strategies/test_signal.py` | 11 (existing, needs impl) |
| 0b | `tests/futures/test_monitor.py` | 3 (new) |
| 0c | `tests/futures/test_monitor.py` | 3 (new) |
| 1 | `tests/test_decision_logger.py` | 4 |
| 1 | `tests/test_strategy_registry.py` | 6 |
| 1 | `tests/test_circuit_breaker.py` | 10 |
| 2 | `tests/test_diagnostic_engine.py` | 12 |
| 2 | `tests/test_daily_review.py` | 6 |
| 3 | `tests/futures/test_monitor.py` | 4 (integration) |

**Total: +59 tests**

---

## Execution Order

```
Step 0a (15m)  →  Signal.validate()
Step 0b (30m)  →  Consecutive losses counter
Step 0c (45m)  →  Entry diagnostic snapshot
──────────────────────────────────────────  Phase 0 done
Step 1   (30m)  →  Decision Logger
Step 2   (45m)  →  Strategy Registry
Step 3   (45m)  →  Circuit Breaker
──────────────────────────────────────────  Phase 1 done
Step 4   (90m)  →  Diagnostic Rule Engine
Step 5   (60m)  →  Post-Session Review
──────────────────────────────────────────  Phase 2 done
Step 6   (45m)  →  Monitor Integration
Step 7   (60m)  →  Dashboard Pipeline
──────────────────────────────────────────  Phase 3 done
```

**Total: ~7.5 hours (5-6 dev sessions)**

---

## Success Criteria

After implementation, the system must answer YES to:

1. Does `signal.validate()` exist and not crash?
2. Does the monitor track `consecutive_losses`?
3. Does every trade have an entry diagnostic snapshot?
4. Do 3 consecutive losses trigger DIAGNOSE (not blind SWITCH)?
5. Does the diagnostic engine return TIGHTEN_ENTRY for entry quality problems?
6. Does the diagnostic engine return COOLDOWN for normal variance (< 5 trades)?
7. Does the diagnostic engine return SWITCH_STRATEGY only when rolling PF < 1.0?
8. Do day/night sessions have independent counters and config files?
9. Is every decision logged to `logs/decisions.csv`?

---

## Risks

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| Diagnostic false positive (tighten when shouldn't) | Medium | Low | Has COOLDOWN fallback for < 5 trades |
| Config write-back corrupts YAML | High | Low | Write to temp file first, then atomic rename |
| Entry diagnostic slows down entry | Low | Low | Just reading existing dict fields, no computation |
| Day/night counters get mixed up | Medium | Low | Separate class instances, test independence |
