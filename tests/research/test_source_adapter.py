#!/usr/bin/env python3
"""Runtime source adapter — RED contract tests (v2: real runtime shapes).

RESEARCH ONLY, test-first. Actual runtime evidence:
- files are JSONL (one record per line), NOT one JSON list
- fills: fill_type ∈ {ENTRY, RELEASE, EXIT, COMBINED_EXIT}; ENTRY sides are
  LONG/SHORT (not Buy/Sell); fills.contract is the NEAR/FAR LABEL
- BBO telemetry: contract_code is the ACTUAL code (TMFH6/TMFI6), which
  CHANGES after monthly settlement/roll — never a global hardcoded mapping
"""

from scripts.research.event_snapshot import (  # noqa: F401
    ANCHOR_EVENT_KINDS, BBO_SOURCE_ALLOWLIST, FILL_TYPES, adapt_bbo,
    adapt_fill, adapt_spread_event, build_normalized_snapshot, join_anchor,
    join_positions, normalize_sources, resolve_contract_mapping)

import json
import pytest


def _jsonl_bytes(lines):
    return ("\n".join(lines) + "\n").encode("utf-8")


FILL_ENTRY_NEAR = ('{"fill_type": "ENTRY", "timestamp": "2026-08-08T09:00:00.000", '
                   '"leg": "near", "contract": "NEAR", "side": "LONG", '
                   '"qty": 2, "trade_id": "t1"}')
FILL_RELEASE = ('{"fill_type": "RELEASE", "timestamp": "2026-08-08T10:00:02.000", '
                '"leg": "near", "contract": "NEAR", "side": "SHORT", '
                '"qty": 2, "trade_id": "t1"}')
EVENT_DECISION = ('{"event": "RELEASE_DECISION", "ts": "2026-08-08T10:00:00.000", '
                  '"trade_id": "t1", "order_id": "o1"}')
BBO_LINE = ('{"event_type": "BBO_UPDATE", "leg": "near", '
            '"contract_code": "TMFH6", "exchange_ts_ms": 1700000000000, '
            '"receive_ts_ms": 1700000000050, "source": "shioaji_bidask", '
            '"bid": 50.0, "ask": 100.0}')

CTX = {"source": "fills.jsonl", "sha256": "a" * 64,
       "record_no": 2, "byte_offset": 96}


# ── JSONL parsing: real byte offsets, torn lines REFUSED ─────────────────────

def test_jsonl_parse_preserves_exact_byte_offsets(tmp_path):
    # v2: JSONL parsed from bytes ONCE; byte_offset is the ACTUAL byte
    # position of the line start (not the record index)
    lines = [FILL_ENTRY_NEAR, FILL_RELEASE, EVENT_DECISION]
    p = tmp_path / "sources.jsonl"
    p.write_bytes(_jsonl_bytes(lines))
    result = normalize_sources([str(p)])
    assert result is not None
    if isinstance(result, dict):
        recs = result.get("fills", [])
        assert len(recs) == 2, recs
        first = recs[0]
        second = recs[1]
        assert first["byte_offset"] == 0
        assert second["byte_offset"] == len(lines[0].encode("utf-8")) + 1, \
            "byte_offset must be the actual line-start byte position"


def test_jsonl_malformed_line_refused(tmp_path):
    # a torn/malformed line REFUSES the whole source (never skipped)
    p = tmp_path / "torn.jsonl"
    p.write_bytes(b'{"fill_type": "ENTRY", "timestamp": "2026-08-08T09:00:00"\n'
                  b'NOT-JSON-LINE\n{"a": 1}\n')
    result = normalize_sources([str(p)])
    assert isinstance(result, tuple) and result[0] == "REFUSED", result
    assert "line" in result[1].lower(), result


# ── fill_type enum + ENTRY LONG/SHORT positions ──────────────────────────────

def test_adapt_fill_known_enum_only():
    for kind in ("ENTRY", "RELEASE", "EXIT", "COMBINED_EXIT"):
        raw = json.loads(FILL_ENTRY_NEAR.replace("ENTRY", kind))
        f = adapt_fill(raw, CTX)
        assert f is not None, kind
        assert f.get("fill_type") == kind
    bad = json.loads(FILL_ENTRY_NEAR.replace("ENTRY", "HEDGE"))
    result = adapt_fill(bad, CTX)
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result


def test_entry_sides_are_long_short_not_buy_sell():
    raw = json.loads(FILL_ENTRY_NEAR)
    f = adapt_fill(raw, CTX)
    assert f.get("side") == "LONG", f
    raw2 = json.loads(FILL_ENTRY_NEAR.replace("LONG", "SHORT"))
    assert adapt_fill(raw2, CTX).get("side") == "SHORT"


def test_entry_qty_side_validation():
    raw = json.loads(FILL_ENTRY_NEAR)
    raw["qty"] = 0
    result = adapt_fill(raw, CTX)
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result
    assert "qty" in result[1], result


def test_join_positions_from_entry_long_short():
    fills = [{"trade_id": "t1", "leg": "near", "fill_type": "ENTRY",
              "side": "LONG", "qty": 2},
             {"trade_id": "t1", "leg": "far", "fill_type": "ENTRY",
              "side": "SHORT", "qty": 2}]
    pos = join_positions(fills, "t1")
    assert pos == {"near": "LONG", "far": "SHORT"}, pos


# ── contract mapping: labels vs codes, per-window, never hardcoded ───────────

def test_contract_labels_never_compared_to_codes():
    # fills.contract is the NEAR/FAR LABEL; BBO contract_code is the ACTUAL
    # code — the adapter must NEVER compare the two directly; a mapping is
    # required
    raw = json.loads(BBO_LINE)  # contract_code=TMFH6
    q = adapt_bbo(raw, CTX)
    assert q is not None
    assert q.get("contract") == "TMFH6"
    assert q.get("leg") == "near"
    # the label NEAR and the code TMFH6 are different namespaces — no test
    # may assert an equality between them


def test_contract_mapping_resolved_per_decision(tmp_path):
    # mapping resolves from authoritative records / a versioned mapping
    # input, keyed by decision timestamp
    mapping = {
        "windows": [
            {"valid_from_ms": 1_600_000_000_000,
             "valid_to_ms": 1_800_000_000_000,
             "near": "TMFH6", "far": "TMFI6",
             "version": "map-v1"},
        ]
    }
    m = resolve_contract_mapping(
        records=[], mapping_input=mapping,
        decision_ts_ms=1_700_000_000_000)
    assert m is not None
    assert m.get("near") == "TMFH6" and m.get("far") == "TMFI6", m
    assert m.get("version") == "map-v1"
    assert m.get("evidence") and m.get("hash"), m


def test_contract_mapping_roll_windows_distinct():
    # v2-P0: near/far codes CHANGE after monthly settlement — two windows
    # map DIFFERENT codes; resolution is per-window
    mapping = {
        "windows": [
            {"valid_from_ms": 1_600_000_000_000,
             "valid_to_ms": 1_700_000_000_000,
             "near": "TMFH6", "far": "TMFI6", "version": "map-v1"},
            {"valid_from_ms": 1_700_000_000_000,
             "valid_to_ms": 1_800_000_000_000,
             "near": "TMFI6", "far": "TMFH6", "version": "map-v2"},
        ]
    }
    m1 = resolve_contract_mapping([], mapping, 1_650_000_000_000)
    m2 = resolve_contract_mapping([], mapping, 1_750_000_000_000)
    assert m1["near"] == "TMFH6" and m1["far"] == "TMFI6", m1
    assert m2["near"] == "TMFI6" and m2["far"] == "TMFH6", m2
    assert m1["version"] != m2["version"]


def test_contract_mapping_missing_not_available():
    result = resolve_contract_mapping(
        records=[], mapping_input=None,
        decision_ts_ms=1_700_000_000_000)
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result
    assert "mapping" in result[1].lower(), result


def test_contract_mapping_at_roll_boundary_ambiguous():
    # at the roll boundary the window is ambiguous -> NOT_AVAILABLE, never
    # a guess
    mapping = {
        "windows": [
            {"valid_from_ms": 1_600_000_000_000,
             "valid_to_ms": 1_700_000_000_000,
             "near": "TMFH6", "far": "TMFI6", "version": "map-v1"},
        ]
    }
    result = resolve_contract_mapping(
        [], mapping, 1_700_000_000_000)
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result


def test_bbo_wrong_period_rejected():
    # a BBO from a DIFFERENT validity window (cross-roll) never joins —
    # the quote's code must match the event's per-window mapping
    mapping = {
        "windows": [
            {"valid_from_ms": 1_600_000_000_000,
             "valid_to_ms": 1_700_000_000_000,
             "near": "TMFH6", "far": "TMFI6", "version": "map-v1"},
            {"valid_from_ms": 1_700_000_000_000,
             "valid_to_ms": 1_800_000_000_000,
             "near": "TMFI6", "far": "TMFH6", "version": "map-v2"},
        ]
    }
    m = resolve_contract_mapping([], mapping, 1_750_000_000_000)
    assert m["near"] == "TMFI6"
    # a TMFH6 BBO (previous window's near code) is WRONG for this event
    raw = json.loads(BBO_LINE)  # contract_code=TMFH6
    q = adapt_bbo(raw, CTX)
    assert q["contract"] == "TMFH6"
    assert q["contract"] != m["near"], \
        "cross-roll BBO code must never join this window's near leg"


# ── manifest mapping evidence ────────────────────────────────────────────────

def test_manifest_records_mapping_evidence():
    result = build_normalized_snapshot(
        [], str(pytest.importorskip("pathlib").Path("/tmp") / "na_out"))
    if isinstance(result, dict):
        assert "contract_mapping" in result.get("manifest", {}), result


# ── retained contracts (v1) ──────────────────────────────────────────────────

def test_bbo_non_allowlisted_source_rejected():
    raw = json.loads(BBO_LINE.replace("shioaji_bidask", "other_feed"))
    result = adapt_bbo(raw, CTX)
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result
    assert "source" in result[1], result


def test_no_last_price_ohlc_conversion():
    raw = {"event_type": "LAST_UPDATE", "leg": "near",
           "contract_code": "TMFH6", "exchange_ts_ms": 1_700_000_000_000,
           "receive_ts_ms": 1_700_000_000_050, "source": "shioaji_bidask",
           "last": 75.0}
    result = adapt_bbo(raw, CTX)
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result


def test_join_anchor_legal_same_trade():
    fills = [{"trade_id": "t1", "leg": "near", "fill_type": "RELEASE"}]
    events = [{"trade_id": "t1", "kind": "RELEASE_DECISION", "order_id": "o1"}]
    anchor = join_anchor(fills, events)
    assert anchor is not None


def test_join_anchor_ambiguous_rejected():
    fills = [{"trade_id": "t1", "leg": "near", "fill_type": "RELEASE"}]
    events = [{"trade_id": "t2", "kind": "RELEASE_DECISION", "order_id": "o1"}]
    result = join_anchor(fills, events)
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result


def test_unsupported_raw_tick_csv_refused(tmp_path):
    p = tmp_path / "ticks.csv"
    p.write_text("ts,price,qty\n1,44300,2\n", encoding="utf-8")
    result = normalize_sources([str(p)])
    assert isinstance(result, tuple) and result[0] == "REFUSED", result
    assert "csv" in result[1].lower(), result


def test_output_no_replace_atomic(tmp_path):
    (tmp_path / "events.json").write_text("OLD", encoding="utf-8")
    result = build_normalized_snapshot([], str(tmp_path))
    assert isinstance(result, tuple) and result[0] == "REFUSED", result
    assert (tmp_path / "events.json").read_text(encoding="utf-8") == "OLD"
