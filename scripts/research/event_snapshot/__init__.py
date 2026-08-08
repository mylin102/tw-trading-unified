"""Reconciled event snapshot builder — skeletal (research-only, test-first).

Every function raises NotImplementedError until the reviewed implementation
lands; RED contract tests fail independently at their intended points.
"""

from scripts.research.event_snapshot import builder as _builder  # noqa: F401

SCHEMA_VERSION = "event-snapshot-v1"

SOURCE_KINDS = ("fills", "events", "quotes")

# executable bid/ask quote feeds (v2): last/mark/OHLC can NEVER populate
# bid/ask — quote_source must be one of these
EXECUTABLE_QUOTE_FEEDS = ("bbo_near", "bbo_far")


def build_snapshot(input_paths, out_dir):
    """Build events.json (versioned list, run_replay-compatible) +
    manifest.json from the named immutable byte snapshots.

    - each input read EXACTLY once; sha256 over the same parsed bytes
    - ordering (exchange_ts, source_event_seq); ties resolved by source
      record identity (never incidental file order); duplicates rejected
    - output events ONLY from legal decision anchors (timestamped,
      same-trade release-decision/submission); RELEASE fills NEVER supply
      decision_ts; no anchor -> no candidate or explicit NOT_AVAILABLE
      provenance row — never synthesized
    - quotes: executable-feed allowlist only; exchange_ts <= anchor, same
      contract/leg, valid close side from pre-decision position; missing/
      invalid -> TYPED per-leg unavailable structure + reason (never a
      last-price inference, never a bare string)
    - malformed/torn input -> REFUSED; no lookahead
    """
    raise NotImplementedError("event_snapshot.build_snapshot: not implemented")


def read_source_once(path):
    """Read one source file exactly once; return (records, sha256)."""
    raise NotImplementedError("event_snapshot.read_source_once: not implemented")


def legal_anchor(records):
    """The legal decision anchor: a timestamped, SAME-TRADE
    release-decision/submission record. RELEASE fills are post-decision and
    never supply decision_ts. No anchor -> ("NOT_AVAILABLE", reason)."""
    raise NotImplementedError("event_snapshot.legal_anchor: not implemented")


def attach_provenance(record):
    """Event-time provenance: source byte hash + record number/byte offset
    + original timestamp text + parsed timestamp/unit/offset."""
    raise NotImplementedError(
        "event_snapshot.attach_provenance: not implemented")


def order_events(events):
    """Deterministic total order by (exchange_ts, source_event_seq); EQUAL
    timestamps tie by source record identity (byte offset), not file
    order. Duplicates -> ("DUPLICATE", reason)."""
    raise NotImplementedError("event_snapshot.order_events: not implemented")


def attach_quotes(events, quote_records):
    """Attach per-leg BBO from the EXECUTABLE_QUOTE_FEEDS allowlist.

    Validate exchange_ts <= legal decision anchor + same contract/leg +
    exact contract mapping + valid close side from pre-decision position.
    Missing/invalid -> TYPED per-leg unavailable structure
    {"available": False, "reason": ...} — never a bare string, never a
    last/mark/OHLC price.
    """
    raise NotImplementedError("event_snapshot.attach_quotes: not implemented")


def emit_manifest(out_dir, events, sources):
    """Versioned manifest mapping every output event to source records and
    hashes. Same exclusive no-overwrite/atomic finalization as the
    runner."""
    raise NotImplementedError("event_snapshot.emit_manifest: not implemented")
