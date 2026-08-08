"""A4 decision output under execution-quality tiers — skeletal (A4 v2)."""


def decide(theta, state, extrema, params):
    raise NotImplementedError("decision.decide: R0/R1/R2/R3 — causal only, NO future information")


def forward_outcome_separate(decision_rule, forward_outcome):
    raise NotImplementedError("decision.forward_outcome_separate: ex-post forward outcome must be distinguishable from a deployable decision rule")


def params_from_config(config, event):
    raise NotImplementedError("decision.params_from_config: thresholds resolve from deployed config per event, else NOT_AVAILABLE")
