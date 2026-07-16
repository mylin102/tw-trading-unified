# ADR-004: Test Leakage Prevention and Order Export Isolation

## Status
Accepted

## Date
2026-07-08

## Context

During test execution, several tests instantiate `FuturesMonitor` or `OptionsMonitor` without isolating the current working directory (e.g., using `monkeypatch.chdir(tmp_path)`).
When these tests simulate order actions (such as `MTS_EXIT` signals or `_execute_trade`), the order manager writes pending or completed orders to the relative path `exports/trades/`.
Because the tests are run from the project root directory `/Users/mylin/Documents/mylin102/tw-trading-unified`, these relative writes write directly into the production `exports/trades/` folder (producing files like `TMF_20260709_orders.json`).
Consequently, mock exit orders with status `pending_submit` (e.g., `ORD-20260709-000003` and `ORD-20260709-000004`) leak into the active session JSON and show up on the dashboard as "ghost orders" in the Order Lifecycle table, causing confusion and UI noise.

## Decision

We have implemented automatic test detection and directory isolation in both monitors:
- [strategies/futures/monitor.py](file:///Users/mylin/Documents/mylin102/tw-trading-unified/strategies/futures/monitor.py)
- [strategies/options/live_options_squeeze_monitor.py](file:///Users/mylin/Documents/mylin102/tw-trading-unified/strategies/options/live_options_squeeze_monitor.py)

Before exporting orders to the JSON file, the system checks if a test framework is executing (`pytest` in `sys.modules` or `PYTEST_CURRENT_TEST` in environment variables) AND if the current working directory is the project root containing `RULES.md` and `exports/`.
If both conditions are met, the orders export directory is redirected to a temporary test directory `tests/temp_exports_trades/` instead of the production `exports/trades/` directory.

### Isolation Logic
```python
import sys
import os
from pathlib import Path

orders_dir = "exports/trades"
if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
    current_cwd = Path.cwd().resolve()
    if (current_cwd / "RULES.md").exists() and (current_cwd / "exports").exists():
        orders_dir = "tests/temp_exports_trades"
```

This ensures:
1. **Production Safety:** Production runs (where `pytest` is not loaded) will always write to the standard `exports/trades/` path.
2. **Backward Compatibility:** Tests that explicitly isolate themselves via `monkeypatch.chdir(tmp_path)` will not match the workspace root condition and will continue using relative `exports/trades/` under their temporary folder as expected by assertions.
3. **No Leakage:** Non-isolated test runs will write to `tests/temp_exports_trades/` (which is excluded from production/dashboard tracking) instead of polluting the production directory.
4. **State File Isolation:** The MTS position state file path falls back to `/tmp/test_mts_position_state.json` instead of the production `/tmp/mts_position_state.json` when `pytest` is executing. This prevents test side effects (e.g. mock trades closing mock positions) from setting `has_position=false` in production, which previously caused `[MTS_SPLIT_BRAIN]` freezes in the live system.

## Consequences

- "Ghost orders" will no longer appear on the dashboard after running `pytest` tests from the project root.
- The `tests/temp_exports_trades` folder will be created during test runs. It has been added to `.gitignore`.
- Production state files are 100% isolated and cannot be overwritten by test runs, preventing live/paper monitor freezes.
- Existing tests pass with no modifications required, and contract tests verifying path fallback mechanisms remain fully compatible.
