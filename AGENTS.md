You are working on a Taiwan futures + options trading system (Shioaji broker). Bugs cause real financial loss.

Read RULES.md in project root before any code change.
Run `python3 -m pytest tests/ -v` before and after every change.

# Project Context

## Before Any Change

You MUST output:
  FIX: <one sentence describing exactly what bug or behavior is being changed>
  SCOPE: <list of files to be modified>
  VERIFY: <how to confirm the fix works>

You MAY NOT add extra fixes, refactors, or improvements to the same change.
If you discover additional issues during a fix: document them, do NOT fix them. Tell the user.
If the fix requires 3+ modified files: stop and ask.

## Key rules
- Side effects (CSV/log write) ONLY after operation succeeds, never before
- PaperTrader.position is the single source of truth for position state
- All PnL must include broker fees + exchange fees + tax
- Stop loss offset >= 10 pts (round-trip cost ~8 pts for TMF)
- Paper mode capital limit: 40,000 TWD — block entries exceeding margin
- Never use `from datetime import datetime` in files that also need `datetime.timedelta`
- Strategy plugins must return {"action", "reason", "stop_loss"} or None
- Every entry checks: position==0, margin sufficient, price>0, not same bar
- Every exit: zero position BEFORE logging, pass explicit quantity

## SQUEEZE_FIRE_SCOUT Regression Contract

Before changing any of:
- squeeze_fire_scout plugin (`strategies/plugins/futures/squeeze_fire_scout.py`)
- futures_strategy_router (`core/futures_strategy_router.py`)
- monitor regime override (`strategies/futures/monitor.py` — bar_regime patch)
- scout time_stop or size_multiplier logic

Run:

```bash
pytest tests/strategies/test_squeeze_fire_scout.py -q
```

Expected result: `21 passed`

# Role & Environment Context
- The user is working on a high-performance quantitative backtesting system.
- The local environment is a Mac with Apple Silicon (M4 chip) running macOS.

# Rule: macOS Resource and Temperature Management
Whenever generating Python code, execution steps, or terminal commands for running computational heavy tasks (e.g., backtesting, optimizations, multiprocessing), you MUST follow these guidelines to prevent overheating and force execution on Efficiency Cores (E-Cores).

## 1. Terminal Execution Rule
- DO NOT suggest raw `python <script>.py` commands or `cpulimit` for running scripts.
- ALWAYS wrap execution commands with `taskpolicy -c background` to restrict the processes to E-Cores.
- Example pattern to provide:
  ```bash
  taskpolicy -c background python your_script.py
  ```

## 2. Python Script Boilerplate Rule
- When creating or modifying main Python scripts (`if __name__ == '__main__':`) that utilize `multiprocessing`, `concurrent.futures`, or heavy loops, you MUST inject the following macOS-specific self-throttling snippet at the entry point:
  ```python
  import sys
  import os

  if __name__ == '__main__':
      # macOS Silicon optimization: Force main and spawned sub-processes to E-Cores
      if sys.platform == "darwin":
          os.system(f"taskpolicy -b -p {os.getpid()}")
  ```

## 3. Explanations and Communication
- Do not mention `cpulimit` or typical Linux-based throttling utilities, as they are inefficient on Apple Silicon schedulers.
- Remind the user that this policy applies to all spawned child processes automatically, maintaining a cool device with zero fan noise.


## P0: PM2 and Dashboard Configuration Safety (2026-08-05)
- Dashboard, YAML updates, watchlist sync, and UI mode changes MUST NOT create `.restart`, invoke `pm2`, or cause `trading-system` to exit.
- Never use `pm2 restart all`, `pm2 reload`, or a dashboard-triggered restart for trading configuration. A dashboard-only code update may target `dashboard` only.
- A trading-system stop/start/restart is a maintenance deployment: require explicit user approval, target preflight, broker position + working-order reconciliation, and `VERIFIED_FLAT`. Ledger or stale state files alone do not prove flat.
- Start trading-system only from the canonical `ecosystem.config.js`; `ecosystem.config.cjs` is compatibility-only. Do not use ad-hoc `pm2 start main.py` commands.
- After a deployment, verify exactly one trading process, the intended commit/cwd/interpreter/args, near/far Tick+BidAsk subscriptions, and 120 seconds of stable observe-only operation before `pm2 save`. Confirm `dump.pm2` contains `trading-system` and uses the same `PM2_HOME` as launchd.
- If broker health is unknown while a position may be open: block new entries, preserve strategy state, alarm, and reconcile. Do not autonomously stop/restart the process.
