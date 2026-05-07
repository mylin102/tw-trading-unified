# Trade Ledger Durability Fix Spec

## 1. Problem Summary

The current options trading monitor can send an entry email while the dashboard shows no corresponding trade record. The most likely root cause is not GitHub Actions or dashboard rendering, but a local PM2 `trading-system` process that generated a signal, sent email, and then failed to persist the trade ledger row before the process exited/crashed.

Observed behavior:

- Email was sent for `[TXO] ENTRY C @ 10.0`.
- Dashboard showed no trade record.
- `paper_trading/options_trade_ledger.csv` had no new row for the day.
- PM2 process restarted multiple times and sessions exited due to stale TX data.
- The current CSV append path likely uses `pandas.to_csv(..., mode="a")` without explicit `flush()` / `fsync()`.

## 2. Root Cause Classification

| Issue | Status | Priority | Notes |
|---|---:|---:|---|
| CSV append without `flush()` / `fsync()` | Confirmed likely root cause | P0 | Low-frequency write can remain buffered and disappear on abnormal process exit. |
| Email sent before confirmed persistence | Design flaw / amplifier | P1 | Email success does not imply trade persistence. |
| Ledger path mismatch | Possible but unproven | P2 | Should still be instrumented and verified. |
| Logger hierarchy mismatch | Observability issue | P2 | May hide useful diagnostics but is not the direct cause. |

## 3. Correct Durability Semantics

The old mental model was:

```text
to_csv() succeeded → trade is persisted
```

The correct model is:

```text
write() → userspace/kernel buffer
flush() → pushed from Python buffer
fsync() → forced to storage device
```

For a trading ledger, `fsync()` is required after every trade event.

## 4. Required Fixes

### Fix 1 — Make `log_trade()` durability-safe

Do not rely on `pandas.to_csv(..., mode="a")` alone. Generate the CSV row in memory, then write through an explicitly opened file handle and call `flush()` + `os.fsync()` on the same file descriptor.

```python
from pathlib import Path
import io
import os
import pandas as pd


def append_csv_row_durable(path: Path, row: dict) -> None:
    """
    Append one CSV row and force it to disk.

    This is intended for low-frequency, high-importance trading records
    where losing one row is unacceptable.
    """
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    write_header = not path.exists() or path.stat().st_size == 0

    df_row = pd.DataFrame([row])
    buf = io.StringIO()
    df_row.to_csv(buf, index=False, header=write_header)

    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())
        f.flush()
        os.fsync(f.fileno())

    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"Trade ledger write failed: {path}")
```

### Fix 2 — Return a `trade_id` from `log_trade()`

Every trade event should have a deterministic ID. Use timestamp + action + side + price + qty, or a UUID.

```python
from datetime import datetime, timezone
from uuid import uuid4


def make_trade_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"trade_{ts}_{uuid4().hex[:8]}"
```

Example `log_trade()` wrapper:

```python
def log_trade(self, action: str, side: str, price: float, note: str = "") -> str:
    self._update_log_paths()  # Ensure current live/paper path is correct.

    trade_id = make_trade_id()
    row = {
        "trade_id": trade_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "side": side,
        "price": price,
        "note": note,
        "ledger_path": str(self.ledger_path.resolve()),
    }

    append_csv_row_durable(self.ledger_path, row)

    logger.info(
        "TRADE_LEDGER_WRITTEN path=%s trade_id=%s action=%s side=%s price=%s size=%d",
        self.ledger_path.resolve(),
        trade_id,
        action,
        side,
        price,
        self.ledger_path.stat().st_size,
    )

    return trade_id
```

### Fix 3 — Send email only after ledger persistence

Old flow:

```text
signal → send email → write ledger maybe succeeds
```

Correct flow:

```text
signal → write ledger durably → verify row → send email with trade_id
```

Example:

```python
trade_id = self.log_trade("LIVE_ENTRY_FILLED", side, price, f"qty={quantity}")

self._notify(
    title=f"[TXO] ENTRY {side} @ {price}",
    message=f"trade_id={trade_id}\nqty={quantity}\nledger={self.ledger_path.resolve()}",
)
```

### Fix 4 — Add path diagnostics before every write

Because paper/live paths may change depending on `dry_run`, `live_trading`, wrapper configuration, or runtime overrides, always log the resolved ledger path.

```python
logger.info(
    "TRADE_LEDGER_PATH mode=%s live_trading=%s dry_run=%s path=%s",
    "LIVE" if self.live_trading else "PAPER",
    self.live_trading,
    self.dry_run,
    self.ledger_path.resolve(),
)
```

### Fix 5 — Optional atomic append lock

If multiple PM2 processes or duplicate monitors can write to the same ledger, add a process-level file lock.

```python
import fcntl

with open(path, "a", encoding="utf-8", newline="") as f:
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    try:
        f.write(buf.getvalue())
        f.flush()
        os.fsync(f.fileno())
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
```

Use this if duplicate monitor risk still exists.

## 5. Verification Checklist

### A. Find all possible ledgers

```bash
find . \( -name "*ledger*.csv" -o -name "*trade*.csv" \) -print | xargs ls -lt
```

### B. Search for the missing trade

```bash
grep -R "TXO\|ENTRY\|CALL\|LIVE_ENTRY\|PAPER_ENTRY\|8:44" . \
  --include="*.csv" \
  --include="*.log"
```

### C. Confirm all call sites

```bash
grep -R "def log_trade\|log_trade(" . --include="*.py"
```

### D. Simulate crash immediately after write

Create a one-off test:

```python
monitor.log_trade("TEST_ENTRY", "CALL", 10.0, "crash-test")
os._exit(1)
```

Then verify the row remains:

```bash
tail -5 paper_trading/options_trade_ledger.csv
ls -l --full-time paper_trading/options_trade_ledger.csv
```

### E. Confirm `fsync()` syscall, optional advanced check

```bash
strace -e trace=write,fsync -p <pid>
```

Expected: a `write(...)` followed by `fsync(...)` after trade logging.

## 6. Recommended Implementation Order

1. Add durable CSV append with `flush()` + `fsync()`.
2. Add `TRADE_LEDGER_WRITTEN` log with resolved absolute path.
3. Move email notification after successful ledger write.
4. Add `trade_id` into both ledger and email.
5. Add file locking if duplicate PM2 / duplicate monitor processes are still possible.
6. Re-run crash simulation.
7. Confirm dashboard reads the same resolved ledger path.

## 7. Production Principle

For trading systems:

```text
Notification is not the source of truth.
Ledger persistence is the source of truth.
```

Therefore:

```text
No ledger row → no confirmed trade event
Ledger row persisted → then notify
```

## 8. Final Diagnosis

The most likely bug is:

```text
log_trade() wrote a low-frequency trade row through buffered CSV append,
but the PM2 session exited/crashed before the row was flushed and fsynced.
The email had already been sent, so the user saw a notification without a dashboard record.
```

The durable-write fix should eliminate this failure mode.
