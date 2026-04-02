# Project Context — tw-trading-unified

## CRITICAL: Read at Every Session Start

Before responding to ANY user request, you MUST read:
- `/Users/mylin/Documents/mylin102/tw-trading-unified/RULES.md`

This is a live Taiwan futures + options trading system (PAPER mode). Bugs cause real financial loss.

## Key Rules Summary

1. **Side effects AFTER success** — CSV/log writes only after operation succeeds
2. **Single source of truth** — `PaperTrader.position` for futures, `ShioajiOptionsSmartMonitor.position` for options
3. **Guard every entry/exit** — Check position, margin, price, same-bar before entry; zero position before logging on exit
4. **PnL includes all costs** — broker fee, exchange fee, tax, slippage (~8 pts round-trip for TMF)
5. **Stop loss >= 10 pts** — Must cover round-trip costs
6. **Paper mode capital limit: 40,000 TWD** — Block entries exceeding margin
7. **No `from datetime import datetime`** in files using `datetime.timedelta`
8. **Strategy plugin contract** — Return `{"action", "reason", "stop_loss"}` or `None`
9. **Config changes don't require restart** — `active_strategy` read every tick cycle
10. **Test before deploy** — Run `python3 -m pytest tests/ -v` before and after every change

## Always Verify

Run `python3 -m pytest tests/ -v` before and after every code change.
