# TX Feed Incident Postmortem

**Date:** 2026-04-21  
**Severity:** HIGH - Trading quality and runtime trust degradation  
**Status:** PARTIALLY FIXED / FOLLOW-UP REQUIRED

---

## Summary

During the 2026-04-21 day session, the system repeatedly reported:

- `TX stale: ...`
- `[shioaji_client] 獲取合約 TX 錯誤: 'NoneType' object is not subscriptable`
- `[kbars] no contract for TX`
- `[CROSS] tx=UNKNOWN tmf=UNKNOWN`

This looked like a single TX data outage, but it was actually a **two-stage failure chain**:

1. The cross-regime TX kbar fallback requested an invalid ticker (`TX`), which produced `no contract for TX`.
2. The live TX subscription resolver could still choose the wrong TX futures contract by container iteration order, which made freshness unreliable even when subscription succeeded.

---

## Impact

1. Cross-regime logic degraded into `tx=UNKNOWN tmf=UNKNOWN`.
2. Runtime health logs became noisy and misleading.
3. TX freshness lost credibility as an operational signal.
4. Operators could not immediately tell whether the problem was:
   - bad ticker resolution,
   - failed subscription,
   - wrong contract month,
   - or true broker/feed silence.

---

## Timeline Reconstruction

### Phase 1: Runtime symptoms

The live process showed repeated TX health warnings and contract lookup failures:

- `TX stale: ...`
- `[shioaji_client] 獲取合約 TX 錯誤`
- `[kbars] no contract for TX`

At this stage the visible symptom suggested "TX feed is stale", but the first confirmed bug was actually in the **kbar fallback path**.

### Phase 2: First root cause identified

`strategies/futures/monitor.py` used:

```python
self.client.get_kline("TX", interval="5m")
```

But `strategies/futures/squeeze_futures/data/shioaji_client.py` did not support bare `TX` as a legal futures contract lookup. That caused:

- invalid contract resolution,
- the `NoneType` lookup failure,
- and `[kbars] no contract for TX`.

### Phase 3: First fix applied

The TX kbar path was hardened:

- `get_kline("TX")` support was normalized in `shioaji_client.py`
- monitor fallback was changed to request `TXFR1`

This removed the first failure mode from the code path.

### Phase 4: Second root cause surfaced

After the first fix, the runtime still showed TX freshness problems. Logs showed successful subscription messages such as:

- `📡 Subscribed TX tick: TXFC7`

But the tick callback path did not show corresponding first-seen TX ticks, while TX freshness still deteriorated.

This exposed a second issue: **live TX subscription could pick the wrong TX contract**.

### Phase 5: Subscription resolver investigation

`main.py` used `resolve_tx_contract(...)` to choose the TX contract for live subscription. The earlier resolver collected TXF candidates and could still fall back to container iteration order. In practice that allowed a far-month contract such as `TXFC7` to be selected instead of the expected same-month mapping from the current TMF contract.

### Phase 6: Hardening direction

The resolver was updated to prefer:

1. the TXF contract mapped from the active TMF month (`TMFE6 -> TXFE6`)
2. otherwise the nearest valid TXF contract by delivery date

Regression tests were added to lock in:

- nearest actual TXF preference
- TMF-matched TXF month preference

---

## Root Cause Analysis

## Root Cause 1: TX kbar fallback used an invalid ticker

**File:** `strategies/futures/monitor.py`  
**Relevant path:** cross-regime fallback

The monitor asked the client for TX bars using bare `TX`. That symbol was not supported by the futures client lookup rules, which expected either:

- a legal rolling symbol such as `TXFR1`, or
- a real TXF contract code.

### Why this failed

`shioaji_client.py` resolved futures contracts by explicit cases and category lookups. Bare `TX` did not fit the supported resolution pattern, so the fallback raised the observed contract lookup errors.

### Result

This bug directly caused:

- `no contract for TX`
- contract lookup exceptions
- empty TX fallback bars

---

## Root Cause 2: Live TX subscription selection was not deterministic enough

**File:** `main.py`  
**Relevant path:** `resolve_tx_contract(...)`

The live subscription path was separate from the kbar path. Even after the kbar ticker bug was fixed, the live resolver could still choose an unintended TXF contract if direct matched lookup failed and the code fell back to candidate collection / iteration order.

### Why this was dangerous

Subscription could succeed at the broker level while still targeting the wrong contract month. Operationally, this is worse than a clean failure because:

1. logs show "subscribed successfully"
2. operators assume TX is covered
3. freshness may still never become trustworthy

### Result

This created a second failure mode:

- subscription success without a reliable near-month TX signal
- stale or misleading cross-regime behavior

---

## Contributing Factors

### 1. Multiple TX resolution paths existed

The repo had different TX handling logic in:

- `main.py`
- `strategies/futures/monitor.py`
- `strategies/futures/squeeze_futures/data/shioaji_client.py`

That allowed one path to be fixed while another remained wrong.

### 2. FeedHealth bucket boundaries were too loose

`main.py` currently classifies TX with:

```python
TX_PREFIXES = ("TXF", "TX", "TXO")
```

This means TXO option ticks can also satisfy the `TX` prefix test. That weakens the meaning of `FeedHealth["TX"]` and can blur the line between:

- real TX futures freshness
- and unrelated option activity.

### 3. Runtime validation started too late

The system warned only after freshness decayed. It did not immediately assert:

- whether the subscribed TX contract was the expected month
- whether the first TX tick arrived within a bounded startup window

### 4. Cross-regime observability was weak

The runtime surfaced `tx=UNKNOWN tmf=UNKNOWN`, but that message did not clearly distinguish:

- no TX bars,
- bad TX contract,
- no TX ticks,
- or time-alignment failure.

---

## What Was Fixed

### Fix 1: TX kbar fallback no longer relies on bare `TX`

**Files:**

- `strategies/futures/monitor.py`
- `strategies/futures/squeeze_futures/data/shioaji_client.py`

Changes:

1. `monitor.py` now requests TX fallback bars with `TXFR1`
2. `shioaji_client.py` now normalizes `TX` / `TXF` aliases to a legal front-month TXF contract
3. regression tests were added for TX alias and kline resolution

### Fix 2: TX live resolver now prefers TMF-matched month

**File:** `main.py`

Changes:

1. `resolve_tx_contract(api, reference_contract)` was added
2. when TMF is known, the resolver first tries the mapped TXF code for the same month
3. fallback candidate selection now sorts by delivery date instead of trusting raw container order

### Fix 3: Regression coverage

**Files:**

- `tests/test_tx_contract_resolution.py`
- `tests/test_tx_subscription_resolution.py`

Coverage added for:

1. TX alias contract resolution
2. TX kline resolution through alias normalization
3. nearest actual TXF preference
4. TMF-matched TXF month preference

---

## What Is Still Not Fully Closed

1. **FeedHealth TX bucket still includes `TXO` prefixes**
   - this should be split so TX freshness means TX futures only

2. **Cross-regime still reports `tx=UNKNOWN` during time-alignment failures**
   - this is safer than false positives, but operationally too vague

3. **Live startup should assert first TX tick arrival**
   - subscription success alone is not enough

4. **The final resolver hardening needs explicit live verification every time it changes**
   - test coverage protects logic
   - runtime confirmation protects real contract behavior

---

## Lessons Learned

### Lesson 1: "Subscribed" is not the same as "healthy"

A broker accepting a subscription does not prove:

- the contract month is correct
- the feed is active
- or the signal is usable

Runtime health must validate **first tick arrival** and **expected contract identity**.

### Lesson 2: Symbol handling must be centralized

TX contract resolution cannot live in multiple loosely coupled functions. One canonical resolver must serve:

- live subscription
- kbar fallback
- recovery / diagnostics
- future cross-regime features

### Lesson 3: Prefix-based bucket logic is dangerous in mixed-asset systems

Using broad prefixes like `("TXF", "TX", "TXO")` for one health bucket hides the difference between:

- TX futures
- TX options
- and generic TX-related symbols

In a trading system, ambiguous freshness is almost as dangerous as stale freshness.

### Lesson 4: Runtime observability must explain the failure mode

Good runtime telemetry should distinguish:

1. bad contract resolution
2. subscribe failure
3. no first tick after subscribe
4. stale previously healthy feed
5. misaligned cross-regime bars

Without that separation, operators burn time debugging symptoms instead of causes.

---

## Prevention Plan

### Code-level prevention

1. Keep a single canonical TX resolver shared by:
   - `main.py`
   - futures monitor
   - Shioaji client helpers

2. Make TX freshness track **TXF only**
   - remove `TXO` from the TX health bucket

3. Add startup assertions:
   - subscribed TX contract must match expected month
   - TX must produce a first tick inside a bounded window

4. Keep regression tests for:
   - invalid bare ticker handling
   - nearest TXF selection
   - TMF-to-TXF month mapping
   - no far-month preference by container order

### Operational prevention

1. After each trading-system restart, check:
   - `Subscribed TX tick: ...`
   - first observed TX tick code
   - `feed health | TX=...`

2. Treat these as separate incidents:
   - `no contract for TX`
   - `subscribed but no first TX tick`
   - `TX stale after healthy startup`

3. Do not trust cross-regime output while TX is:
   - unresolved,
   - unseen,
   - or time-misaligned.

---

## Concrete Follow-Up Items

1. Split TX futures freshness from TXO option freshness in `tick_dispatcher()`
2. Add a startup guard: if no TXF tick arrives within the grace window, surface a dedicated alert
3. Improve cross-regime logging so `UNKNOWN` includes explicit cause
4. Keep TMF->TXF month mapping as the first-priority contract selection rule

---

## Files Touched During Remediation

- `main.py`
- `strategies/futures/monitor.py`
- `strategies/futures/squeeze_futures/data/shioaji_client.py`
- `tests/test_tx_contract_resolution.py`
- `tests/test_tx_subscription_resolution.py`

---

## Final Takeaway

This incident was a reminder that **data-plane failures in trading systems are often layered**. The visible symptom was "TX stale", but the actual chain was:

1. invalid TX kbar symbol
2. then nondeterministic TX subscription selection
3. plus weak health bucket boundaries and vague observability

The lesson is not only "fix the bug", but also:

**make the system prove that it subscribed to the right contract, received the first tick, and is measuring the right asset when it says the feed is healthy.**
