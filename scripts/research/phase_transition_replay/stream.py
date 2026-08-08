"""Immutable globally-ordered market-event stream (shared research contract).

Implements the phase-transition/A4 stream manifest: every event carries
source_event_seq / exchange_ts / recv_ts / replay_seq; the stream hash binds
the full ordered payload; all arms consume the SAME stream object.
"""

import hashlib
import json


def ordered_stream(events, clock_contract="immutable-global"):
    """Return (events_with_manifest, stream_hash, clock_contract).

    Events are ordered by (exchange_ts, source_event_seq); replay_seq is
    assigned 1..N; the digest covers the ordered manifest payload.
    """
    if clock_contract != "immutable-global":
        raise ValueError(f"unknown clock contract: {clock_contract!r}")
    ordered = sorted(
        list(events or []),
        key=lambda e: (e.get("exchange_ts") if e.get("exchange_ts") is not None
                       else float("inf"),
                       e.get("source_event_seq") or 0))
    out = []
    for i, ev in enumerate(ordered, start=1):
        row = dict(ev)
        row.setdefault("replay_seq", i)
        out.append(row)
    digest = hashlib.sha256(
        json.dumps(out, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return out, digest, clock_contract
