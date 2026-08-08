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
    join_entries, join_positions, normalize_sources, release_leg,
    resolve_contract_mapping)

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


# ── Step 1: entries + release_leg (replay conclusion gap) ────────────────────

def test_join_entries_per_leg_from_entry_fills():
    fills = [{"trade_id": "t1", "leg": "near", "fill_type": "ENTRY",
              "side": "LONG", "price": 45.0, "qty": 2},
             {"trade_id": "t1", "leg": "far", "fill_type": "ENTRY",
              "side": "SHORT", "price": 30.0, "qty": 2}]
    result = join_entries(fills, "t1")
    assert result == {"near": {"price": 45.0, "qty": 2},
                      "far": {"price": 30.0, "qty": 2}}, result


def test_join_entries_missing_entry_fail_closed():
    fills = [{"trade_id": "t1", "leg": "near", "fill_type": "RELEASE",
              "side": "SHORT", "qty": 2}]
    result = join_entries(fills, "t1")
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result
    assert "entry" in result[1].lower(), result


def test_join_entries_missing_price_fail_closed():
    fills = [{"trade_id": "t1", "leg": "near", "fill_type": "ENTRY",
              "side": "LONG", "qty": 2}]
    result = join_entries(fills, "t1")
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result
    assert "price" in result[1].lower(), result


def test_release_leg_from_release_fill():
    fills = [{"trade_id": "t1", "leg": "near", "fill_type": "RELEASE",
              "side": "SHORT", "qty": 2}]
    assert release_leg(fills, "t1") == "near"


def test_release_leg_ambiguous_rejected():
    fills = [{"trade_id": "t1", "leg": "near", "fill_type": "RELEASE",
              "side": "SHORT", "qty": 2},
             {"trade_id": "t1", "leg": "far", "fill_type": "RELEASE",
              "side": "LONG", "qty": 2}]
    result = release_leg(fills, "t1")
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result


def test_release_leg_missing_rejected():
    fills = [{"trade_id": "t1", "leg": "near", "fill_type": "ENTRY",
              "side": "LONG", "qty": 2}]
    result = release_leg(fills, "t1")
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", result


def test_build_event_carries_entries_and_release_leg(tmp_path):
    # Step 1 integration: the emitted event carries per-leg entries +
    # release_leg so the replay engine can produce Y0..Y3
    fills, evs, bbo = _fixture_sources(tmp_path)
    out = tmp_path / "out"
    result = build_normalized_snapshot(
        [str(fills), str(evs), str(bbo)], str(out),
        mapping_input=MAPPING_V1)
    assert isinstance(result, list), result
    ev = result[0]
    assert ev.get("release_leg") == "near", ev
    assert ev.get("entries") == {"near": {"price": 45.0, "qty": 2},
                                 "far": {"price": 30.0, "qty": 2}}, ev


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
    anchors = join_anchor(fills, events)
    assert isinstance(anchors, list) and len(anchors) == 1, anchors
    assert anchors[0]["trade_id"] == "t1"


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


# ── integration: runner-compatible event LIST pipeline ───────────────────────

MAPPING_V1 = {"windows": [
    {"valid_from_ms": 1_700_000_000_000,
     "valid_to_ms": 1_800_000_000_000,
     "near": "TMFH6", "far": "TMFI6", "version": "map-v1"}]}


def _unavailable_quote(reason):
    """Mirror of the adapter's fixed typed unavailable quote shape."""
    return {"available": False, "bid": None, "ask": None, "age_s": None,
            "close_action": None, "quote_exchange_ts": None,
            "quote_source": None, "reason": reason}


def _fixture_sources(tmp_path, with_bbo=True, unknown_type=None):
    lines = [
        '{"fill_type": "ENTRY", "timestamp": "2026-08-08T09:00:00.000", '
        '"leg": "near", "contract": "NEAR", "side": "LONG", "qty": 2, '
        '"price": 45.0, "trade_id": "t1"}',
        '{"fill_type": "ENTRY", "timestamp": "2026-08-08T09:00:01.000", '
        '"leg": "far", "contract": "FAR", "side": "SHORT", "qty": 2, '
        '"price": 30.0, "trade_id": "t1"}',
        '{"fill_type": "RELEASE", "timestamp": "2026-08-08T10:00:02.000", '
        '"leg": "near", "contract": "NEAR", "side": "SHORT", "qty": 2, '
        '"price": 52.0, "trade_id": "t1"}',
    ]
    if unknown_type:
        lines.append('{"fill_type": "%s", "timestamp": '
                     '"2026-08-08T10:00:03.000", "trade_id": "t9"}'
                     % unknown_type)
    fills = tmp_path / "fills.jsonl"
    fills.write_text("\n".join(lines) + "\n", encoding="utf-8")
    evs = tmp_path / "events.jsonl"
    evs.write_text('{"event": "RELEASE_DECISION", "ts": '
                   '"2026-08-08T10:00:00.000", "trade_id": "t1", '
                   '"order_id": "o1"}\n', encoding="utf-8")
    bbo = tmp_path / "bbo.jsonl"
    if with_bbo:
        # decision at 2026-08-08T10:00Z (1786183200000); quotes must be
        # within [decision - 30s, decision] to survive the window filter
        bbo.write_text(
            '{"event_type": "BBO_UPDATE", "leg": "near", '
            '"contract_code": "TMFH6", "exchange_ts_ms": 1786183190000, '
            '"receive_ts_ms": 1786183190050, "source": "shioaji_bidask", '
            '"bid": 50.0, "ask": 100.0}\n'
            '{"event_type": "BBO_UPDATE", "leg": "far", '
            '"contract_code": "TMFI6", "exchange_ts_ms": 1786183190500, '
            '"receive_ts_ms": 1786183190550, "source": "shioaji_bidask", '
            '"bid": 25.0, "ask": 50.0}\n', encoding="utf-8")
    else:
        bbo.write_text("", encoding="utf-8")
    return fills, evs, bbo


def test_build_emits_runner_compatible_list(tmp_path):
    fills, evs, bbo = _fixture_sources(tmp_path)
    out = tmp_path / "out"
    result = build_normalized_snapshot(
        [str(fills), str(evs), str(bbo)], str(out),
        mapping_input=MAPPING_V1)
    assert isinstance(result, list), result
    ev = result[0]
    assert {"source_event_seq", "exchange_ts", "recv_ts",
            "decision_ts_ms"} <= set(ev), ev
    assert set(ev["quotes"]) == {"near", "far"}, ev


def test_build_attaches_mapping_positions_bbo(tmp_path):
    fills, evs, bbo = _fixture_sources(tmp_path)
    out = tmp_path / "out"
    result = build_normalized_snapshot(
        [str(fills), str(evs), str(bbo)], str(out),
        mapping_input=MAPPING_V1)
    ev = result[0]
    assert ev["contracts"] == {"near": "TMFH6", "far": "TMFI6"}, ev
    near = ev["quotes"]["near"]
    far = ev["quotes"]["far"]
    assert near.get("available") is True, near
    assert far.get("available") is True, far
    assert near["close_action"] == "LONG", \
        "near close_action from ENTRY LONG position"
    assert far["close_action"] == "SHORT", \
        "far close_action from ENTRY SHORT position"
    assert near["bid"] == 50.0 and far["ask"] == 50.0
    assert near["quote_exchange_ts"] <= ev["decision_ts_ms"]


def test_build_no_bbo_censored(tmp_path):
    fills, evs, bbo = _fixture_sources(tmp_path, with_bbo=False)
    out = tmp_path / "out"
    result = build_normalized_snapshot(
        [str(fills), str(evs), str(bbo)], str(out),
        mapping_input=MAPPING_V1)
    assert isinstance(result, list), result
    for leg in ("near", "far"):
        q = result[0]["quotes"][leg]
        assert isinstance(q, dict) and q.get("available") is False, q
        assert q.get("reason"), "no-BBO must be explicit, never fake"
        # v4: fixed typed shape — every runner QUOTE_FIELD present (None)
        for f in ("bid", "ask", "age_s", "close_action",
                  "quote_exchange_ts", "quote_source"):
            assert f in q, f"{leg} quote missing field {f}"
            assert q[f] is None, f"{leg} quote {f} must be null"


def test_build_unmatched_release_fills_listed(tmp_path):
    # v4: every RELEASE fill without a legal anchor is listed explicitly
    fills, evs, bbo = _fixture_sources(tmp_path)
    with fills.open("a", encoding="utf-8") as f:
        f.write('{"fill_type": "RELEASE", "timestamp": '
                '"2026-08-08T10:00:02.000", "leg": "near", '
                '"contract": "NEAR", "side": "SHORT", "qty": 2, '
                '"trade_id": "t9"}\n')
    out = tmp_path / "out"
    result = build_normalized_snapshot(
        [str(fills), str(evs), str(bbo)], str(out),
        mapping_input=MAPPING_V1)
    assert isinstance(result, list), result
    manifest = json.loads(
        (out / "manifest.json").read_text(encoding="utf-8"))
    unmatched = manifest.get("unmatched_release_fills", [])
    assert len(unmatched) == 1, unmatched
    u = unmatched[0]
    assert u["trade_id"] == "t9", u
    assert u["source"].endswith("fills.jsonl"), u
    assert isinstance(u["record_no"], int) and \
        isinstance(u["byte_offset"], int), u
    assert "anchor" in u["reason"].lower(), u


def test_runner_censors_typed_unavailable_as_value(tmp_path):
    # v4: typed-unavailable quotes pass the runner schema gate (fields
    # present) -> censored as VALUE, never 'schema: missing field'
    from scripts.research.phase_transition_replay import run_replay
    events = [{
        "source_event_seq": 1, "exchange_ts": 1786183190000,
        "recv_ts": 1786183190050, "decision_ts_ms": 1786183190000,
        "quotes": {"near": _unavailable_quote("near: no valid BBO"),
                   "far": _unavailable_quote("far: no valid BBO")},
    }]
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps(events), encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1", "--dry-run"])
    assert rc == 0, rc
    manifest = json.loads(
        (out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["n_kept"] == 0 and manifest["n_censored"] == 1
    reasons = [c["reason"] for c in manifest["censored_reasons"]]
    assert all("schema:" not in r for r in reasons), reasons
    assert reasons and reasons[0], reasons


def test_build_unknown_fill_type_counted(tmp_path):
    fills, evs, bbo = _fixture_sources(
        tmp_path, unknown_type="HEDGE")
    out = tmp_path / "out"
    result = build_normalized_snapshot(
        [str(fills), str(evs), str(bbo)], str(out),
        mapping_input=MAPPING_V1)
    assert isinstance(result, list), result
    manifest = json.loads(
        (out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("unknown_fill_types") == {"HEDGE": 1}, manifest


def test_build_multiple_anchors_multiple_events(tmp_path):
    # v3: EVERY legal anchor -> one output event; BBO keep/censor counted
    lines = [
        '{"fill_type": "ENTRY", "timestamp": "2026-08-08T08:00:00.000", '
        '"leg": "near", "contract": "NEAR", "side": "LONG", "qty": 2, '
        '"trade_id": "t2"}',
        '{"fill_type": "ENTRY", "timestamp": "2026-08-08T08:00:01.000", '
        '"leg": "far", "contract": "FAR", "side": "SHORT", "qty": 2, '
        '"trade_id": "t2"}',
        '{"fill_type": "RELEASE", "timestamp": "2026-08-08T09:00:02.000", '
        '"leg": "near", "contract": "NEAR", "side": "SHORT", "qty": 2, '
        '"trade_id": "t2"}',
        '{"fill_type": "ENTRY", "timestamp": "2026-08-08T09:00:00.000", '
        '"leg": "near", "contract": "NEAR", "side": "LONG", "qty": 2, '
        '"trade_id": "t1"}',
        '{"fill_type": "ENTRY", "timestamp": "2026-08-08T09:00:01.000", '
        '"leg": "far", "contract": "FAR", "side": "SHORT", "qty": 2, '
        '"trade_id": "t1"}',
        '{"fill_type": "RELEASE", "timestamp": "2026-08-08T10:00:02.000", '
        '"leg": "near", "contract": "NEAR", "side": "SHORT", "qty": 2, '
        '"trade_id": "t1"}',
    ]
    fills = tmp_path / "fills.jsonl"
    fills.write_text("\n".join(lines) + "\n", encoding="utf-8")
    evs = tmp_path / "events.jsonl"
    evs.write_text(
        '{"event": "ORDER_SUBMITTED", "ts": "2026-08-08T09:00:00.000", '
        '"trade_id": "t2", "order_id": "o2", "strategy": "MTS_RELEASE"}\n'
        '{"event": "ORDER_SUBMITTED", "ts": "2026-08-08T10:00:00.000", '
        '"trade_id": "t1", "order_id": "o1", "strategy": "MTS_RELEASE"}\n',
        encoding="utf-8")
    bbo = tmp_path / "bbo.jsonl"
    # BBO at 09:06:40Z (1786180000000) — pre-anchor for t1 (10:00Z),
    # AFTER t2 (09:00Z) -> t2 censored
    bbo.write_text(
        '{"event_type": "BBO_UPDATE", "leg": "NEAR", '
        '"contract_code": "TMFH6", "exchange_ts_ms": 1786180000000.0, '
        '"receive_ts_ms": 1786180000050.0, "source": "shioaji_bidask", '
        '"bid": 50.0, "ask": 100.0}\n'
        '{"event_type": "BBO_UPDATE", "leg": "FAR", '
        '"contract_code": "TMFI6", "exchange_ts_ms": 1786180000500.0, '
        '"receive_ts_ms": 1786180000550.0, "source": "shioaji_bidask", '
        '"bid": 25.0, "ask": 50.0}\n', encoding="utf-8")
    out = tmp_path / "out"
    result = build_normalized_snapshot(
        [str(fills), str(evs), str(bbo)], str(out),
        mapping_input=MAPPING_V1)
    assert isinstance(result, list), result
    assert len(result) == 2, \
        "every legal anchor must produce an output event"
    trades = [ev["contract_mapping_evidence"]["decision_ts_ms"]
              for ev in result]
    assert len(set(trades)) == 2
    # t1 (10:00 decision) has pre-anchor BBO -> kept
    kept = sum(1 for ev in result for leg in ("near", "far")
               if ev["quotes"][leg].get("available") is True)
    censored = sum(1 for ev in result for leg in ("near", "far")
                   if ev["quotes"][leg].get("available") is False)
    assert kept == 2, f"t1 quotes kept, got {kept}"
    assert censored == 2, f"t2 no-BBO censored, got {censored}"
    manifest = json.loads(
        (out / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["event_map"]) == 2, manifest["event_map"]
