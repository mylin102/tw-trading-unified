# tw-trading-unified Development Rules

## Meta Rules — Apply to Every Task (Unless Explicitly Overridden)

These rules govern all work in this project. Bias: caution over speed on non-trivial work. Use judgment on trivial tasks.

### Rule 1 — Think Before Coding
State assumptions explicitly. If uncertain, ask rather than guess.
Present multiple interpretations when ambiguity exists.
Push back when a simpler approach exists.
Stop when confused. Name what's unclear.

### Rule 2 — Simplicity First
Minimum code that solves the problem. Nothing speculative.
No features beyond what was asked. No abstractions for single-use code.
Test: would a senior engineer say this is overcomplicated? If yes, simplify.

### Rule 3 — Surgical Changes
Touch only what you must. Clean up only your own mess.
Don't "improve" adjacent code, comments, or formatting.
Don't refactor what isn't broken. Match existing style.

### Rule 4 — Goal-Driven Execution
Define success criteria. Loop until verified.
Don't follow steps. Define success and iterate.
Strong success criteria let you loop independently.

### Rule 5 — One Fix Per Change (New 2026-05-20)
Each patch must fix EXACTLY ONE thing.
- Before any change: state FIX, SCOPE, and VERIFY.
- If you discover a second bug during a fix: document it, do NOT fix it. Tell the user.
- If the fix requires changing 3+ files: stop and ask.
- After the change: verify ONLY the stated fix.
- You MAY NOT add extra fixes, refactors, improvements, or style changes to the same change.

### Rule 7 — Surface Conflicts, Don't Average Them
If two patterns contradict, pick one (more recent / more tested).
Explain why. Flag the other for cleanup.
Don't blend conflicting patterns.

### Rule 8 — Read Before You Write
Before adding code, read exports, immediate callers, shared utilities.
"Looks orthogonal" is dangerous. If unsure why code is structured a way, ask.

### Rule 9 — Tests Verify Intent, Not Just Behavior
Tests must encode WHY behavior matters, not just WHAT it does.
A test that can't fail when business logic changes is wrong.

### Rule 10 — Checkpoint After Every Significant Step
Summarize what was done, what's verified, what's left.
Don't continue from a state you can't describe back.
If you lose track, stop and restate.

### Rule 11 — Match the Codebase's Conventions, Even If You Disagree
Conformance > taste inside the codebase.
If you genuinely think a convention is harmful, surface it. Don't fork silently.

### Rule 12 — Fail Loud
"Completed" is wrong if anything was skipped silently.
"Tests pass" is wrong if any were skipped.
Default to surfacing uncertainty, not hiding it.

### Rule 13 — Code Attribution (2026-05-22)
EVERY code modification MUST include a comment with the timestamp (ISO 8601 or YYYY-MM-DD) and the author ("Hermes Agent"). Applies to all patch/write_file operations. The timestamp in each comment MUST be the actual date the modification is made — do not copy the rule's creation date or any example date.
Example (correct, written on 2026-05-22): `# 2026-05-22 Hermes Agent: fix feed health pollution from TMF_VIRTUAL`

---

## CRITICAL: Read Before Any Code Change

This is a live trading system (currently PAPER mode). Bugs cause real financial loss.
Run `python3 -m pytest tests/ -v` before AND after every change.

---

## Rule 1: Never Write Before Validate

```
❌ save_trade() → execute_signal()
✅ execute_signal() → if success → save_trade()
```

Side effects (CSV write, log, notification) MUST happen AFTER the core operation succeeds.
If `execute_signal()` returns None, write NOTHING.

## Rule 2: Single Source of Truth for Position

- `PaperTrader.position` is the ONLY truth for futures position
- `ShioajiOptionsSmartMonitor.position` is the ONLY truth for options position
- Ledger CSV is a LOG, not a state store
- On restart, recover from API (live) or ledger (paper), then trust in-memory state

## Rule 3: Guard Every Entry and Exit

```python
# Entry: check BEFORE doing anything
def enter():
    if position != 0: return        # already in position
    if not margin_check(): return   # insufficient funds
    if price <= 0: return           # invalid price
    if same_bar: return             # already traded this bar

# Exit Path (Atomic Sequence):
# 1. Reentrancy Guard: prevent double-processing during IO
# 2. Freeze Snapshot: capture qty, entry_price, spread_data
# 3. Create Order Intent: call _record_paper_order or similar
#    - If FAILED (None): return early, KEEP position for retry on next tick
# 4. Update SSOT: zero or decrement position immediately AFTER order accepted
# 5. Side Effects: log_trade, notifications AFTER state change
def exit():
    if getattr(self, "_exit_in_progress", False): return
    self._exit_in_progress = True
    try:
        qty = self.position
        if qty == 0: return
        
        # 1. Capture snapshot
        snapshot = ...
        
        # 2. Establish order intent
        order = self._record_paper_order(...)
        if order is None: return # Keep position, retry next tick
        
        # 3. Update SSOT immediately
        self.position = 0 
        
        # 4. IO / Logging
        log_trade(qty=qty, snapshot=snapshot, ...)
    finally:
        self._exit_in_progress = False
```

## Rule 4: PnL Must Include All Costs

```python
pnl_cash = gross_pnl - broker_fee - exchange_fee - tax - slippage
# NEVER show gross PnL to user without deducting costs
# TMF: ~8 pts round-trip cost per lot
# TXO: varies by premium
```

## Rule 5: Stop Loss Must Cover Costs

- Break-even offset >= 10 pts (TMF round-trip cost ~8 pts)
- ATR multiplier >= 1.5x (minimum)
- Never use stop_loss_level as exit_price; use actual market price

## Rule 6: Paper Mode Capital Limits

- `initial_capital: 100000` in options config
- Buyer: need `premium × 50 × lots` available
- Seller spread: need `wing_width × 50 × lots` available
- Reserve 20% always untouched
- Check BEFORE entry, block if insufficient

## Rule 7: No `from datetime import datetime` in Files Using `datetime.timedelta`

```python
# ❌ This breaks datetime.timedelta:
from datetime import datetime
now = datetime.now()
yesterday = now - datetime.timedelta(days=1)  # AttributeError!

# ✅ Option A: import module
import datetime
now = datetime.datetime.now()
yesterday = now - datetime.timedelta(days=1)

# ✅ Option B: import both explicitly
from datetime import datetime, timedelta
yesterday = datetime.now() - timedelta(days=1)
```

## Rule 8: Strategy Plugin Contract

Every strategy function MUST:
- Accept `(state: dict, cfg: dict)` → return `dict | None`
- Return dict must have: `{"action": "BUY"|"SELL", "reason": str, "stop_loss": float}`
- `stop_loss` must be > 0
- Return `None` for no signal (never return empty dict)

## Rule 9: Config Changes Don't Require Restart

- `active_strategy` is read every tick cycle
- Dashboard writes YAML → monitor reads next cycle
- Exception: Shioaji connection changes need full restart

## Rule 10: Test Before Deploy

```bash
# Mandatory before any deployment:
python3 -m pytest tests/ -v          # All tests pass
python3 -c "import py_compile; ..."  # No syntax errors
python3 main.py --dry-run            # Starts without crash
```

## Rule 11: Zero Hardcoding Policy

- NEVER hardcode product tickers (e.g., "TMF", "MXF", "TXO") in core logic, function defaults, or file patterns.
- Always load current product from `config/futures.yaml` (ticker field) or via `StrategyContext`.
- If a ticker is needed for a default argument, use `None` and resolve it at runtime from configuration.
- Rationale: High maintenance cost and high risk of using wrong instrument data when switching markets.

---

## Architecture Quick Reference

```
main.py                    → startup, tick dispatch, health check
strategies/futures/
  monitor.py               → FuturesMonitor (indicator calc + strategy dispatch)
  entry_strategies.py      → pluggable entry strategies (8 strategies)
  squeeze_futures/engine/
    simulator.py            → PaperTrader (position state, PnL calc)
    vectorized.py           → backtest engine
strategies/options/
  monitor.py               → OptionsMonitor wrapper
  live_options_squeeze_monitor.py → main options engine
  theta_gang.py            → ThetaGang sell-premium strategies
  options_engine/engine/
    greeks.py               → BS pricing (py_vollib)
    greeks_ql.py            → QuantLib pricing
config/
  futures.yaml              → futures params + active_strategy
  options_strategy.yaml     → options params + theta_gang config
  risk_global.yaml          → capital allocation
```

## Known Gotchas

1. `data_storage.py` uses full-rewrite CSV (not append) — clearing CSV without clearing JSON causes ghost entries
2. Shioaji C++ backend crashes on rapid login/logout — wait 15s between restarts
3. Night session (15:00~05:00) dates: use previous calendar day before 05:00
4. `api.kbars()` has 5-min rate limit — tick bars are primary data source
5. Options `position` field is overwritten (not accumulated) on entry — guard prevents double entry
