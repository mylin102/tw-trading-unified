This is a Taiwan futures + options trading system. Bugs cause real financial loss.

Read RULES.md before any code change. Run `python3 -m pytest tests/ -v` before and after.

Key rules:
- Side effects (CSV/log) AFTER validation, never before
- PaperTrader.position = single source of truth
- PnL must include fees+tax
- Stop loss >= 10 pts
- Paper capital limit 40,000 TWD
- Don't use `from datetime import datetime` with `datetime.timedelta`
- Strategy plugins return {"action","reason","stop_loss"} or None
