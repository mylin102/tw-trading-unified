"""NORMAL -> RELEASE_ARMED -> decision (R0..R3) — skeletal (A4 v2.2).

A safety escape returns a typed TerminalDecision (cause + terminal=True);
after it fires, R3 must never transition or emit an order candidate.
"""


class TerminalDecision(Exception):
    """Typed terminal outcome of a safety escape (v2.2)."""

    def __init__(self, cause: str):
        super().__init__(f"TERMINAL: {cause}")
        self.cause = cause
        self.terminal = True


def transition(state, event):
    raise NotImplementedError("state_machine.transition: NORMAL/RELEASE_ARMED with safety escapes")


def safety_escape(cause):
    raise NotImplementedError("state_machine.safety_escape: returns TerminalDecision(cause) — terminal; after escape R3 must NOT continue")
