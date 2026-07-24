# I-001: Order ID Session-Date Prefix 系統性錯誤

**Status:** Root Cause Confirmed / Permanent Fix Pending
**Detected:** 2026-07-17
**Primary Root Cause (Confirmed):** Stale cached session date without trading-day boundary invalidation
**Secondary Defects (Confirmed, not primary trigger):** Broad exception masking, wall-clock domain fallback
**Refuted Hypotheses:** `get_session_date_str()` failure, intermittent import/calendar error
**Impact:** P1 data-integrity, P2 operations
**Fix PR:** `fix/taifex-trading-day-provenance` (planned)

---

## Incident Lifecycle

| Stage | Status |
|---|---|
| Incident Identification | ✅ Complete |
| Scope Assessment | ✅ Complete |
| Impact Assessment | ✅ Complete |
| Mitigation (Diagnostic) | ✅ Deployed |
| Root Cause Confirmation | ✅ **Confirmed via order timestamp analysis across 9 files** |
| Permanent Fix | ⏳ Planned: session-date rollover refresh |
| Historical Data Correction | ⏳ Pending fix completion |
| Verification | ⏳ Pending production deploy + monitoring |

---

## Primary Root Cause

**`OrderManager.__init__()` computes `self._session_date` once via `get_session_date_str()`, then never refreshes it.**

When a single OrderManager instance crosses a TAIFEX trading-day boundary (15:00), all orders after 15:00 inherit the stale prefix from the previous trading day.

### The Pattern (100% reproducible from data)

| File | Session type | Pre-15:00 prefix | Post-15:00 prefix | Correct? | Cause |
|---|---|---|---|---|---|
| 20260707 | Day → Night | 20260707 | 20260708 | ✅ PM2 restart before 15:00 | Fresh init |
| 20260708 | Day → Night | 20260708 | 20260708 | ❌ No restart | Stale cache |
| 20260709 | Day → Night | 20260709 | 20260709 | ❌ No restart | Stale cache |
| 20260710 | Night only | — | 20260710 | ✅ PM2 restart | Fresh init |
| 20260713 | Day → Night | 20260713 | 20260713 | ❌ No restart | Stale cache |
| 20260714 | Night→Day* | — | 20260714 | ✅ PM2 restart | Fresh init |
| 20260715 | Night→Day* | — | 20260715 | ✅ PM2 restart | Fresh init |
| 20260716 | Night→Day* | — | 20260716 | ✅ PM2 restart | Fresh init |
| 20260717 | Day → Night | 20260717 | 20260717 | ❌ No restart | Stale cache |

*Crossing midnight (00:00) does NOT trigger a boundary — TAIFEX trading-day semantics change at 15:00, not 00:00.*

### Every crossing with no PM2 restart → wrong prefix. Every PM2 restart → correct prefix.

This is **cached derived state without invalidation**, not an intermittent failure.

---

## Secondary Defects (Not the primary trigger, but real)

1. **`except Exception` with no logging** — would mask a real calendar resolver failure if one occurred
2. **Wall-clock fallback** (`datetime.now().strftime`) — always produces wrong date for night sessions

These did NOT cause this incident but remain latent defects.

---

## Refuted Hypotheses (Remove from consideration)

- `get_session_date_str()` never failed — it returned correct values at every init
- No import/calendar/environment error occurred
- No intermittent failure pattern exists — the staleness is deterministic

---

## Historical Correction

Now straightforward: derive `canonical_trading_session_date` from each order's `submitted_at`:

```python
canonical_date = resolve_taifex_trading_day(submitted_at)
```

No need to guess based on PM2 restart timing. Still:
- Do NOT rename order IDs
- Do NOT modify original ledger
- Add append-only correction mapping

---

## 7/7 Mixed Prefix Explanation

- Pre-15:00 instance held `20260707` (correct for day session)
- PM2 restart (or new instance) at ~15:10 produced fresh `20260708` (correct for night session)
- Both persisted to same file → mixed prefixes observed
- No resolver intermittency required

---

## Formal Finding

```
Primary:
  Session date computed once at __init__, never refreshed at trading-day boundaries (15:00).
  → 100% reproducible stale prefix after any non-restarted boundary crossing.

Secondary:
  except Exception without logging masks unrelated failures.
  Wall-clock fallback always violates TAIFEX semantics.

Refuted:
  get_session_date_str() failure. Calendar/environment error. Import issue.
```

---

## Fix Direction

1. ✅ Diagnostic logging + provenance fields (deployed)
2. **`_refresh_session_date_if_needed()` called before every order ID allocation**
3. Atomic rollover: detect date change → reindex sequence → allocate next ID
4. Fix `except Exception` boundary
5. Domain-aware fallback (secondary)
6. Add `trading_session_date` as authoritative field, decoupled from order ID
7. Historical correction mapping (append-only, no rename)

---

## Key Regression Test

```python
manager = OrderManager(now="2026-07-17T13:34:00+08:00")
# Pre-15:00 uses Friday July 17
assert manager.next_order_id(now="2026-07-17T13:34:01+08:00").startswith("ORD-20260717")
# Same instance, post-15:00 uses Monday July 20
assert manager.next_order_id(now="2026-07-17T16:16:00+08:00").startswith("ORD-20260720")
```
