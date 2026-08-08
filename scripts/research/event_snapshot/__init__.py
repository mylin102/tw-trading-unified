"""Reconciled event snapshot builder + runtime source adapter —
research-only.

Builder API (six P0 contracts) + source adapter API (runtime schema
mappings, shioaji_bidask allowlist, no last-price/OHLC conversion).
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
    NORMALIZED_SCHEMA,
    SIDE_MAP,
    adapt_bbo,
    adapt_fill,
    adapt_spread_event,
    build_normalized_snapshot,
    join_anchor,
    join_positions,
    normalize_sources,
)
