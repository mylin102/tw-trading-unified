You are working on a Taiwan futures + options trading system (Shioaji broker). Bugs cause real financial loss.

Read RULES.md in project root before any code change.
Run `python3 -m pytest tests/ -v` before and after every change.

Key rules:
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
