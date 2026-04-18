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

<!-- GSD Configuration — managed by get-shit-done installer -->
# Instructions for GSD

- Use the get-shit-done skill when the user asks for GSD or uses a `gsd-*` command.
- Treat `/gsd-...` or `gsd-...` as command invocations and load the matching file from `.github/skills/gsd-*`.
- When a command says to spawn a subagent, prefer a matching custom agent from `.github/agents`.
- Do not apply GSD workflows unless the user explicitly asks for them.
- After completing any `gsd-*` command (or any deliverable it triggers: feature, bug fix, tests, docs, etc.), ALWAYS: (1) offer the user the next step by prompting via `ask_user`; repeat this feedback loop until the user explicitly indicates they are done.
<!-- /GSD Configuration -->
