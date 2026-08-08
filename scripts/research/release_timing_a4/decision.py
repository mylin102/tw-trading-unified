"""A4 decision output — skeletal (A4 v2.2: read-set trace + forward eval)."""


def decide(theta, state, extrema, params):
    raise NotImplementedError("decision.decide: R0/R1/R2/R3 — causal only, NO future information")


def decide_trace(theta, state, extrema, params, events):
    raise NotImplementedError("decision.decide_trace: returns read-set replay_seqs — must exclude future events (max <= decision replay_seq)")


def forward_evaluator(events, decision_ts):
    raise NotImplementedError("decision.forward_evaluator: consumes events AFTER decision_ts — separate from the causal decision rule")


def forward_outcome_separate(decision_rule, forward_outcome):
    raise NotImplementedError("decision.forward_outcome_separate: ex-post forward outcome distinguishable from a deployable decision rule")


def params_from_config(config, event):
    raise NotImplementedError("decision.params_from_config: thresholds resolve from deployed config per event, else NOT_AVAILABLE")
