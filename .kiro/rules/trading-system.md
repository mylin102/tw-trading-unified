You are working on a Taiwan futures + options trading system (Shioaji broker).
This is a LIVE-CAPABLE system. Bugs cause real financial loss.

BEFORE making any code change:
1. Read RULES.md in project root — it contains 10 mandatory rules from past incidents
2. Read docs/SDD.md — architecture and interface contracts
3. Run `python3 -m pytest tests/ -v` before AND after changes

CRITICAL RULES (from RULES.md):
- Never write logs/CSV before validating the operation succeeded
- PaperTrader.position is the single source of truth — never maintain position state elsewhere
- Every entry must check: position==0, margin sufficient, price>0, not same bar
- Every exit must: zero position BEFORE logging, pass explicit quantity
- PnL must always include broker fees + tax (never show gross PnL)
- Stop loss offset must be >= 10 pts to cover round-trip costs (~8 pts for TMF)
- Paper mode enforces 40,000 capital limit — block entries exceeding available margin
- Never use `from datetime import datetime` in files that also need `datetime.timedelta`
- All strategy plugins must return `{"action", "reason", "stop_loss"}` or None

TEST COVERAGE:
- tests/test_trading_bugs.py — 18 tests covering all known bugs
- Every past bug has a corresponding test case
- If you introduce a new bug pattern, add a test for it

ARCHITECTURE:
- main.py → tick dispatch only
- strategies/futures/monitor.py → indicator calc + strategy dispatch
- strategies/futures/entry_strategies.py → 8 pluggable strategies
- strategies/options/live_options_squeeze_monitor.py → options engine
- strategies/options/theta_gang.py → sell-premium strategies
- config/*.yaml → all parameters, hot-reloadable
