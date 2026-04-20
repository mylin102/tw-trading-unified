---
status: resolved
trigger: "Investigate issue: autostart-dashboard-crash\n\n**Summary:** Running `autostart.sh` should start `main.py` and the dashboards cleanly, but it crashes / starts in a degraded state. Find and fix the root causes."
created: 2026-04-20T00:00:00Z
updated: 2026-04-20T00:51:00Z
---

## Current Focus

hypothesis: the issue is resolved end-to-end; remaining work is archival and documentation only
test: commit the targeted code/docs changes and append a reusable knowledge-base record
expecting: clean archival trail plus a knowledge-base entry for future matching
next_action: update knowledge base and create the final commits

## Symptoms

expected: `autostart.sh` should launch `main.py` and dashboard services successfully; the dashboard should render without exceptions.
actual: Running `autostart.sh` leads to crashes / broken startup behavior.
errors: User reported `AttributeError: 'bool' object has no attribute 'passed'` from `ui/dashboard.py` line 2272. During prior investigation, these additional runtime symptoms were observed: `Strategy loop error: 'ShioajiOptionsSmartMonitor' object has no attribute 'exchange_fee_per_side'`; `autostart.sh` logs repeated `核心退出 (code=0)` even though the core process behavior looked suspicious; `autostart.sh` references `$MM` without defining it.
reproduction: Run `bash autostart.sh` from repo root.
started: Started today; user says it happens consistently.

## Eliminated

## Evidence

- timestamp: 2026-04-20T00:05:00Z
  checked: .planning/debug/knowledge-base.md
  found: Knowledge base file does not exist
  implication: No prior known-pattern match is available; investigate from first principles

- timestamp: 2026-04-20T00:06:00Z
  checked: RULES.md
  found: Project requires running `python3 -m pytest tests/ -v` before and after changes; live trading rules emphasize minimal targeted fixes and preserving cost-inclusive PnL/state invariants
  implication: Must establish baseline tests before code edits and avoid unrelated modifications

- timestamp: 2026-04-20T00:07:00Z
  checked: core/live_readiness.py and ui/dashboard.py readiness section
  found: `check_all()` returns `(is_ready, results_dict)`, while `ui/dashboard.py` assigns `results = check_all()` and then iterates `for r in results`, treating each item as an object with `.passed/.name/.value/.detail`
  implication: This directly explains the reported `AttributeError: 'bool' object has no attribute 'passed'`

- timestamp: 2026-04-20T00:08:00Z
  checked: autostart.sh
  found: Core loop runs `main.py | tee -a unified.log` then captures `$?`, which is the pipeline status from `tee`; health-check maintenance logic also uses `$MM` without assigning it
  implication: Autostart can misreport main.py exit status and execute broken minute-based maintenance conditions

- timestamp: 2026-04-20T00:16:00Z
  checked: symbol search in strategies/options/live_options_squeeze_monitor.py
  found: Class initialization defines `broker_fee_per_side` and `tax_rate`, but later exit-PnL code references `self.exchange_fee_per_side` at line ~1103 without any matching initialization in the file
  implication: Strategy loop can crash at runtime exactly as reported when that code path executes

- timestamp: 2026-04-20T00:17:00Z
  checked: baseline `python3 -m pytest tests/ -v`
  found: Test suite passed with `385 passed, 1 skipped` in 43.98s before any code changes
  implication: Repository has a clean baseline before targeted changes, reducing risk that existing failures are unrelated

- timestamp: 2026-04-20T00:22:00Z
  checked: strategies/options/live_options_squeeze_monitor.py fee block
  found: Exit-PnL logic at lines ~1100-1107 deducts broker fee, exchange fee, and tax; only `exchange_fee_per_side` is missing from initialization, while other cost fields exist
  implication: A minimal fix can initialize the missing attribute without changing unrelated position or PnL mechanics

- timestamp: 2026-04-20T00:40:00Z
  checked: focused validation
  found: `bash -n autostart.sh`, `python3 -m py_compile ...`, and `python3 -m pytest tests/test_autostart_dashboard_crash.py -v` all passed; focused regression tests covered readiness normalization, dashboard helper usage, autostart PIPESTATUS/MM handling, and options fee initialization
  implication: The targeted fixes are syntactically valid and directly guarded by regression coverage

- timestamp: 2026-04-20T00:46:00Z
  checked: full `python3 -m pytest tests/ -v`
  found: Full suite passed after changes with `389 passed, 1 skipped`
  implication: No test-detected regressions were introduced by the fixes

## Resolution

root_cause: Dashboard readiness UI iterated the `(is_ready, results_dict)` tuple from `check_all()` as if each item were a result object; options exit-PnL code referenced `self.exchange_fee_per_side` without initializing it; autostart captured `tee`'s exit code instead of `main.py`'s and used `$MM` in maintenance checks without defining it.
fix: Applying minimal targeted changes to normalize readiness details for the dashboard, initialize the missing options exchange-fee attribute from config, and fix autostart exit-code/minute handling with regression tests.
verification: Focused checks passed (`bash -n autostart.sh`, `py_compile`, 4 targeted regression tests) and full suite passed (`python3 -m pytest tests/ -v` → `389 passed, 1 skipped`). User then confirmed the real `bash autostart.sh` workflow is fixed end-to-end in their environment.
files_changed: [core/live_readiness.py, ui/dashboard.py, strategies/options/live_options_squeeze_monitor.py, autostart.sh, tests/test_autostart_dashboard_crash.py]
