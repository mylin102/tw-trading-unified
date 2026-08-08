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
            "leg": str(raw.get("leg") or "").lower(),
            "contract": str(raw.get("contract") or "").lower(),
            "side": side, "qty": qty, "ts_text": ts_text,
            "parsed_ts": _parse_ts(ts_text), "unit": "epoch_ms",
            **source_ctx}


def adapt_spread_event(raw, source_ctx):
    """spread events JSONL record -> normalized event. Legal anchor kinds:
    RELEASE_DECISION/SUBMISSION — the ACTUAL runtime encodes SUBMISSION as
    ORDER_SUBMITTED with strategy=MTS_RELEASE."""
    kind = raw.get("event")
    if kind == "ORDER_SUBMITTED" and raw.get("strategy") == "MTS_RELEASE":
        kind = "SUBMISSION"
    if kind not in ANCHOR_EVENT_KINDS:
        return ("NOT_AVAILABLE", f"event {raw.get('event')!r} not a legal "
                f"anchor kind")
    ts_text = raw.get("ts")
    return {"kind": kind, "trade_id": raw.get("trade_id"),
            "order_id": raw.get("order_id"), "ts_text": ts_text,
            "parsed_ts": _parse_ts(ts_text), "unit": "epoch_ms",
            "strategy": raw.get("strategy"),
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
    return {"leg": str(raw.get("leg") or "").lower(),
            "contract": raw.get("contract_code"),
            "exchange_ts_ms": _norm_ts(raw.get("exchange_ts_ms")),
            "receive_ts_ms": _norm_ts(raw.get("receive_ts_ms")),
            "feed": src, "bid": raw.get("bid"), "ask": raw.get("ask"),
            **source_ctx}


def _norm_ts(v):
    """Actual telemetry emits FLOAT epoch-ms — normalize to int ms."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


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
    tick CSV or malformed/torn line -> ("REFUSED", reason). Unknown
    fill_type records are COUNTED (never silently dropped)."""
    fills, events, quotes = [], [], []
    sources = []
    unknown_fill_types = {}
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
            elif rec.get("fill_type"):
                if rec["fill_type"] in FILL_TYPES:
                    r = adapt_fill(rec, ctx)
                    if not isinstance(r, tuple):
                        fills.append(r)
                else:
                    unknown_fill_types[rec["fill_type"]] = \
                        unknown_fill_types.get(rec["fill_type"], 0) + 1
            elif rec.get("event") in ANCHOR_EVENT_KINDS or (
                    rec.get("event") == "ORDER_SUBMITTED"
                    and rec.get("strategy") == "MTS_RELEASE"):
                r = adapt_spread_event(rec, ctx)
                if not isinstance(r, tuple):
                    events.append(r)
        sources.append({"path": str(path), "sha256": sha})
    return {"fills": fills, "events": events, "quotes": quotes,
            "sources": sources,
            "unknown_fill_types": unknown_fill_types}


def join_anchor(fills, events):
    """EVERY legal same-trade anchor: each RELEASE_DECISION/SUBMISSION
    event joined to its trade's RELEASE fills, one anchor per unique
    trade/release. Ambiguous -> per-candidate NOT_AVAILABLE entries;
    zero candidates -> ("NOT_AVAILABLE", reason)."""
    rel_fills = [f for f in (fills or []) if f.get("fill_type") == "RELEASE"]
    rel_trades = {f.get("trade_id") for f in rel_fills}
    if not rel_trades:
        return ("NOT_AVAILABLE", "no RELEASE fill trade to join")
    seen = set()
    anchors = []
    for e in (events or []):
        if e.get("kind") not in ANCHOR_EVENT_KINDS:
            continue
        tid = e.get("trade_id")
        if tid not in rel_trades:
            anchors.append(("NOT_AVAILABLE",
                            f"anchor trade {tid!r} has no RELEASE fill"))
            continue
        if tid in seen:
            continue  # unique trade/release event
        seen.add(tid)
        anchors.append(e)
    if not seen:
        return ("NOT_AVAILABLE", "no anchor joins a RELEASE fill trade")
    return anchors


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


EPOCH_MS_MIN = 1_400_000_000_000
EPOCH_MS_MAX = 2_500_000_000_000


def _valid_epoch_ms(v):
    return (isinstance(v, int) and not isinstance(v, bool)
            and EPOCH_MS_MIN <= v <= EPOCH_MS_MAX)


def _valid_price(v):
    if v is None or isinstance(v, bool):
        return False
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and f > 0.0 and f != float("inf")


def _attach_leg_bbo(leg, contract, decision_ts_ms, position_side,
                    quotes, params):
    """Attach the pre-anchor, exact-code/leg BBO for one leg with age +
    close-side gates. Missing/invalid -> typed unavailable."""
    reasons = []
    best = None
    for q in quotes or []:
        if q.get("leg") != leg:
            continue
        if q.get("feed") not in BBO_SOURCE_ALLOWLIST:
            continue
        if q.get("contract") != contract:
            reasons.append(f"{leg}: code {q.get('contract')!r} != "
                           f"{contract!r} (window mapping)")
            continue
        ts = q.get("exchange_ts_ms")
        if not isinstance(ts, int) or ts <= 0:
            reasons.append(f"{leg}: exchange_ts_ms invalid: {ts!r}")
            continue
        if ts > decision_ts_ms:
            reasons.append(
                f"{leg}: quote ts {ts} after decision anchor "
                f"{decision_ts_ms}")
            continue
        if not _valid_price(q.get("bid")) or not _valid_price(q.get("ask")):
            reasons.append(f"{leg}: bid/ask invalid")
            continue
        recv = q.get("receive_ts_ms")
        age_s = ((recv - ts) / 1000.0) if isinstance(recv, int) else None
        if age_s is None or age_s < 0 or age_s > params["max_age_s"]:
            reasons.append(f"{leg}: age {age_s}s out of bounds")
            continue
        if best is None or ts > (best.get("exchange_ts_ms") or 0):
            best = q
    if best is None:
        return {"available": False,
                "reason": "; ".join(reasons) or f"{leg}: no valid BBO"}
    return {"available": True, "bid": float(best["bid"]),
            "ask": float(best["ask"]),
            "age_s": ((best.get("receive_ts_ms") - best["exchange_ts_ms"])
                      / 1000.0),
            "close_action": position_side,
            "quote_exchange_ts": best["exchange_ts_ms"],
            "quote_source": f"{best['source']}:{best.get('byte_offset')}"}


def _mapping_evidence(events_list):
    evs = events_list or []
    return [{"version": e.get("contract_mapping_version"),
             "evidence": e.get("contract_mapping_evidence")}
            for e in evs if e.get("contract_mapping_evidence")]


def _build_event(anchor, positions, mapping, quotes, params):
    """One runner-compatible event with per-leg BBO attached."""
    decision_ts_ms = anchor.get("parsed_ts")
    event = {"source_event_seq": anchor.get("record_no") or 1,
             "exchange_ts": decision_ts_ms,
             "recv_ts": decision_ts_ms,
             "decision_ts_ms": decision_ts_ms,
             "contracts": {"near": mapping["near"], "far": mapping["far"]},
             "contract_mapping_version": mapping.get("version"),
             "contract_mapping_evidence": mapping.get("evidence"),
             "quotes": {}}
    for leg in ("near", "far"):
        event["quotes"][leg] = _attach_leg_bbo(
            leg, mapping[leg], decision_ts_ms, positions[leg], quotes,
            params)
    return event


def build_normalized_snapshot(input_paths, out_dir, mapping_input=None):
    """Runner-compatible normalized snapshot pipeline:

    normalize JSONL -> legal same-trade anchor -> validated anchor
    epoch-ms -> per-leg ENTRY positions -> per-decision roll mapping ->
    near/far actual codes -> attach pre-anchor allowlisted BBO with
    age/skew/close-side gates -> emit events.json (runner LIST) +
    manifest (event_map/mapping/source hashes) via no-replace atomic
    all-or-nothing. Missing/invalid -> typed REFUSED, never fake success.
    """
    params = {"max_age_s": (mapping_input or {}).get("max_age_s", 30),
              "max_pair_skew_ms": (mapping_input or {}).get(
                  "max_pair_skew_ms", 1000)}
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
    anchors = join_anchor(norm["fills"], norm["events"])
    if isinstance(anchors, tuple):
        _cleanup(out_dir, [])
        return anchors
    events_list = []
    refused = []
    event_map = {}
    for anchor in anchors:
        if isinstance(anchor, tuple):
            refused.append({"anchor": str(anchor[0]), "reason": anchor[1]})
            continue
        decision_ts_ms = anchor.get("parsed_ts")
        if not _valid_epoch_ms(decision_ts_ms):
            refused.append({"trade_id": anchor.get("trade_id"),
                            "reason": f"anchor timestamp invalid: "
                                      f"{anchor.get('ts_text')!r}"})
            continue
        positions = join_positions(norm["fills"], anchor.get("trade_id"))
        if isinstance(positions, tuple):
            refused.append({"trade_id": anchor.get("trade_id"),
                            "reason": str(positions[1])})
            continue
        mapping = resolve_contract_mapping(
            [], mapping_input, decision_ts_ms)
        if isinstance(mapping, tuple):
            refused.append({"trade_id": anchor.get("trade_id"),
                            "reason": str(mapping[1])})
            continue
        ev = _build_event(anchor, positions, mapping, norm["quotes"],
                          params)
        legs_ok = [ev["quotes"][l] for l in ("near", "far")
                   if ev["quotes"][l].get("available")]
        if len(legs_ok) == 2:
            skew = abs(legs_ok[0]["quote_exchange_ts"]
                       - legs_ok[1]["quote_exchange_ts"])
            if skew > params["max_pair_skew_ms"]:
                for l in ("near", "far"):
                    ev["quotes"][l] = {"available": False,
                                       "reason": f"pair skew {skew}ms > "
                                                 f"{params['max_pair_skew_ms']}ms"}
        seq = len(events_list) + 1
        ev["source_event_seq"] = seq
        events_list.append(ev)
        event_map[str(seq)] = {
            "trade_id": anchor.get("trade_id"),
            "anchor": anchor.get("source"),
            "anchor_byte_offset": anchor.get("byte_offset"),
            "positions": positions,
            "contracts": {"near": mapping["near"], "far": mapping["far"]}}
    if not events_list:
        _cleanup(out_dir, [])
        return ("NOT_AVAILABLE",
                "no candidate produced an event: " + str(refused))
    manifest = {
        "schema_version": "source-adapter-v2",
        "sources": norm.get("sources", []),
        "unknown_fill_types": norm.get("unknown_fill_types", {}),
        "refused_candidates": refused,
        "contract_mapping": {
            "evidence": [m["evidence"] for m in
                         (_mapping_evidence(events_list) if events_list
                          else [])],
            "versions": sorted({m["version"] for m in
                                _mapping_evidence(events_list)}
                               if events_list else []),
        },
        "event_map": event_map,
    }
    events_payload = json.dumps(events_list, indent=2, sort_keys=True,
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
    return events_list


