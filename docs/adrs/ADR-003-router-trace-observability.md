# ADR-003: Router Trace — Per-Bar Decision Observability

**Status:** Accepted (2026-04-30)  
**Scope:** Strategy router, all strategy plugins, dashboard

## Context

Before this ADR, diagnosing why the system didn't trade required grepping thousands of lines of PM2 logs. Each strategy had its own logging pattern. There was no structured record of "what was evaluated and why it was rejected."

## Decision

Every bar produces a structured decision record via three layers:

1. **StrategyEval** — Each strategy's `on_bar()` returns a dataclass with `triggered`, `skip_reason`, `edge_score`, and `notes`
2. **RouterTrace** — The router collects all evals and writes to `logs/router_trace/router_trace_YYYYMMDD.jsonl`
3. **Dashboard** — Pipeline tab renders the trace as status cards + edge timeline + skip reason barchart

## Implementation

- `core/strategy_eval.py` — StrategyEval and RouterTrace dataclasses
- `core/strategy_base.py` — `_set_eval()` helper, `last_eval` attribute
- `core/futures_strategy_router.py` — collects evals, writes trace on TRADE/NO_WINNER
- All 5 strategy plugins — `_set_eval()` at every return path
- `ui/dashboard.py` — Pipeline tab: latest status, edge timeline, skip reason distribution

## Consequences

- Every bar, even no-trade bars, has a JSONL record
- Each strategy's skip reason is visible per bar (not rate-limited)
- Dashboard answers "why no trade" directly
- Stdout shows one summary line per bar (not per-tick logging)
- Two existing tests needed minor adjustments for the new eval collection path

## Related

- `docs/architecture/strategy_router.md` — skip reason tables for all strategies
- `docs/operations/no_trade_diagnosis.md` — debug workflow
- `core/strategy_eval.py` — implementation
