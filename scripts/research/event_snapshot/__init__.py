"""Reconciled event snapshot builder — research-only.

Re-exports the implemented API from builder.py (six P0 contracts):
legal same-trade decision anchor, byte/record provenance + deterministic
ties, executable BBO feed allowlist with typed unavailable legs,
pre-anchor contract/leg/close-side validation, exclusive no-overwrite
atomic events+manifest output, read-once hash binding.
"""

from scripts.research.event_snapshot.builder import (  # noqa: F401
    EXECUTABLE_QUOTE_FEEDS,
    SCHEMA_VERSION,
    SOURCE_KINDS,
    attach_provenance,
    attach_quotes,
    build_snapshot,
    emit_manifest,
    legal_anchor,
    order_events,
    read_source_once,
)
from scripts.research.event_snapshot.source_adapter import (  # noqa: F401
    ANCHOR_EVENT_KINDS,
    BBO_SOURCE_ALLOWLIST,
    FILL_TYPES,
    adapt_bbo,
    adapt_fill,
    adapt_spread_event,
    build_normalized_snapshot,
    join_anchor,
    join_positions,
    normalize_sources,
)
