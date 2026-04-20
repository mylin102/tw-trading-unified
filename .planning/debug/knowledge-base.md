# GSD Debug Knowledge Base

Resolved debug sessions. Used by `gsd-debugger` to surface known-pattern hypotheses at the start of new investigations.

---

## autostart-dashboard-crash — Autostart dashboard startup crashed from readiness tuple misuse, missing options fee init, and incorrect autostart status handling
- **Date:** 2026-04-20
- **Error patterns:** AttributeError, bool object has no attribute passed, ui/dashboard.py, Strategy loop error, ShioajiOptionsSmartMonitor, exchange_fee_per_side, autostart.sh, 核心退出, code=0, MM undefined, broken startup behavior
- **Root cause:** Dashboard readiness UI iterated the `(is_ready, results_dict)` tuple from `check_all()` as if each item were a result object; options exit-PnL code referenced `self.exchange_fee_per_side` without initializing it; autostart captured `tee`'s exit code instead of `main.py`'s and used `$MM` in maintenance checks without defining it.
- **Fix:** Applying minimal targeted changes to normalize readiness details for the dashboard, initialize the missing options exchange-fee attribute from config, and fix autostart exit-code/minute handling with regression tests.
- **Files changed:** core/live_readiness.py, ui/dashboard.py, strategies/options/live_options_squeeze_monitor.py, autostart.sh, tests/test_autostart_dashboard_crash.py
---
