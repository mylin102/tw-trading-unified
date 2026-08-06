# MTS Position Authority

## Purpose

This contract prevents a paper-trading spread becoming unmanaged when runtime
memory, dashboard state, fills, and pending orders disagree. A dashboard field
or manual flag is never proof of a filled position.

## Sources of truth

| Mode | Exposure fact | Transition intent | Rule |
| --- | --- | --- | --- |
| Paper | Local confirmed-fill JSONL | Local pending MTS orders | Reconstruct only from complete per-leg fills. |
| Live | Fresh broker position snapshot | Broker/local open orders | Broker fact wins; ledger disagreement is `UNKNOWN`. |

Submitted is not filled: orders are transition intent, not exposure.

## Authority states

| State | Meaning | Automatic behaviour |
| --- | --- | --- |
| `FLAT` | No net exposure and no transition | A stale in-memory position may reset. |
| `OPEN` | Current trade has verified signed per-leg quantities | Runtime may reconstruct from fill facts. |
| `TRANSITIONING` | Legal pending entry, release, or exit | Stop normal `on_bar()` decisions and duplicate submission. |
| `UNKNOWN` | Invalid/unreadable ledger fact or missing live broker proof | Fail closed: no entry, no reset, no automatic state clear. |

Only `LONG` and `SHORT` are position sides. `NEAR`, `FAR`, empty, or unknown
values are labels, never directions. Emergency close rejects them with
`FAILED: INVALID_POSITION_SIDE`, retains forensic state, and alerts an operator.

## Legal transition matrix

| Confirmed exposure | Pending intent |
| --- | --- |
| `FLAT` | `ENTRY_BOTH_PENDING` |
| `OPEN` | `EXIT_BOTH_PENDING` |
| `OPEN` | `RELEASE_SIBLING_PENDING` |
| `OPEN` | `EXIT_REMAINING_PENDING` |
| `FLAT` or `OPEN` | `EXIT_SETTLEMENT_PENDING` |

Every unlisted pair is `UNKNOWN`. Never infer `SPREAD` or `SINGLE_LEG` from a
label alone.

## Integrity and operations

`MtsLedgerProjection` tail-reads JSONL only when file identity, offset, or
mtime changes. Its token records identity, offset, and last complete-record
hash, fencing a decision against replacement, truncation, and append races.
A trailing partial or malformed record is `UNKNOWN` until safely resolved.

When `UNKNOWN`, do not delete state or restart PM2 to make it appear flat.
Inspect fills, pending orders, and in live mode a broker snapshot. An urgent
manual close requires independently verified contract, side, and quantity; it
uses an idempotent command and completes only through fill callbacks.

Run before deployment:

```bash
.venv/bin/python -m pytest -q tests/strategies/test_mts_ledger_authority.py tests/strategies/test_mts_incident_recovery.py
```

The suite covers the 2026-08-06 reset incident, per-leg quantities, duplicates,
old-trade isolation, invalid side, pending transitions, JSONL tail recovery,
rotation identity, and paper/live separation.

## Limitation

State writes use revision CAS and atomic replacement. Owner-verified,
cross-process locking is not in this change. It needs a separate implementation
and test because it must hold only around snapshot, revision check, and replace,
never during callbacks, fill simulation, or order submission.
