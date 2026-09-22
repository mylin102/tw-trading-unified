# Session instrumentation: PAPER two-owner reproduction

This slice is observe-only. Do not run a second login with production
credentials. To reproduce the suspected same-account session competition in an
isolated environment, use two fake API owners and the session observer:

```sh
MTS_SESSION_EVENT_LOG_PATH=/tmp/session-two-owners.jsonl \
python -m pytest -q tests/core/test_session_instrumentation.py \
  -k paper_two_session_owners_are_observable_without_broker_login
```

The test creates two independent API-shaped objects, assigns two monotonic
`session_generation` values, and verifies distinct `api_object_id` values in
the append-only event stream. It performs no Shioaji construction, login,
logout, quote subscription, query, or order operation. A real multi-login
experiment is intentionally prohibited during live hours; the evidence needed
to compare real owners is the resulting `SESSION_API_CREATED` and
`SESSION_LOGIN` records from each resident process.
