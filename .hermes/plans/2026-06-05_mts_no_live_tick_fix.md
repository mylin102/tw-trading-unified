# Implementation Plan: Fix MTS REJECTED: NO_LIVE_TICK

## Goal

Fix `REJECTED: NO_LIVE_TICK` when MTS manual trade flag is consumed but `market_data[self.ticker]` has no live tick.

## Root Cause (unchanged)

Two bugs:
1. **Flag deleted before validation** — fixed in Step 2 (rename → process → delete protocol)
2. **NO_LIVE_TICK blocks all modes equally** — paper mode connects to Shioaji and receives ticks, but `_process_manual_trade_flag()` rejects before ticks arrive

## Implemented (Step 1-2)

- Contract tests: Group A (retry, schema, idempotency, TTL) — all passing
- Atomic flag lifecycle: `rename → process → delete` with crash recovery
- Schema validation (C6), TTL check (C5), Idempotency (C2), Active order guard (C2), MAX_RETRIES (C7)

## Step 3: Fix price resolution — LIVE_TICK for all modes, fallback only for dry_run

### Problem with Step 3 v1 (reverted)

Added `if not self.live_trading:` branch with 5-tier fallback chain that bypassed NO_LIVE_TICK. Wrong approach — paper mode should get real ticks, not fallback prices.

### Correct approach

Paper mode (`live_trading=False, dry_run=False`) runs via `run_system(dry_run=False)` which already connects Shioaji and subscribes contracts. Ticks arrive via `on_tick()` → `market_data` is populated → LIVE_TICK check passes. No special path needed.

The only mode that genuinely has no ticks: **dry_run** (unit tests, no Shioaji connection).

### Changes in `strategies/futures/monitor.py`

#### 3a: Restore LIVE_TICK as single shared path

Revert the Step 3 v1 branching (`if not self.live_trading:` / `else:` / `_live_tick_resolved`). All modes share the same LIVE_TICK check, with dry_run getting fallback:

```python
# 2026-06-05 Hermes Agent: Step 3 — LIVE_TICK for all modes with Shioaji.
# Fallback chain (_resolve_entry_price) is dry_run-only (no Shioaji connection).
# Paper mode gets real ticks from Shioaji (run_system dry_run=False).
if self.dry_run:
    # No Shioaji → no ticks → fallback chain
    _price, _price_source = self._resolve_entry_price(_flag)
    if _price is None:
        self._manual_trade_status = "REJECTED: NO_PRICE_SOURCE"
        console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: All price tiers exhausted (Source={_price_source})[/red]")
        self._flag_retry_count += 1
        return True
else:
    # Live or paper: Shioaji connected, ticks arrive via on_tick()
    _live_tick = self.market_data.get(self.ticker, {})
    _price_raw = _live_tick.get("close")
    _arrival_at = _live_tick.get("local_arrival_at")
    
    if _price_raw and _price_raw > 0 and _arrival_at:
        _tick_age_ms = (time.time() - _arrival_at) * 1000
        if _tick_age_ms <= _MAX_ENTRY_AGE_MS:
            _price = float(_price_raw)
            _price_source = "LIVE_TICK"
        else:
            self._manual_trade_status = f"REJECTED: STALE_TICK ({int(_tick_age_ms)}ms)"
            console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: Latest tick is stale ({int(_tick_age_ms)}ms > {_MAX_ENTRY_AGE_MS}ms)[/red]")
            self._append_mts_event("REJECTED_ENTRY", reason="STALE_TICK",
                                  near_age_ms=int(_tick_age_ms),
                                  far_age_ms=-1, max_allowed_age_ms=_MAX_ENTRY_AGE_MS,
                                  ticker=self.ticker)
            self._flag_retry_count += 1
            return True

# Shared check: if price not resolved by either path, reject
if _price_source != "LIVE_TICK":
    self._manual_trade_status = "REJECTED: NO_LIVE_TICK"
    console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: No fresh LIVE_TICK available (Source={_price_source})[/red]")
    self._flag_retry_count += 1
    console.print(f"[dim]🔄 [MANUAL_TRADE] Retry {self._flag_retry_count}/10 (NO_LIVE_TICK)[/dim]")
    return True
```

Compare to Step 3 v1 (reverted): the removed lines were `4198-4214` branching — `if not self.live_trading:` → fallback, `else:` → LIVE_TICK, `_live_tick_resolved` flag for NO_LIVE_TICK skip. The new structure replaces that with a single `if self.dry_run:` guard at the top, followed by the LIVE_TICK logic for all non-dry modes.

#### 3b: Keep `_resolve_entry_price()` but guard with `self.dry_run`

The method stays, but its scope is explicitly dry_run-only:

```python
def _resolve_entry_price(self, _flag: dict) -> tuple:
    """5-tier price fallback chain for dry_run mode only (no Shioaji).
    
    Paper and live modes receive real ticks via Shioaji — this is NOT called.
    
    Returns (price: float | None, source_label: str).
    """
    # Tier 1-5: same as before
```

#### 3c: Code attribution

```python
# 2026-06-05 Hermes Agent: Step 3 — LIVE_TICK for all modes with Shioaji.
# Fallback chain (_resolve_entry_price) is dry_run-only.
# Paper mode gets real ticks from Shioaji connection (run_system dry_run=False).
```

### Changes in `tests/contracts/test_mts_no_live_tick_retry.py`

Group B tests need updating to reflect new design:

| Test | New expected behavior |
|------|----------------------|
| `test_paper_mode_uses_bar_fallback` | Delete or rename. Paper mode no longer uses fallback — it gets real ticks. |
| `test_all_tiers_fail_returns_no_price_source` | Keep. Change `live_trading=False` → set `dry_run=True` so it goes through fallback chain. |

Group A `test_flag_survives_no_live_tick_retry`: currently asserts NO_LIVE_TICK. After Step 3, the test's `_make_minimal_monitor()` sets `dry_run=True`. In dry_run, `_resolve_entry_price()` is called. If flag has `near_close`, Tier 4 resolves → no NO_LIVE_TICK. **Need to test with flag that has no `near_close`** to force NO_LIVE_TICK through fallback chain.

## Dependencies

- Step 2 must be complete (atomic flag lifecycle + tests passing)
- No config changes needed
- No new Shioaji connection logic needed (already works)

## Files Changed

| File | Change |
|------|--------|
| `strategies/futures/monitor.py` | ~20 lines: restore shared LIVE_TICK path, guard `_resolve_entry_price()` with `self.dry_run` |
| `tests/contracts/test_mts_no_live_tick_retry.py` | ~10 lines: update Group B tests + Group A NO_LIVE_TICK test for dry_run |

## Verification

```bash
# Step 3 tests
python3 -m pytest tests/contracts/test_mts_no_live_tick_retry.py -v

# Regression
python3 -m pytest tests/strategies/test_squeeze_fire_scout.py -q
python3 -c "import py_compile; py_compile.compile('strategies/futures/monitor.py', doraise=True)"
```
