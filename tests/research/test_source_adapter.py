#!/usr/bin/env python3
"""Runtime source adapter — RED contract tests.

RESEARCH ONLY, test-first. The committed skeletal
scripts/research/event_snapshot/source_adapter.py exposes the API; every
contract test COLLECTS and FAILS independently at its intended
NotImplementedError point.
"""

from scripts.research.event_snapshot import (  # noqa: F401
    ANCHOR_EVENT_KINDS, BBO_SOURCE_ALLOWLIST, FILL_TYPES, adapt_bbo,
    adapt_fill, adapt_spread_event, build_normalized_snapshot, join_anchor,
    join_positions, normalize_sources)

import pytest

CTX = {"source": "fills.json", "sha256": "a" * 64,
       "record_no": 3, "byte_offset": 128}


# ── schema mapping ────────────────────────────────────────────────────────────

def test_adapt_fill_maps_runtime_schema():
    raw = {"fill_type": "fill", "timestamp": "2026-08-08T10:00:02.000",
           "leg": "near", "contract": "TMFH6", "side": "Sell",
           "trade_id": "t1"}
    fill = adapt_fill(raw, CTX)
    assert fill is not None
    for field in ("trade_id", "leg", "contract", "side", "ts_text",
                  "parsed_ts", "unit", "source", "record_no",
                  "byte_offset", "byte_hash"):
        assert field in fill, f"missing {field}: {fill}"


def test_adapt_spread_event_maps_anchor():
    raw = {"event": "RELEASE_DECISION", "ts": "2026-08-08T10:00:00.000",
           "trade_id": "t1", "order_id": "o1"}
    ev = adapt_spread_event(raw, CTX)
    assert ev is not None
    assert ev.get("kind") == "RELEASE_DECISION"
    assert ev.get("trade_id") == "t1"


def test_adapt_bbo_maps_telemetry():
    raw = {"event_type": "BBO_UPDATE", "leg": "near",
           "contract_code": "TMFH6", "exchange_ts_ms": 1_700_000_000_000,
           "receive_ts_ms": 1_700_000_000_050, "source": "shioaji_bidask",
           "bid": 50.0, "ask": 100.0}
    q = adapt_bbo(raw, CTX)
    assert q is not None
    for field in ("leg", "contract", "exchange_ts_ms", "receive_ts_ms",
                  "source", "bid", "ask"):
        assert field in q, f"missing {field}: {q}"


# ── allowlist / no inference ─────────────────────────────────────────────────

def test_bbo_non_allowlisted_source_rejected():
    raw = {"event_type": "BBO_UPDATE", "leg": "near",
           "contract_code": "TMFH6", "exchange_ts_ms": 1_700_000_000_000,
           "receive_ts_ms": 1_700_000_000_050, "source": "some_other",
           "bid": 50.0, "ask": 100.0}
    result = adapt_bbo(raw, CTX)
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result
    assert "source" in result[1], result


def test_no_last_price_ohlc_conversion():
    # a last/OHLC record must NEVER be converted into bid/ask
    raw = {"event_type": "LAST_UPDATE", "leg": "near",
           "contract_code": "TMFH6", "exchange_ts_ms": 1_700_000_000_000,
           "receive_ts_ms": 1_700_000_000_050, "source": "shioaji_bidask",
           "last": 75.0}
    result = adapt_bbo(raw, CTX)
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result


# ── read-once + provenance ───────────────────────────────────────────────────

def test_normalize_sources_reads_once_and_hashes(tmp_path):
    p = tmp_path / "fills.json"
    payload = (b'[{"fill_type": "fill", "timestamp": "2026-08-08T10:00:02", '
               b'"leg": "near", "contract": "TMFH6", "side": "Sell", '
               b'"trade_id": "t1"}]')
    p.write_bytes(payload)
    result = normalize_sources([str(p)])
    assert result is not None
    import hashlib
    if isinstance(result, dict):
        assert result["sources"][0]["sha256"] == \
            hashlib.sha256(payload).hexdigest()


def test_provenance_preserved_in_adapted_records():
    raw = {"fill_type": "fill", "timestamp": "2026-08-08T10:00:02.000",
           "leg": "near", "contract": "TMFH6", "side": "Sell",
           "trade_id": "t1"}
    fill = adapt_fill(raw, CTX)
    assert fill["source"] == "fills.json"
    assert fill["record_no"] == 3 and fill["byte_offset"] == 128
    assert fill["byte_hash"] == "a" * 64
    assert fill["ts_text"] == "2026-08-08T10:00:02.000"
    assert fill["unit"] == "epoch_ms"


# ── anchors / joins ──────────────────────────────────────────────────────────

def test_join_anchor_legal_same_trade():
    fills = [{"trade_id": "t1", "leg": "near", "side": "Buy"}]
    events = [{"trade_id": "t1", "kind": "RELEASE_DECISION",
               "order_id": "o1"}]
    anchor = join_anchor(fills, events)
    assert anchor is not None


def test_join_anchor_ambiguous_rejected():
    # an event with NO matching fill trade cannot anchor
    fills = [{"trade_id": "t1", "leg": "near", "side": "Buy"}]
    events = [{"trade_id": "t2", "kind": "RELEASE_DECISION",
               "order_id": "o1"}]
    result = join_anchor(fills, events)
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result


def test_join_positions_per_leg():
    fills = [{"trade_id": "t1", "leg": "near", "side": "Buy"},
             {"trade_id": "t1", "leg": "far", "side": "Sell"}]
    pos = join_positions(fills, "t1")
    assert pos is not None
    assert pos == {"near": "LONG", "far": "SHORT"}, pos


def test_join_positions_missing_leg_not_available():
    fills = [{"trade_id": "t1", "leg": "near", "side": "Buy"}]
    result = join_positions(fills, "t1")
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result


# ── refusals ─────────────────────────────────────────────────────────────────

def test_malformed_record_refused():
    result = normalize_sources(["/nonexistent/torn.json"])
    assert isinstance(result, tuple) and result[0] == "REFUSED", result


def test_unsupported_raw_tick_csv_refused(tmp_path):
    p = tmp_path / "ticks.csv"
    p.write_text("ts,price,qty\n1,44300,2\n", encoding="utf-8")
    result = normalize_sources([str(p)])
    assert isinstance(result, tuple) and result[0] == "REFUSED", result
    assert "csv" in result[1].lower(), result


def test_no_bbo_period_explicit_not_available(tmp_path):
    # a decision window with NO BBO records must be explicit NOT_AVAILABLE
    result = build_normalized_snapshot([], str(tmp_path / "out"))
    assert result is not None
    assert not (tmp_path / "out" / "events.json").exists() or \
        result[0] in ("REFUSED", "NOT_AVAILABLE")


def test_output_no_replace_atomic(tmp_path):
    (tmp_path / "events.json").write_text("OLD", encoding="utf-8")
    result = build_normalized_snapshot([], str(tmp_path))
    assert isinstance(result, tuple) and result[0] == "REFUSED", result
    assert (tmp_path / "events.json").read_text(encoding="utf-8") == "OLD"
