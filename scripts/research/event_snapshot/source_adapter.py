"""Runtime source adapter — implementation (research-only).

Converts the ACTUAL runtime schemas into builder-native normalized records:
- fills: fill_type/timestamp/leg/contract/side/trade_id
- spread events: event/ts/trade_id/order_id (RELEASE_DECISION/SUBMISSION
  are the only legal anchors)
- BBO telemetry: event_type=BBO_UPDATE/leg/contract_code/exchange_ts_ms/
  receive_ts_ms/source/bid/ask — source=shioaji_bidask allowlist ONLY

No last-price/OHLC conversion, no raw tick CSV, no ambiguous joins.
Read-once + hash-bound provenance. Exclusive no-replace atomic output.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

from scripts.research.event_snapshot.builder import (
    _cleanup, _exclusive_out_dir, _write_no_replace)

BBO_SOURCE_ALLOWLIST = ("shioaji_bidask",)
ANCHOR_EVENT_KINDS = ("RELEASE_DECISION", "SUBMISSION")
FILL_TYPES = ("fill",)
SIDE_MAP = {"Buy": "LONG", "Sell": "SHORT"}
NORMALIZED_SCHEMA = "normalized-snapshot-v1"


def _parse_ts_text(ts_text):
    if ts_text is None:
        return None
    try:
        dt = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _prov(source_ctx):
    return {"source": source_ctx.get("source"),
            "record_no": source_ctx.get("record_no"),
            "byte_offset": source_ctx.get("byte_offset"),
            "byte_hash": (source_ctx.get("byte_hash")
                          or source_ctx.get("sha256"))}


# ── record adapters ──────────────────────────────────────────────────────────

def adapt_fill(raw, source_ctx):
    """fills runtime record -> normalized fill. Malformed -> NOT_AVAILABLE."""
    if not isinstance(raw, dict) or raw.get("fill_type") not in FILL_TYPES:
        return ("NOT_AVAILABLE",
                f"fill_type unsupported: {raw.get('fill_type')!r}")
    return {
        "trade_id": raw.get("trade_id"),
        "leg": raw.get("leg"),
        "contract": raw.get("contract"),
        "side": raw.get("side"),
        "ts_text": raw.get("timestamp"),
        "parsed_ts": _parse_ts_text(raw.get("timestamp")),
        "unit": "epoch_ms",
        "source": source_ctx.get("source"),
        "record_no": source_ctx.get("record_no"),
        "byte_offset": source_ctx.get("byte_offset"),
        "byte_hash": (source_ctx.get("byte_hash")
                      or source_ctx.get("sha256")),
    }


def adapt_spread_event(raw, source_ctx):
    """spread events runtime record -> normalized event. Only
    RELEASE_DECISION/SUBMISSION are legal anchors; anything else ->
    NOT_AVAILABLE."""
    kind = raw.get("event") if isinstance(raw, dict) else None
    if kind not in ANCHOR_EVENT_KINDS:
        return ("NOT_AVAILABLE",
                f"event kind {kind!r} is not a legal anchor")
    return {
        "trade_id": raw.get("trade_id"),
        "order_id": raw.get("order_id"),
        "kind": kind,
        "ts_text": raw.get("ts"),
        "parsed_ts": _parse_ts_text(raw.get("ts")),
        "unit": "epoch_ms",
        "provenance": _prov(source_ctx),
    }


def adapt_bbo(raw, source_ctx):
    """BBO telemetry -> normalized quote. source=shioaji_bidask ONLY —
    no last-price/OHLC conversion."""
    if not isinstance(raw, dict) or raw.get("event_type") != "BBO_UPDATE":
        return ("NOT_AVAILABLE",
                f"event_type {raw.get('event_type')!r} — no last-price/OHLC "
                f"conversion")
    if raw.get("source") not in BBO_SOURCE_ALLOWLIST:
        return ("NOT_AVAILABLE",
                f"source {raw.get('source')!r} not in allowlist "
                f"{BBO_SOURCE_ALLOWLIST}")
    return {
        "leg": raw.get("leg"),
        "contract": raw.get("contract_code"),
        "exchange_ts_ms": raw.get("exchange_ts_ms"),
        "receive_ts_ms": raw.get("receive_ts_ms"),
        "source": raw.get("source"),
        "bid": raw.get("bid"),
        "ask": raw.get("ask"),
        "provenance": _prov(source_ctx),
    }


# ── read-once normalization ──────────────────────────────────────────────────

def _classify(record):
    if isinstance(record, dict) and "fill_type" in record:
        return "fills"
    if isinstance(record, dict) and "event" in record and "ts" in record:
        return "events"
    if isinstance(record, dict) and record.get("event_type") == "BBO_UPDATE":
        return "quotes"
    return None


def normalize_sources(input_paths):
    """Read each source ONCE + hash exact bytes; adapt every record.
    Returns {"fills", "events", "quotes", "sources"} — malformed/torn
    input or unsupported raw tick CSV -> ("REFUSED", reason)."""
    fills, events, quotes = [], [], []
    sources = []
    for path in input_paths or []:
        if str(path).lower().endswith(".csv"):
            return ("REFUSED", f"unsupported raw tick CSV: {path}")
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            return ("REFUSED", str(e))
        sha = hashlib.sha256(data).hexdigest()
        try:
            records = json.loads(data)
        except ValueError as e:
            return ("REFUSED", f"malformed/torn input: {path}: {e}")
        if not isinstance(records, list):
            return ("REFUSED", f"input must be a JSON list: {path}")
        base = {"source": os.path.basename(path), "sha256": sha}
        for i, rec in enumerate(records):
            kind = _classify(rec)
            if kind is None:
                return ("REFUSED",
                        f"unclassifiable record {i} in {path}: {rec!r}")
            ctx = dict(base, record_no=i, byte_offset=i, byte_hash=sha)
            if kind == "fills":
                r = adapt_fill(rec, ctx)
            elif kind == "events":
                r = adapt_spread_event(rec, ctx)
            else:
                r = adapt_bbo(rec, ctx)
            if isinstance(r, tuple) and r[0] == "NOT_AVAILABLE":
                return ("REFUSED", f"{path}[{i}]: {r[1]}")
            {"fills": fills, "events": events,
             "quotes": quotes}[kind].append(r)
        sources.append({"path": os.path.basename(path), "sha256": sha,
                        "record_count": len(records)})
    return {"fills": fills, "events": events, "quotes": quotes,
            "sources": sources}


# ── anchors / joins ──────────────────────────────────────────────────────────

def join_anchor(fills, events):
    """Legal same-trade anchor: a RELEASE_DECISION/SUBMISSION event joined
    to its trade's release fills. Ambiguous/missing -> ("NOT_AVAILABLE",
    reason) — never synthesized."""
    fill_trades = {f.get("trade_id") for f in (fills or [])
                   if f.get("trade_id")}
    legal = [e for e in (events or [])
             if e.get("kind") in ANCHOR_EVENT_KINDS]
    matches = [e for e in legal if e.get("trade_id") in fill_trades]
    if not matches:
        return ("NOT_AVAILABLE",
                "no legal same-trade anchor (event trade not joined by "
                "release fills)")
    if len({m.get("trade_id") for m in matches}) > 1:
        return ("NOT_AVAILABLE",
                f"ambiguous anchor: events span multiple trades "
                f"{sorted({m.get('trade_id') for m in matches})}")
    return matches[0]


def join_positions(fills, trade_id):
    """Per-leg pre-decision position mapping from the trade's release
    fills: {"near": side, "far": side}. Missing leg -> NOT_AVAILABLE."""
    legs = {}
    for f in (fills or []):
        if f.get("trade_id") == trade_id and f.get("leg") in ("near", "far"):
            side = SIDE_MAP.get(f.get("side"))
            if side is not None:
                legs[f["leg"]] = side
    if set(legs) != {"near", "far"}:
        return ("NOT_AVAILABLE",
                f"per-leg position incomplete: {sorted(legs)}")
    return {"near": legs["near"], "far": legs["far"]}


# ── normalized snapshot build ────────────────────────────────────────────────

def _attach_bbo(quotes, anchor_ts, positions, contract):
    out = {}
    for leg in ("near", "far"):
        best = None
        for q in quotes or []:
            if q.get("leg") != leg:
                continue
            if q.get("contract") != contract:
                continue
            t = q.get("exchange_ts_ms")
            if not (isinstance(t, int) and t > 0) or t > anchor_ts:
                continue
            if best is None or t > best["exchange_ts_ms"]:
                best = q
        if best is None:
            out[leg] = {"available": False,
                        "reason": f"{leg}: no BBO in decision window"}
        else:
            out[leg] = {"available": True, "bid": best["bid"],
                        "ask": best["ask"],
                        "exchange_ts_ms": best["exchange_ts_ms"],
                        "receive_ts_ms": best["receive_ts_ms"],
                        "source": best["source"],
                        "close_action": positions[leg]}
    return out


def build_normalized_snapshot(input_paths, out_dir):
    """Normalized snapshot (builder-native) + manifest with exclusive
    no-replace atomic writes. Existing out-dir/race -> ("REFUSED", reason)
    — zero partial output."""
    try:
        out_dir = _exclusive_out_dir(out_dir)
    except FileExistsError as e:
        return ("REFUSED", str(e))
    norm = normalize_sources(input_paths)
    if isinstance(norm, tuple) and norm[0] == "REFUSED":
        _cleanup(out_dir, [])
        return norm
    fills, events, quotes = norm["fills"], norm["events"], norm["quotes"]
    anchor = join_anchor(fills, events)
    if isinstance(anchor, tuple) and anchor[0] == "NOT_AVAILABLE":
        _cleanup(out_dir, [])
        return anchor
    positions = join_positions(fills, anchor.get("trade_id"))
    if isinstance(positions, tuple) and positions[0] == "NOT_AVAILABLE":
        _cleanup(out_dir, [])
        return positions
    anchor_ts = _parse_ts_text(anchor.get("ts_text"))
    if anchor_ts is None:
        _cleanup(out_dir, [])
        return ("REFUSED",
                f"invalid anchor timestamp: {anchor.get('ts_text')!r}")
    contract = fills[0].get("contract") if fills else None
    prov = anchor.get("provenance") or {}
    events_out = [{
        "source_event_seq": 1,
        "exchange_ts": anchor_ts,
        "recv_ts": anchor_ts,
        "decision_ts_ms": anchor_ts,
        "decision_ts_text": anchor.get("ts_text"),
        "contract": contract,
        "position": positions,
        "byte_offset": prov.get("byte_offset"),
        "record_no": prov.get("record_no"),
        "byte_hash": prov.get("byte_hash"),
        "quotes": _attach_bbo(quotes, anchor_ts, positions, contract),
    }]
    manifest = {
        "schema_version": NORMALIZED_SCHEMA,
        "sources": norm["sources"],
        "event_map": {
            "1": {"anchor": anchor.get("kind"), "trade_id":
                  anchor.get("trade_id"), "order_id": anchor.get("order_id"),
                  "decision_ts_text": anchor.get("ts_text")}},
    }
    events_payload = json.dumps(events_out, indent=2, sort_keys=True,
                                ensure_ascii=False)
    manifest_payload = json.dumps(manifest, indent=2, sort_keys=True,
                                  ensure_ascii=False)
    created = []
    try:
        created.append(_write_no_replace(out_dir, "events.json",
                                         events_payload))
        created.append(_write_no_replace(out_dir, "manifest.json",
                                         manifest_payload))
    except (OSError, FileExistsError) as e:
        _cleanup(out_dir, created)
        return ("REFUSED", str(e))
    return events_out
