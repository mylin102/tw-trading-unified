"""R3 deterministic bounded branch control — A4 engine.

The branch state key is deterministic (no hindsight/combinatorial paths);
each next decision has a fixed next level/max wait/safety; all branches
consume THE SAME events stream (same object, same hash, identical bars).
"""

from scripts.research.phase_transition_replay.stream import ordered_stream


def branch_state_key(level, event_seq):
    """Deterministic tree-budget key — identical inputs => identical key."""
    return f"L{level}:E{event_seq}"


def next_decision_level(level, max_wait, safety):
    """Fixed next decision level with the same max-wait/safety budget."""
    return {"level": level + 1, "max_wait": max_wait, "safety": safety}


def derived_bars(events, branch_id=None):
    """Derive bars from the SAME events stream; report the input reference
    (id), the shared stream hash and the bar sequence."""
    out, stream_hash, clock = ordered_stream(
        list(events or []), clock_contract="immutable-global")
    bars = [{"replay_seq": e["replay_seq"], "exchange_ts": e.get("exchange_ts"),
             "close": e.get("close")} for e in out]
    return {"branch_id": branch_id, "input_id": id(events),
            "stream_hash": stream_hash, "bars": bars,
            "clock_contract": clock}
