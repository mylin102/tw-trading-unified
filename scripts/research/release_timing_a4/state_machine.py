"""NORMAL -> RELEASE_ARMED -> decision (R0..R3) — skeletal (A4 v2).

A safety escape is a TERMINAL decision: after it fires, R3 must never
continue — the state machine exits ARMED for good (TERMINATED).
"""


def transition(state, event):
    raise NotImplementedError("state_machine.transition: NORMAL/RELEASE_ARMED with safety escapes")


def safety_escape(cause):
    raise NotImplementedError("state_machine.safety_escape: terminal — combined-loss floor / max adverse / max wait / quote|data-quality / lifecycle|pending; after escape R3 must NOT continue")
