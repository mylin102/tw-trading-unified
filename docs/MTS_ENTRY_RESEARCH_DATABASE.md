# MTS Entry Research Database

`core/entry_research_store.py` is a shadow, research-only SQLite store for
entry observations. It is not an order gate and is never consulted by the
gateway, watchdog, or strategy authorization path.

## Runtime contract

- Default path: `TRADING_RUNTIME_DIR/exports/research/mts_entry_research.sqlite3`.
- `MTS_ENTRY_RESEARCH_DB` may override the path in tests or an isolated
  research run.
- Each row carries mode, session, config/release identity, run ID, source,
  contracts, spread/z/slope/velocity features, executable BBO, quote age and
  skew, ATR/regime, estimated costs/net edge, decision and rejection reason.
- Writes use WAL and `INSERT OR IGNORE` with a deterministic event ID. A
  locked, unavailable, or malformed database returns `False` and is ignored by
  the trading path; it cannot reject, delay by retry, or change an order.
- Paper and live rows remain mode/provenance scoped. No historical row is
  backfilled with invented fills, PnL, BBO, or timestamps.

## Evidence and replay use

The store records what was known at the candidate decision time. It is the
input for offline A/B/C/D comparisons (z-only, causal reversal confirmation,
cost-only, and combined policy). A row is eligible for replay comparison only
when its required causal features and executable quotes are present; missing
fields remain `NULL` and must be reported as unavailable rather than inferred
from post-entry data.

This database does not change `live_trading`, entry thresholds, release rules,
or any PM2 configuration. Deploy/restart is a separate operator action.
