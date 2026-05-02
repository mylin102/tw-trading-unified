# Breakout Engine v2 Specification

**Version:** 2.0
**Author:** Trading System Upgrade
**Date:** 2026-05

---

# 1. Objective

Upgrade breakout detection from percentage-based to **ATR-normalized, regime-aware, multi-stage entry system**.

---

# 2. Core Formula

## 2.1 Breakout Strength (ATR-normalized)

```
breakout_strength = (Close - High_20_prev) / ATR
```

### Notes:

* `High_20_prev = rolling(20).max().shift(1)`
* ATR must include safety floor:

```
ATR_safe = max(ATR, ATR_floor)
```

---

# 3. Threshold Design

## 3.1 Dual Threshold System

| Type               | Threshold | Purpose |
| ------------------ | --------- | ------- |
| Early Breakout     | 0.15      | 捕捉趨勢起點  |
| Confirmed Breakout | 0.25      | 確認強勢突破  |

---

# 4. Regime-aware Threshold

```
if regime == "SQUEEZE":
    threshold = 0.25

elif regime == "TREND":
    threshold = 0.15

elif regime == "WEAK":
    threshold = 0.20

elif regime == "CHOP":
    skip breakout
```

---

# 5. Three-stage Breakout Confirmation

## 5.1 Structure

```
close > High_20_prev
```

## 5.2 Strength

```
breakout_strength >= threshold
```

## 5.3 Behavior

```
volume_spike >= 1.5
and close > vwap
```

---

# 6. Entry System (Scout + Scale)

## 6.1 Scout Entry (Early)

```
if breakout_strength >= 0.15 and regime == "TREND":
    action = BUY
    size = 0.3
    tag = "EARLY_BREAKOUT"
```

---

## 6.2 Scale Entry (Confirmed)

```
if breakout_strength >= 0.25:
    action = BUY
    size = 0.7
    tag = "CONFIRMED_BREAKOUT"
```

---

# 7. Full Entry Logic

```
if (
    close > high_20_prev
    and volume_spike >= 1.5
    and close > vwap
):
    if breakout_strength >= 0.25:
        enter_full_position()

    elif breakout_strength >= 0.15 and regime == "TREND":
        enter_scout_position()
```

---

# 8. ATR Safety Mechanism

## 8.1 Static Floor

```
ATR_floor = 50
```

## 8.2 Dynamic Floor (Recommended)

```
ATR_floor = close * 0.0015
```

---

# 9. Additional Filters

## 9.1 Session Stabilization

```
bars_since_open >= 5
```

## 9.2 Avoid Noise

```
spread_is_valid == True
mid_price > 0
```

---

# 10. Output Fields

| Field                  | Description             |
| ---------------------- | ----------------------- |
| breakout_strength_atr  | ATR normalized strength |
| is_structural_breakout | close > high_20_prev    |
| is_confirmed_breakout  | full condition met      |
| entry_type             | EARLY / CONFIRMED       |

---

# 11. Strategy Integration

## adaptive_orb

* Uses breakout engine as trigger
* Applies scout + scale
* Regime-aware threshold

---

# 12. Expected Behavior

| Scenario        | Behavior                |
| --------------- | ----------------------- |
| Early trend     | Scout entry triggered   |
| Strong breakout | Full position           |
| Chop            | No entry                |
| Fake breakout   | Filtered by volume/VWAP |

---

# 13. Key Insight

This system separates:

* **Position (structure)**
* **Strength (ATR)**
* **Behavior (volume + VWAP)**

---

# 14. Final Conclusion

* 0.25 is NOT replaced — it becomes confirmation layer
* 0.15 unlocks early trend capture
* Edge comes from:

  * regime filtering
  * position scaling
  * trend continuation

---

# 15. Next Steps

* Backtest Scout vs Confirmed PnL split
* Add exit optimization (ATR trailing / structure break)
* Integrate with Strategy Router v2

---

