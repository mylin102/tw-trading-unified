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
