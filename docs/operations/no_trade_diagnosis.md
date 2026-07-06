# No-Trade Diagnosis

## Purpose

When the system shows no trades for an extended period, use this flow to determine whether it's **healthy silence** (correct behavior) or a **bug** (pipeline failure).

---

## Step 1: Check Data Freshness

Open the Dashboard → Futures tab or Pipeline tab.

| Symptom | Likely Cause |
|---|---|
| Bars stop updating (stale > 5 min) | VPN disconnect → PM2 restarted → waiting for next 5m boundary |
| `feed health` shows MXF=infs | Contract subscription lost, reconnect needed |
| Bars update but indicators show stale prices | Tick-5m deque empty, running on api-1m fallback |

**Key diagnostic:** check `logs/market_data/MXF_{tday}_PAPER_indicators.csv` for recent bars:
```bash
tail -3 logs/market_data/MXF_20260430_PAPER_indicators.csv
```

If bars are current → data is healthy. Move to Step 2.

---

## Step 2: Read the Router Trace

The RouterTrace is the single source of truth for "why no trade":

### A. Check latest stdout summary

```bash
grep "RouterTrace" /Users/mylin/.pm2/logs/trading-system-out.log | tail -5
```

Example output:
```
[RouterTrace] ts=2026-04-30 19:25 regime=BEAR selected=None |
counter_vwap=SKIP:NO_FIRE_EVENT edge=0.0 |
spring_upthrust=SKIP:NO_SQUEEZE edge=0.0 |
adaptive_orb=DISABLED:NOT_IN_BEAR_STRATEGIES edge=N/A |
calendar_condor_v2=DISABLED:NOT_IN_BEAR_STRATEGIES edge=N/A
```

### B. Or open the Dashboard Pipeline tab

Shows:
- **Latest status cards** — each strategy's current reason + edge score
- **Skip reason bar chart** — distribution of skip reasons over the session
- **Edge timeline** — edge score trend (is it approaching trigger thresholds?)

### C. Or read the JSONL directly

```bash
cat logs/router_trace/router_trace_20260430.jsonl | python3 -m json.tool --no-ensure-ascii | head -30
```

---

## Step 3: Classify the Silence Pattern

| Pattern | Classification | Action |
|---|---|---|
| `NO_FIRE_EVENT` + `NO_SQUEEZE` | **Healthy silence** — night session, no squeeze activity | No action needed |
| Fire detected but expired (`FIRE_EXPIRED`) | **Healthy** — setup didn't confirm | Review confirm_bars param if pattern repeats |
| Close tracking but not extreme (`NO_COUNTER_EXTREME`) | **Healthy** — price hasn't reversed yet | Wait for reversal |
| Only `DISABLED:NOT_IN_BEAR_STRATEGIES` | **Design choice** — regime restricts eligible strategies | Consider if regime classification is correct |
| Missing RouterTrace entirely | **Bug** — strategy evaluation not reaching router | Check strategy registration in context |
| RouterTrace present but `NO_BAR` or `NO_DATA` | **Bug** — data pipeline issue | Check canonical bar selector, backfill status |

---

## Step 4: Verify Each Strategy's Gate

### Counter-VWAP
```
NO_FIRE_EVENT?   → Check sqz_on / fired indicators in MXF CSV
WEAK_FIRE?       → momentum too low for current threshold
NO_COUNTER_EXTREME? → Price still making new highs/lows, not reversing
VWAP_CONTEXT_INVALID? → Price broke extreme but VWAP/mom conditions not met
```

### Spring-Upthrust
```
NO_SQUEEZE?           → Check bb_upper/bb_lower/sqz_on columns
SPRING_CONTEXT_UNFAVORABLE? → Context filter blocked (check is_spring_long_context_favorable)
```

### Adaptive ORB
```
NO_BREAKOUT?         → Close within ORB range
BREAKOUT_QUALITY_FAILED? → Volume/VWAP/LinReg conditions not met
MODEL_PROB_TOO_LOW?  → ML model confidence below threshold
REGIME_NOT_TRADABLE?   → Current regime (e.g., BEAR) not in allowed set
```

### Calendar Condor V2
```
REGIME_NOT_WEAK?     → Only trades WEAK regime
NIGHT_SESSION_DISABLED? → Night trading off by config
SPREAD_Z_NOT_EXTREME? → Spread and VWAP within normal range
```

---

## Step 5: Check Regime Classification

If you suspect the regime is wrong, check:

```bash
grep "DEBUG_MARK\|RouterInput" /Users/mylin/.pm2/logs/trading-system-out.log | tail -3
```

Example:
```
[DEBUG_MARK] classified regime=BEAR bias=SHORT
[RouterInput] bear_breakout=0.6542 trend=-0.015 adx=20.80 regime=BEAR bias=SHORT
```

If regime seems incorrect, investigate the feature engineering layer (breakout_strength, trend_strength_raw calculations) — not the router config.

---

## Last Resort: Enable Tick-Level Debug

In `config/futures.yaml` (or `futures_night.yaml`):

```yaml
debug:
  tickbar: true    # [TickBar][CLOSE/NEWBAR/ACCUM] per tick
  feed: true       # [FuturesMonitor][ON_TICK] per tick
```

Then restart and watch:
```bash
pm2 restart trading-system
pm2 logs trading-system --lines 50
grep "ON_TICK\|TickBar" /Users/mylin/.pm2/logs/trading-system-out.log
```

**Remember to set both back to `false` after debugging** — tick-level logs are extremely verbose.

---

## Decision Tree (Quick Reference)

```
No trades for 30+ min
  ├─ Bars stale (>5 min)?
  │   ├─ Yes → VPN crash? Check PM2 uptime. Wait for next 5m boundary.
  │   └─ No  → Bars are current.
  │
  ├─ RouterTrace exists?
  │   ├─ No → Bug: strategy pipeline not reaching router.
  │   └─ Yes → Read trace.
  │       │
  │       ├─ NO_FIRE_EVENT + NO_SQUEEZE?
  │       │   └─ ✅ Healthy silence. Night session.
  │       │
  │       ├─ DISABLED:NOT_IN_BEAR_STRATEGIES?
  │       │   └─ ✅ By design. BEAR regime restricts strategies.
  │       │
  │       └─ All strategies = some specific skip reason?
  │           └─ 🔍 Check that reason against the table above.
```

---

## Related

- `docs/architecture/system_overview.md` — entry point
- `docs/architecture/strategy_router.md` — regime mapping + skip reasons
- `docs/operations/pm2_debugging.md` — VPN / restart debugging
- `ui/dashboard.py` — Pipeline tab implementation
