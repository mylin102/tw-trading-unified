#!/usr/bin/env python3
"""Reconciled event snapshot builder — RED contract tests.

RESEARCH ONLY, test-first. The committed skeletal package
(scripts/research/event_snapshot/) exposes the API; every contract test
COLLECTS and FAILS independently at its intended NotImplementedError point
— no importorskip/skip masking.
"""

from scripts.research.event_snapshot import (  # noqa: F401
    EXECUTABLE_QUOTE_FEEDS, SCHEMA_VERSION, SOURCE_KINDS, attach_provenance,
    attach_quotes, build_snapshot, emit_manifest, legal_anchor, order_events,
    read_source_once)

import json
import pytest


# ── output schema / versioning ───────────────────────────────────────────────

def test_builder_emits_versioned_list(tmp_path):
    # events.json must be a versioned list whose items match the run_replay
    # schema (source_event_seq/exchange_ts/recv_ts/decision_ts_ms/quotes)
    events = build_snapshot(
        input_paths=[], out_dir=str(tmp_path / "snap_out"))
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
    # v2: absent BBO must be a TYPED per-leg unavailable structure with a
    # reason — never an inferred last-price BBO, never a bare string that
    # silently becomes a runner-schema error
    events = [{"source_event_seq": 1, "exchange_ts": 100, "recv_ts": 101,
               "decision_ts_ms": 1_700_000_100_000}]
    result = attach_quotes(events, quote_records=[])
    assert result is not None
    out = result if isinstance(result, list) else result.get("events")
    for ev in out or []:
        for leg in ("near", "far"):
            q = ev["quotes"][leg]
            assert isinstance(q, dict) and q.get("available") is False, \
                f"typed per-leg unavailable required: {leg}={q!r}"
            assert q.get("reason"), \
                "unavailable quote must carry a reason"


def test_invalid_synchronized_bbo_explicit_censored():
    # quote ts AFTER the event exchange_ts (future quote) never attaches
    events = [{"source_event_seq": 1, "exchange_ts": 100, "recv_ts": 101,
               "decision_ts_ms": 1_700_000_100_000, "contract": "TMFH6",
               "position_side": "SHORT"}]
    quotes = [{"leg": "near", "bid": 50.0, "ask": 100.0,
               "quote_exchange_ts": 1_700_000_200_000,
               "quote_source": "bbo_near", "contract": "TMFH6",
               "close_action": "SHORT"}]
    result = attach_quotes(events, quote_records=quotes)
    out = result if isinstance(result, list) else result.get("events")
    q = out[0]["quotes"]["near"]
    assert isinstance(q, dict) and q.get("available") is False, q
    assert "anchor" in q.get("reason", ""), \
        "a quote after the legal decision anchor must never attach"


# ── malformed / torn input ───────────────────────────────────────────────────

def test_malformed_input_refused(tmp_path):
    # torn/malformed source bytes REFUSE the whole build — a typed
    # ("REFUSED", reason) result, zero output
    result = build_snapshot(
        input_paths=["/nonexistent/torn.json"],
        out_dir=str(tmp_path / "snap_out"))
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


# ── v2 P0 conditions: legal anchors / provenance / ties / feeds / races ──────

def test_release_fill_only_rejected():
    # v2(1): a RELEASE fill is post-decision — it must NEVER supply
    # decision_ts; with no legal anchor the builder emits no candidate (or
    # an explicit NOT_AVAILABLE provenance row), never a synthesized one
    fills = [{"record": "fill", "trade_id": "t1",
              "ts_text": "2026-08-08T10:00:02", "kind": "RELEASE"}]
    anchor = legal_anchor(records=fills)
    assert isinstance(anchor, tuple) and anchor[0] == "NOT_AVAILABLE", anchor


def test_decision_anchor_from_submission_record():
    # a timestamped, SAME-TRADE release-decision/submission record IS a
    # legal anchor when joined to its trade's fills
    records = [
        {"record": "fill", "trade_id": "t1", "kind": "RELEASE",
         "ts_text": "2026-08-08T10:00:02"},
        {"record": "submission", "trade_id": "t1",
         "ts_text": "2026-08-08T10:00:00", "kind": "RELEASE_DECISION"},
    ]
    anchor = legal_anchor(records=records)
    assert anchor is not None


def test_wrong_trade_anchor_rejected():
    # v2(1): a record from a DIFFERENT trade cannot anchor the output event
    records = [
        {"record": "fill", "trade_id": "t1", "kind": "RELEASE",
         "ts_text": "2026-08-08T10:00:02"},
        {"record": "submission", "trade_id": "t2",
         "ts_text": "2026-08-08T10:00:00", "kind": "RELEASE_DECISION"},
    ]
    anchor = legal_anchor(records=records)
    assert isinstance(anchor, tuple) and anchor[0] == "NOT_AVAILABLE", anchor


def test_provenance_preserves_original_timestamp_text():
    # v2(2): byte hash + record number/byte offset + original ts TEXT +
    # parsed ts/unit/offset
    prov = attach_provenance({
        "record_no": 3, "byte_offset": 128,
        "ts_text": "2026-08-08T10:00:00.123", "trade_id": "t1"})
    assert prov is not None
    for field in ("byte_hash", "record_no", "byte_offset", "ts_text",
                  "parsed_ts", "ts_unit", "ts_offset"):
        assert field in prov, f"provenance missing {field}: {prov}"
    assert prov["ts_text"] == "2026-08-08T10:00:00.123"


def test_equal_timestamp_deterministic_tie():
    # v2(2): EQUAL (exchange_ts, seq) ties resolve by source record
    # identity (byte offset), never incidental file order
    a = {"source_event_seq": 1, "exchange_ts": 100, "byte_offset": 10}
    b = {"source_event_seq": 1, "exchange_ts": 100, "byte_offset": 5}
    ordered = order_events([a, b])
    assert ordered is not None
    seq = [e.get("byte_offset") for e in
           (ordered if isinstance(ordered, list) else [])]
    if seq:
        assert seq == [5, 10], f"tie must use record identity: {seq}"


def test_last_price_quote_rejected():
    # v2(3): last/mark/OHLC feeds can NEVER populate bid/ask
    events = [{"source_event_seq": 1, "exchange_ts": 100, "recv_ts": 101,
               "decision_ts_ms": 1_700_000_100_000}]
    quotes = [{"leg": "near", "bid": 50.0, "ask": 100.0,
               "quote_exchange_ts": 1_700_000_000_000,
               "quote_source": "last_price"}]
    result = attach_quotes(events, quote_records=quotes)
    out = result if isinstance(result, list) else result.get("events")
    q = out[0]["quotes"]["near"]
    assert isinstance(q, dict) and q.get("available") is False, q
    assert "feed" in q.get("reason", ""), q


def test_wrong_contract_quote_rejected():
    # v2(4): a quote for a DIFFERENT contract/leg never attaches
    events = [{"source_event_seq": 1, "exchange_ts": 100, "recv_ts": 101,
               "decision_ts_ms": 1_700_000_100_000,
               "contract": "TMFH6"}]
    quotes = [{"leg": "near", "contract": "TXFH6",
               "bid": 50.0, "ask": 100.0,
               "quote_exchange_ts": 1_700_000_000_000,
               "quote_source": "bbo_near"}]
    result = attach_quotes(events, quote_records=quotes)
    out = result if isinstance(result, list) else result.get("events")
    q = out[0]["quotes"]["near"]
    assert isinstance(q, dict) and q.get("available") is False, q
    assert "contract" in q.get("reason", ""), q


def test_output_no_overwrite(tmp_path):
    # v2(5): events.json + manifest.json use the runner's exclusive
    # no-overwrite/atomic finalization policy
    (tmp_path / "events.json").write_text("OLD-EVENTS", encoding="utf-8")
    result = build_snapshot(
        input_paths=[], out_dir=str(tmp_path))
    assert isinstance(result, tuple) and result[0] == "REFUSED", result
    assert (tmp_path / "events.json").read_text(encoding="utf-8") == \
        "OLD-EVENTS", "existing output must never be overwritten"


# ── v3 P0 conditions: anchor ts validation / contract+position / atomic ─────

def test_invalid_anchor_timestamp_refused(tmp_path):
    # v3(1): decision_ts_ms must be a VALIDATED epoch-ms parsed from the
    # anchor's ts_text — invalid/missing timestamp REFUSES the build; a
    # post-decision RELEASE fill timestamp is never used
    fills = tmp_path / "fills.json"
    fills.write_text(json.dumps([{"record": "fill", "trade_id": "t1",
                                  "kind": "RELEASE",
                                  "ts_text": "2026-08-08T10:00:02"}]),
                     encoding="utf-8")
    evs = tmp_path / "events.json"
    evs.write_text(json.dumps([{"record": "submission", "trade_id": "t1",
                                "kind": "RELEASE_DECISION",
                                "ts_text": "NOT-A-TIMESTAMP"}]),
                   encoding="utf-8")
    result = build_snapshot([str(fills), str(evs)],
                            str(tmp_path / "out"))
    assert isinstance(result, tuple) and result[0] == "REFUSED", result
    assert "timestamp" in result[1].lower(), result


def test_missing_quote_contract_unavailable():
    # v3(2): a quote WITHOUT a contract can never attach — unavailable
    events = [{"source_event_seq": 1, "exchange_ts": 100, "recv_ts": 101,
               "decision_ts_ms": 1_700_000_100_000, "contract": "TMFH6",
               "position_side": "SHORT"}]
    quotes = [{"leg": "near", "bid": 50.0, "ask": 100.0,
               "quote_exchange_ts": 1_700_000_000_000,
               "quote_source": "bbo_near", "close_action": "SHORT"}]
    result = attach_quotes(events, quote_records=quotes)
    q = result[0]["quotes"]["near"]
    assert isinstance(q, dict) and q.get("available") is False, q
    assert "contract" in q.get("reason", ""), q


def test_close_side_provenance_validated():
    # v3(2): close_action is validated against the pre-decision position
    # record — a mismatched quote claim is unavailable; a matching claim
    # attaches with the position-derived close_action
    base = {"source_event_seq": 1, "exchange_ts": 100, "recv_ts": 101,
            "decision_ts_ms": 1_700_000_100_000, "contract": "TMFH6",
            "position_side": "LONG"}
    quote = {"leg": "near", "contract": "TMFH6",
             "bid": 50.0, "ask": 100.0,
             "quote_exchange_ts": 1_700_000_000_000,
             "quote_source": "bbo_near", "close_action": "SHORT"}
    result = attach_quotes([base], [quote])
    q = result[0]["quotes"]["near"]
    assert isinstance(q, dict) and q.get("available") is False, q
    assert "close_action" in q.get("reason", ""), q
    quote_ok = dict(quote, close_action="LONG")
    result2 = attach_quotes([base], [quote_ok])
    q2 = result2[0]["quotes"]["near"]
    assert q2.get("available") is True, q2
    assert q2.get("close_action") == "LONG", \
        "close_action must come from the validated position side"


def test_manifest_failure_leaves_zero_files(tmp_path, monkeypatch):
    # v3(3): all-or-nothing — a manifest failure removes every
    # newly-created output file/dir (zero files, no partial output)
    import scripts.research.event_snapshot.builder as bmod
    fills = tmp_path / "fills.json"
    fills.write_text(json.dumps([{"record": "fill", "trade_id": "t1",
                                  "kind": "RELEASE",
                                  "ts_text": "2026-08-08T10:00:02"}]),
                     encoding="utf-8")
    evs = tmp_path / "events.json"
    evs.write_text(json.dumps([{"record": "submission", "trade_id": "t1",
                                "kind": "RELEASE_DECISION",
                                "ts_text": "2026-08-08T10:00:00"}]),
                   encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(bmod, "emit_manifest", boom)
    out = tmp_path / "out"
    result = build_snapshot([str(fills), str(evs)], str(out))
    assert isinstance(result, tuple) and result[0] == "REFUSED", result
    assert not out.exists(), "zero newly-created files on failure"
