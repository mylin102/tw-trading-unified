"""Reconciled event snapshot builder — implementation (research-only).

Six P0 contracts (codex v2 acceptance) preserved:
1. legal same-trade decision anchor — RELEASE fills never supply decision_ts
2. byte/record provenance + deterministic ties by record identity
3. executable BBO feed allowlist + typed per-leg unavailable
4. pre-anchor exact contract/leg/close-side validation
5. exclusive no-overwrite atomic events+manifest output
6. read-once hash binding; malformed -> REFUSED; no lookahead
"""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone

SCHEMA_VERSION = "event-snapshot-v1"
SOURCE_KINDS = ("fills", "events", "quotes")
EXECUTABLE_QUOTE_FEEDS = ("bbo_near", "bbo_far")
DECISION_KINDS = ("RELEASE_DECISION", "SUBMISSION")
ANCHOR_TS_UNIT = "epoch_ms"


# ── input: read-once + hash binding (TOCTOU) ─────────────────────────────────

def read_source_once(path):
    """Read one source file exactly once; return (records, sha256) over the
    SAME parsed bytes."""
    with open(path, "rb") as f:
        data = f.read()
    sha = hashlib.sha256(data).hexdigest()
    try:
        records = json.loads(data)
    except ValueError as e:
        raise ValueError(f"input artifact unparseable: {path}: {e}") from e
    if not isinstance(records, list):
        raise ValueError(f"input artifact must be a JSON list: {path}")
    return records, sha


# ── legal decision anchors (P0-1) ────────────────────────────────────────────

def _record_trades(records):
    return {r.get("trade_id") for r in (records or []) if r.get("trade_id")}


def legal_anchor(records):
    """The legal decision anchor: a timestamped release-decision/submission
    record joined to the SAME trade's fills. RELEASE fills are post-decision
    and never supply decision_ts. No legal anchor -> ("NOT_AVAILABLE",
    reason) — never synthesized."""
    records = list(records or [])
    decisions = [r for r in records
                 if r.get("kind") in DECISION_KINDS]
    if not decisions:
        return ("NOT_AVAILABLE",
                "no release-decision/submission record (RELEASE fill is "
                "post-decision and never supplies decision_ts)")
    fills = [r for r in records if r.get("kind") == "RELEASE"]
    fill_trades = _record_trades(fills)
    if not fill_trades:
        return ("NOT_AVAILABLE", "no trade join: no RELEASE fill records")
    for dec in decisions:
        if dec.get("trade_id") in fill_trades:
            if not dec.get("ts_text") and not dec.get("exchange_ts"):
                return ("NOT_AVAILABLE",
                        f"decision anchor untimestamped: {dec.get('record')}")
            return dec
    return ("NOT_AVAILABLE",
            f"decision anchor trade {_record_trades(decisions)} does not "
            f"join fills {fill_trades}")


# ── event-time provenance (P0-2) ─────────────────────────────────────────────

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


def attach_provenance(record):
    """Event-time provenance: byte hash + record number/byte offset +
    original timestamp text + parsed timestamp/unit/offset."""
    r = record or {}
    ts_text = r.get("ts_text")
    return {
        "byte_hash": (r.get("byte_hash")
                      or hashlib.sha256(
                          json.dumps(r, sort_keys=True, default=str)
                          .encode("utf-8")).hexdigest()),
        "record_no": r.get("record_no"),
        "byte_offset": r.get("byte_offset"),
        "ts_text": ts_text,
        "parsed_ts": _parse_ts_text(ts_text),
        "ts_unit": ANCHOR_TS_UNIT,
        "ts_offset": r.get("ts_offset", 0),
    }


# ── deterministic ordering (P0-2) ────────────────────────────────────────────

def _sort_key(e):
    return (e.get("exchange_ts"), e.get("source_event_seq"),
            e.get("byte_offset") or 0)


def order_events(events):
    """Deterministic total order by (exchange_ts, source_event_seq); EQUAL
    timestamps tie by source record identity (byte_offset), never file
    order. Duplicates -> ("DUPLICATE", reason)."""
    evs = list(events or [])
    seen = {}
    for e in evs:
        key = _sort_key(e)[:2] + ((e.get("byte_offset") or 0),)
        if key in seen:
            return ("DUPLICATE",
                    f"duplicate (exchange_ts, seq, byte_offset): {key}")
        seen[key] = e
    sorted_evs = sorted(evs, key=_sort_key)
    reordered = any(evs[i] is not sorted_evs[i] for i in range(len(evs)))
    if reordered:
        return {"ordered": sorted_evs, "reordered": True,
                "reason": "input order differs from (exchange_ts, seq)"}
    return sorted_evs


# ── BBO attach: allowlist + typed unavailable (P0-3/P0-4) ────────────────────

def _attach_leg(ev, leg, quote_records):
    anchor = ev.get("decision_ts_ms")
    contract = ev.get("contract")
    reasons = []
    best = None
    for q in quote_records or []:
        if q.get("leg") != leg:
            continue
        src = q.get("quote_source")
        if src not in EXECUTABLE_QUOTE_FEEDS:
            reasons.append(f"{leg}: feed {src!r} not executable")
            continue
        qc = q.get("contract")
        if contract and qc and qc != contract:
            reasons.append(f"{leg}: contract {qc!r} != {contract!r}")
            continue
        qt = q.get("quote_exchange_ts")
        if qt is None or not (isinstance(qt, int) and qt > 0):
            reasons.append(f"{leg}: quote_exchange_ts invalid: {qt!r}")
            continue
        if anchor is not None and qt > anchor:
            reasons.append(
                f"{leg}: quote ts {qt} after legal decision anchor {anchor}")
            continue
        if best is None or qt > (best.get("quote_exchange_ts") or 0):
            best = q
    if best is None:
        return {"available": False,
                "reason": "; ".join(reasons) or f"{leg}: no valid BBO"}
    return {"available": True, "bid": best.get("bid"), "ask": best.get("ask"),
            "age_s": best.get("age_s"),
            "close_action": best.get("close_action"),
            "quote_exchange_ts": best.get("quote_exchange_ts"),
            "quote_source": best.get("quote_source"),
            "contract": best.get("contract")}


def attach_quotes(events, quote_records):
    """Attach per-leg BBO from the EXECUTABLE_QUOTE_FEEDS allowlist, with
    pre-anchor contract/leg validation. Missing/invalid -> TYPED per-leg
    unavailable structure {"available": False, "reason"} — never a bare
    string, never a last/mark/OHLC price."""
    out = []
    for ev in events or []:
        ev = dict(ev)
        ev["quotes"] = {"near": _attach_leg(ev, "near", quote_records),
                        "far": _attach_leg(ev, "far", quote_records)}
        out.append(ev)
    return out


# ── output: exclusive no-overwrite + atomic (P0-5) ───────────────────────────

def _exclusive_out_dir(path):
    p = os.path.abspath(path)
    if os.path.exists(p):
        raise FileExistsError(f"out-dir already exists: {path}")
    os.makedirs(p)
    return p


def _write_no_replace(out_dir, name, payload):
    target = os.path.join(out_dir, name)
    fd, tmp_path = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp_path, target)  # no-replace: fails if target exists
        except FileExistsError:
            raise FileExistsError(
                f"output target already exists: {target}") from None
        os.unlink(tmp_path)
        dir_fd = os.open(out_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def emit_manifest(out_dir, events, sources):
    """Versioned manifest mapping every output event to source records and
    hashes. Written no-replace (atomic); returns the manifest dict."""
    event_map = {}
    for i, ev in enumerate(events or []):
        event_map[str(ev.get("source_event_seq"))] = {
            "event_index": i,
            "event_source": ev.get("event_source"),
            "provenance": ev.get("provenance"),
        }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "sources": [{"path": s.get("path"), "sha256": s.get("sha256")}
                    for s in (sources or [])],
        "event_map": event_map,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _write_no_replace(out_dir, "manifest.json",
                          json.dumps(manifest, indent=2, sort_keys=True,
                                     ensure_ascii=False))
    except FileExistsError as e:
        raise FileExistsError(f"manifest target already exists: {e}") from e
    return manifest


# ── build entry ──────────────────────────────────────────────────────────────

def _merge_events(sources):
    """Merge fills/events/quotes into anchored events (P0-1).

    No records at all -> ([], None) — no candidates, emit an empty list.
    Records present but no legal anchor -> (None, reason) — REFUSED."""
    all_records = []
    for s in sources or []:
        for r in s.get("records", []):
            r = dict(r)
            r["event_source"] = s["path"]
            all_records.append(r)
    if not all_records:
        return [], None
    anchor = legal_anchor(all_records)
    if isinstance(anchor, tuple) and anchor[0] == "NOT_AVAILABLE":
        return None, anchor[1]
    anchor_ts = (anchor.get("parsed_ts")
                 or anchor.get("exchange_ts") or anchor.get("ts"))
    ev = {"source_event_seq": anchor.get("source_event_seq") or 1,
          "exchange_ts": anchor.get("exchange_ts"),
          "recv_ts": anchor.get("recv_ts"),
          "decision_ts_ms": anchor_ts,
          "contract": anchor.get("contract"),
          "event_source": anchor.get("event_source"),
          "provenance": attach_provenance(anchor),
          "anchor_record": anchor.get("record")}
    return [ev], None


def build_snapshot(input_paths, out_dir):
    """Build events.json (versioned list, run_replay-compatible) +
    manifest.json from the named immutable byte snapshots.

    Returns the events LIST on success; ("REFUSED", reason) on refusal
    (existing out-dir, malformed/torn input, no legal anchor) — never a
    partial output."""
    try:
        out_dir = _exclusive_out_dir(out_dir)
    except FileExistsError as e:
        return ("REFUSED", str(e))
    sources = []
    try:
        for path in input_paths:
            records, sha = read_source_once(path)
            sources.append({"path": path, "sha256": sha, "records": records})
    except (OSError, ValueError) as e:
        return ("REFUSED", str(e))
    events, reason = _merge_events(sources)
    if events is None:
        return ("REFUSED", reason)
    ordered = order_events(events)
    if isinstance(ordered, tuple) and ordered[0] == "DUPLICATE":
        return ("REFUSED", ordered[1])
    events = ordered if isinstance(ordered, list) else ordered.get("ordered")
    quote_records = [r for s in sources for r in s.get("records", [])
                     if r.get("leg")]
    events = attach_quotes(events, quote_records)
    try:
        _write_no_replace(out_dir, "events.json",
                          json.dumps(events, indent=2, sort_keys=True,
                                     ensure_ascii=False))
        emit_manifest(out_dir, events, sources)
    except FileExistsError as e:
        return ("REFUSED", str(e))
    return events
