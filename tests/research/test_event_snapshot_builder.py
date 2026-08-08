#!/usr/bin/env python3
"""Reconciled event snapshot builder — RED contract tests.

RESEARCH ONLY, test-first. The committed skeletal package
(scripts/research/event_snapshot/) exposes the API; every contract test
COLLECTS and FAILS independently at its intended NotImplementedError point
— no importorskip/skip masking.
"""

from scripts.research.event_snapshot import (  # noqa: F401
    SCHEMA_VERSION, SOURCE_KINDS, attach_quotes, build_snapshot,
    emit_manifest, order_events, read_source_once)

import pytest


# ── output schema / versioning ───────────────────────────────────────────────

def test_builder_emits_versioned_list():
    # events.json must be a versioned list whose items match the run_replay
    # schema (source_event_seq/exchange_ts/recv_ts/decision_ts_ms/quotes)
    events = build_snapshot(
        input_paths=[], out_dir="/tmp/snap_out")
    assert isinstance(events, list)
    if events:
        ev = events[0]
        assert {"source_event_seq", "exchange_ts", "recv_ts",
                "decision_ts_ms"} <= set(ev), ev
        assert set(ev["quotes"]) == {"near", "far"}, ev


def test_schema_version_registered():
    assert SCHEMA_VERSION == "event-snapshot-v1"
    assert set(SOURCE_KINDS) == {"fills", "events", "quotes"}


# ── read-once + hash binding ─────────────────────────────────────────────────

def test_read_source_once_hash_binds_parsed_bytes(tmp_path):
    payload = b'[{"source_event_seq": 1, "exchange_ts": 100}]'
    p = tmp_path / "events.json"
    p.write_bytes(payload)
    records, sha = read_source_once(str(p))
    import hashlib
    assert sha == hashlib.sha256(payload).hexdigest(), \
        "hash must cover the exact parsed bytes"
    assert records is not None


# ── ordering / duplicates / out-of-order ─────────────────────────────────────

def test_order_events_by_exchange_ts_then_seq():
    ordered = order_events([
        {"source_event_seq": 2, "exchange_ts": 200},
        {"source_event_seq": 1, "exchange_ts": 100},
        {"source_event_seq": 1, "exchange_ts": 100},
    ])
    assert ordered is not None


def test_duplicate_events_rejected_with_reason():
    # identical (exchange_ts, source_event_seq) pairs are rejected
    # deterministically — never silently deduped
    result = order_events([
        {"source_event_seq": 1, "exchange_ts": 100},
        {"source_event_seq": 1, "exchange_ts": 100},
    ])
    assert isinstance(result, tuple) and result[0] == "DUPLICATE", result


def test_out_of_order_flagged():
    # input order differing from (exchange_ts, seq) is flagged + reason
    result = order_events([
        {"source_event_seq": 2, "exchange_ts": 200},
        {"source_event_seq": 1, "exchange_ts": 100},
    ])
    assert result is not None
    if isinstance(result, dict):
        assert result.get("reordered") is True, result


# ── BBO: no inference, explicit censored ─────────────────────────────────────

def test_missing_bbo_explicit_censored():
    # absent BBO must be an EXPLICIT censored/NOT_AVAILABLE record —
    # never an inferred last-price BBO
    events = [{"source_event_seq": 1, "exchange_ts": 100, "recv_ts": 101,
               "decision_ts_ms": 1_700_000_100_000}]
    result = attach_quotes(events, quote_records=[])
    assert result is not None
    out = result if isinstance(result, list) else result.get("events")
    for ev in out or []:
        assert ev["quotes"] == {"near": "NOT_AVAILABLE",
                                "far": "NOT_AVAILABLE"}, ev
        assert ev.get("bbo_reason"), \
            "censored BBO must carry a reason (no last-price inference)"


def test_invalid_synchronized_bbo_explicit_censored():
    # quote ts AFTER the event exchange_ts (future quote) never attaches
    events = [{"source_event_seq": 1, "exchange_ts": 100, "recv_ts": 101,
               "decision_ts_ms": 1_700_000_100_000}]
    quotes = [{"leg": "near", "bid": 50.0, "ask": 100.0,
               "quote_exchange_ts": 1_700_000_200_000,
               "quote_source": "q1"}]
    result = attach_quotes(events, quote_records=quotes)
    out = result if isinstance(result, list) else result.get("events")
    assert out[0]["quotes"]["near"] == "NOT_AVAILABLE", \
        "a future quote must never attach (no lookahead)"


# ── malformed / torn input ───────────────────────────────────────────────────

def test_malformed_input_refused():
    # torn/malformed source bytes REFUSE the whole build — a typed
    # ("REFUSED", reason) result, zero output
    result = build_snapshot(
        input_paths=["/nonexistent/torn.json"],
        out_dir="/tmp/snap_out")
    assert isinstance(result, tuple) and result[0] == "REFUSED", result
    assert result[1], "refusal must carry a reason"


# ── manifest / provenance ────────────────────────────────────────────────────

def test_manifest_maps_every_output_event_to_sources(tmp_path):
    manifest = emit_manifest(
        out_dir=str(tmp_path), events=[{"source_event_seq": 1}],
        sources=[{"path": "events.json", "sha256": "a" * 64}])
    assert manifest is not None
    assert manifest["schema_version"] == "event-snapshot-v1"
    assert "event_map" in manifest and "sources" in manifest, manifest
    assert manifest["sources"][0]["sha256"] == "a" * 64
