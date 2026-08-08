"""A4 decision output under execution-quality tiers — skeletal."""


def decide(theta, state, extrema, params):
    raise NotImplementedError("decision.decide: R0/R1/R2/R3 under deterministic policy")


def params_from_config(config, event):
    raise NotImplementedError("decision.params_from_config: thresholds resolve from deployed config per event, else NOT_AVAILABLE")
