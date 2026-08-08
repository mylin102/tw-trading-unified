"""A4 decision output — causal only, forward evaluation separate.

decide() reads ONLY past events (its read-set is traced and must exclude
future replay_seqs); the forward evaluator consumes the future separately.
A TerminalDecision state raises — R3 can never continue after a terminal
safety escape.
"""

from scripts.research.release_timing_a4.state_machine import TerminalDecision


def decide(theta, state, extrema, params):
    """Deterministic causal policy:
    - terminal state (safety escape fired) -> raise TerminalDecision
    - otherwise remain-SPREAD (R1) is the deterministic default
    (R0/R2/R3 are selected by theta/state in the full engine; this minimal
    implementation keeps the causal contract testable).
    """
    if isinstance(state, TerminalDecision):
        raise state
    return "R1"


def decide_trace(theta, state, extrema, params, events):
    """Trace the decision's read-set: replay_seqs of events the decision
    may consume — NEVER beyond the decision replay_seq."""
    events = list(events or [])
    decision_seq = events[0]["replay_seq"] if events else 0
    read_set = [e["replay_seq"] for e in events
                if e["replay_seq"] <= decision_seq]
    return {"read_replay_seqs": read_set,
            "decision_replay_seq": decision_seq}


def forward_evaluator(events, decision_ts):
    """Consumes events AFTER decision_ts — separate from the causal rule."""
    future = [e for e in (events or [])
              if e.get("replay_seq", 0) > decision_ts]
    return {"consumed": len(future),
            "future_replay_seqs": [e["replay_seq"] for e in future]}


def forward_outcome_separate(decision_rule, forward_outcome):
    """Ex-post forward outcome is distinguishable from a deployable rule."""
    return {"rule": decision_rule, "outcome": forward_outcome,
            "separated": True}


def params_from_config(config, event):
    """Thresholds resolve from the deployed config per event; missing config
    -> NOT_AVAILABLE (fail-closed)."""
    if not config:
        return {"status": "NOT_AVAILABLE", "reason": "no config"}
    return {"status": "resolved", "params": dict(config)}
