# Broker-truth query policy

## Authority

For live MTS, the broker is the authority for positions, fills, and open
orders. Local JSONL, order exports, callbacks, and cached canonical artifacts
are projections and audit evidence only.

## Query timing

- Startup performs one read-only broker reconciliation.
- Immediately before a live entry, release, or exit submission, the process
  performs a fresh read-only broker query and uses that result for the order
  decision.
- An explicit dashboard refresh may request a fresh read-only query.

## Snapshot rule

Periodic renewal and expiry of a cached snapshot are not an order gate. An old
snapshot may make the dashboard show `N/A` or `stale`, but it must not by itself
block a strategy decision when a fresh pre-submit query succeeds. Conversely,
a failed pre-submit query is fail-closed for that one submission: no order,
no retry, and no inference from local state.

## Reconciliation invariants

- A broker position covering a local pending/timeout receipt can promote the
  local row to `FILLED` only on a strict one-to-one code/side/quantity match.
- Ambiguous or incomplete matches stay pending and never create a synthetic
  broker order identity.
- Once broker truth proves `FILLED`, an older `PendingSubmit`/`Submitted`
  receipt cannot regress the local row.
- Paper mode continues to use its paper ledger and does not query the broker.

