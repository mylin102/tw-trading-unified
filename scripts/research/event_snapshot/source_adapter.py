"""Runtime source adapter — implementation (research-only).

Converts ACTUAL runtime schemas (JSONL):
- fills: fill_type {ENTRY,RELEASE,EXIT,COMBINED_EXIT}; ENTRY LONG/SHORT
- spread events: event/ts/trade_id/order_id (RELEASE_DECISION/SUBMISSION)
- BBO: event_type=BBO_UPDATE, contract_code=ACTUAL code, source allowlist
Contract mapping is per-decision-window (roll-safe), never hardcoded.
"""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone

BBO_SOURCE_ALLOWLIST = ("shioaji_bidask",)
ANCHOR_EVENT_KINDS = ("RELEASE_DECISION", "SUBMISSION")
FILL_TYPES = ("ENTRY", "RELEASE", "EXIT", "COMBINED_EXIT")


def _parse_ts(ts_text):
    if ts_text is None:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_text).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _ctx(source, sha, record_no, byte_offset):
    return {"source": source, "sha256": sha, "record_no": record_no,
            "byte_offset": byte_offset, "byte_hash": sha}


def adapt_fill(raw, source_ctx):
    """fills JSONL record -> normalized fill. fill_type must be a known
    enum; ENTRY sides LONG/SHORT; qty > 0. contract is the NEAR/FAR LABEL."""
    ft = raw.get("fill_type")
    if ft not in FILL_TYPES:
        return ("NOT_AVAILABLE", f"unknown fill_type {ft!r}")
    side = raw.get("side")
    if ft == "ENTRY" and side not in ("LONG", "SHORT"):
        return ("NOT_AVAILABLE", f"ENTRY side {side!r} not LONG/SHORT")
    qty = raw.get("qty")
    if qty is not None and (not isinstance(qty, (int, float)) or qty <= 0):
        return ("NOT_AVAILABLE", f"invalid qty {qty!r}")
    ts_text = raw.get("timestamp")
    return {"fill_type": ft, "trade_id": raw.get("trade_id"),
            "leg": raw.get("leg"), "contract": raw.get("contract"),
            "side": side, "qty": qty, "ts_text": ts_text,
            "parsed_ts": _parse_ts(ts_text), "unit": "epoch_ms",
            **source_ctx}


def adapt_spread_event(raw, source_ctx):
    """spread events JSONL record -> normalized event. Only legal anchor
    kinds are accepted."""
    kind = raw.get("event")
    if kind not in ANCHOR_EVENT_KINDS:
        return ("NOT_AVAILABLE", f"event {kind!r} not a legal anchor kind")
    ts_text = raw.get("ts")
    return {"kind": kind, "trade_id": raw.get("trade_id"),
            "order_id": raw.get("order_id"), "ts_text": ts_text,
            "parsed_ts": _parse_ts(ts_text), "unit": "epoch_ms",
            **source_ctx}


def adapt_bbo(raw, source_ctx):
    """BBO telemetry -> normalized quote with the ACTUAL contract code.
    source=shioaji_bidask ONLY; no last-price/OHLC conversion."""
    if raw.get("event_type") != "BBO_UPDATE":
        return ("NOT_AVAILABLE",
                "not a BBO_UPDATE record (no last-price/OHLC conversion)")
    src = raw.get("source")
    if src not in BBO_SOURCE_ALLOWLIST:
        return ("NOT_AVAILABLE", f"source {src!r} not in allowlist")
    return {"leg": raw.get("leg"), "contract": raw.get("contract_code"),
            "exchange_ts_ms": raw.get("exchange_ts_ms"),
            "receive_ts_ms": raw.get("receive_ts_ms"),
            "source": src, "bid": raw.get("bid"), "ask": raw.get("ask"),
            **source_ctx}


def _parse_jsonl(path):
    """Read bytes ONCE; parse each line with its ACTUAL byte offset."""
    with open(path, "rb") as f:
        data = f.read()
    sha = hashlib.sha256(data).hexdigest()
    offset = 0
    records = []
    for i, raw_line in enumerate(data.splitlines(keepends=True), start=1):
        line = raw_line[:-1] if raw_line.endswith(b"\n") else raw_line
        if line.endswith(b"\r"):
            line = line[:-1]
        if not line.strip():
            offset += len(raw_line)
            continue
        try:
            rec = json.loads(line)
        except ValueError as e:
            raise ValueError(
                f"{path}: line {i} (byte {offset}) unparseable: {e}") from e
        if not isinstance(rec, dict):
            raise ValueError(f"{path}: line {i} not a JSON object")
        records.append((rec, i, offset))
        offset += len(raw_line)
    return records, sha


def normalize_sources(input_paths):
    """Parse each JSONL source ONCE; adapt every record. Unsupported raw
    tick CSV or malformed/torn line -> ("REFUSED", reason)."""
    fills, events, quotes = [], [], []
    sources = []
    for path in input_paths or []:
        if str(path).lower().endswith(".csv"):
            return ("REFUSED", f"unsupported raw tick CSV: {path}")
        try:
            records, sha = _parse_jsonl(path)
        except (OSError, ValueError) as e:
            return ("REFUSED", str(e))
        for rec, rec_no, off in records:
            ctx = _ctx(str(path), sha, rec_no, off)
            if rec.get("event_type") == "BBO_UPDATE":
                r = adapt_bbo(rec, ctx)
                if not isinstance(r, tuple):
                    quotes.append(r)
            elif rec.get("fill_type") in FILL_TYPES:
                r = adapt_fill(rec, ctx)
                if not isinstance(r, tuple):
                    fills.append(r)
            elif rec.get("event") in ANCHOR_EVENT_KINDS:
                r = adapt_spread_event(rec, ctx)
                if not isinstance(r, tuple):
                    events.append(r)
        sources.append({"path": str(path), "sha256": sha})
    return {"fills": fills, "events": events, "quotes": quotes,
            "sources": sources}


def join_anchor(fills, events):
    """Legal same-trade anchor: a RELEASE_DECISION/SUBMISSION event joined
    to its trade's RELEASE fills. Ambiguous/missing -> NOT_AVAILABLE."""
    rel_fills = [f for f in (fills or []) if f.get("fill_type") == "RELEASE"]
    rel_trades = {f.get("trade_id") for f in rel_fills}
    anchors = [e for e in (events or [])
               if e.get("kind") in ANCHOR_EVENT_KINDS]
    for a in anchors:
        if a.get("trade_id") in rel_trades:
            return a
    if not rel_trades:
        return ("NOT_AVAILABLE", "no RELEASE fill trade to join")
    return ("NOT_AVAILABLE",
            f"no anchor joins trades {rel_trades}")


def join_positions(fills, anchor_trade):
    """Per-leg pre-decision positions from ENTRY LONG/SHORT fills (qty
    validated). Missing leg -> NOT_AVAILABLE."""
    entries = [f for f in (fills or [])
               if f.get("fill_type") == "ENTRY"
               and f.get("trade_id") == anchor_trade]
    pos = {}
    for f in entries:
        leg = f.get("leg")
        side = f.get("side")
        qty = f.get("qty")
        if side not in ("LONG", "SHORT") or not qty or qty <= 0:
            return ("NOT_AVAILABLE", f"invalid ENTRY side/qty: {f}")
        if leg in ("near", "far"):
            pos[leg] = side
    if set(pos) != {"near", "far"}:
        return ("NOT_AVAILABLE",
                f"position legs incomplete: {sorted(pos)}")
    return {"near": pos["near"], "far": pos["far"]}


def resolve_contract_mapping(records, mapping_input, decision_ts_ms):
    """Per-decision contract mapping {near, far, evidence, version, hash}
    from a versioned mapping input (validity windows). Codes change after
    monthly settlement — resolution is per-window; missing/ambiguous (e.g.
    at a roll boundary) -> NOT_AVAILABLE, never a guess."""
    windows = ((mapping_input or {}).get("windows") or [])
    if not windows:
        return ("NOT_AVAILABLE", "contract mapping input missing")
    matched = []
    for w in windows:
        lo = w.get("valid_from_ms")
        hi = w.get("valid_to_ms")
        if lo is None or hi is None or not w.get("near") or not w.get("far"):
            continue
        if lo <= decision_ts_ms < hi:
            matched.append(w)
    if len(matched) != 1:
        return ("NOT_AVAILABLE",
                f"contract mapping ambiguous at {decision_ts_ms}: "
                f"{len(matched)} windows")
    w = matched[0]
    evidence = {"window": {"valid_from_ms": w.get("valid_from_ms"),
                           "valid_to_ms": w.get("valid_to_ms")},
                "decision_ts_ms": decision_ts_ms,
                "version": w.get("version")}
    h = hashlib.sha256(
        json.dumps({k: w.get(k) for k in ("near", "far", "version",
                                          "valid_from_ms", "valid_to_ms")},
                   sort_keys=True).encode("utf-8")).hexdigest()
    return {"near": w.get("near"), "far": w.get("far"),
            "evidence": evidence, "version": w.get("version"), "hash": h}


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
            os.link(tmp_path, target)
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
    return target


def _cleanup(out_dir, created):
    for f in created:
        try:
            os.unlink(f)
        except OSError:
            pass
    try:
        os.rmdir(out_dir)
    except OSError:
        pass


def build_normalized_snapshot(input_paths, out_dir, mapping_input=None):
    """Normalized snapshot (builder-native) + manifest with contract
    mapping evidence/version/hash — no-replace atomic writes; any failure
    removes the newly-created files/dir."""
    try:
        out_dir = _exclusive_out_dir(out_dir)
    except FileExistsError as e:
        return ("REFUSED", str(e))
    try:
        norm = normalize_sources(input_paths)
    except (OSError, ValueError) as e:
        _cleanup(out_dir, [])
        return ("REFUSED", str(e))
    if isinstance(norm, tuple):
        _cleanup(out_dir, [])
        return norm
    manifest = {"schema_version": "source-adapter-v2",
                "sources": norm.get("sources", []),
                "contract_mapping": mapping_input or
                {"status": "NOT_AVAILABLE", "reason": "no mapping input"}}
    events_payload = json.dumps(norm, indent=2, sort_keys=True,
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
    return norm


