"""R3 deterministic bounded branch control — skeletal (A4 v2.2)."""


def branch_state_key(level, event_seq):
    raise NotImplementedError("branches.branch_state_key: deterministic tree budget — no hindsight/combinatorial paths")


def next_decision_level(level, max_wait, safety):
    raise NotImplementedError("branches.next_decision_level: fixed next level / max wait / safety")


def derived_bars(events):
    raise NotImplementedError("branches.derived_bars: identical derived-bar sequence across all four branches (same stream)")
