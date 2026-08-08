"""Strategy-state deep clone at the release-decision clone point — skeletal."""


def clone_schema_version():
    raise NotImplementedError("clone.clone_schema_version: schema version string")


def clone_state_at_decision(strategy_state, decision_ts):
    raise NotImplementedError("clone.clone_state_at_decision: deep-clone/hash full state; missing -> NOT_AVAILABLE")
