"""Reconciled event snapshot builder — skeletal (research-only, test-first).

Every function raises NotImplementedError until the reviewed implementation
lands; RED contract tests fail independently at their intended points.
"""

from scripts.research.event_snapshot import builder as _builder  # noqa: F401

SCHEMA_VERSION = "event-snapshot-v1"

SOURCE_KINDS = ("fills", "events", "quotes")


def build_snapshot(input_paths, out_dir):
    """Build events.json (versioned list, run_replay-compatible) +
    manifest.json from the named immutable byte snapshots.

    - each input read EXACTLY once; sha256 over the same parsed bytes
    - ordering (exchange_ts, source_event_seq); duplicates rejected with
      reason; out-of-order flagged
    - missing/invalid synchronized BBO -> explicit censored/NOT_AVAILABLE
      record (never an inferred last-price BBO)
    - malformed/torn input -> REFUSED (non-zero, zero output)
    - no lookahead: quotes attach only from records at/before the event
    """
    raise NotImplementedError("event_snapshot.build_snapshot: not implemented")


def read_source_once(path):
    """Read one source file exactly once; return (records, sha256)."""
    raise NotImplementedError("event_snapshot.read_source_once: not implemented")


def order_events(events):
    """Deterministic total order by (exchange_ts, source_event_seq)."""
    raise NotImplementedError("event_snapshot.order_events: not implemented")


def attach_quotes(events, quote_records):
    """Attach per-leg BBO (ts/age/source) — absent/invalid ->
    censored/NOT_AVAILABLE record. NEVER infer last-price BBO."""
    raise NotImplementedError("event_snapshot.attach_quotes: not implemented")


def emit_manifest(out_dir, events, sources):
    """Versioned manifest mapping every output event to source records and
    hashes."""
    raise NotImplementedError("event_snapshot.emit_manifest: not implemented")
