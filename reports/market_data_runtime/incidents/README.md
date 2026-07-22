# Incident Timeline — Shioaji Session Lifecycle

Tracks every session failure event with structured metadata.
Each row represents one complete session lifecycle from start to restart.

## Schema

```json
{
  "incident_id": "INC-20260722-001",
  "process_start": "2026-07-22T06:24:22",
  "process_pid": 866,
  "first_failure": "2026-07-22T08:45:09",
  "exception_type": "…",
  "exception_message": "…",
  "retry_result": "FAILED",
  "restart_time": "2026-07-22T08:50:01",
  "recovery_time": "2026-07-22T08:50:01",
  "session_age_seconds": 8447,
  "last_tick_age_ms": null,
  "lifecycle_events_file": "logs/lifecycle/lifecycle_events.jsonl"
}
```

## Status

Waiting for next restart to populate.
