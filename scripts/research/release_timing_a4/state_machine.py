"""NORMAL -> RELEASE_ARMED -> decision (R0..R3) — A4 engine.

A safety escape returns a typed TerminalDecision (cause + terminal=True);
after it fires, R3 must never transition or emit an order candidate —
decide() raises the TerminalDecision when handed a terminal state.
"""


class TerminalDecision(Exception):
    """Typed terminal outcome of a safety escape."""

    def __init__(self, cause: str):
        super().__init__(f"TERMINAL: {cause}")
        self.cause = cause
        self.terminal = True


ESCAPE_CAUSES = ("combined_loss_floor", "max_adverse_excursion", "max_wait",
                 "quote_data_quality", "lifecycle_pending")


def transition(state, event):
    """NORMAL + breach -> RELEASE_ARMED; otherwise state unchanged."""
    if state == "NORMAL" and event.get("breach"):
        return "RELEASE_ARMED"
    return state


def safety_escape(cause):
    """Return a typed TerminalDecision(cause). Unknown causes raise."""
    if cause not in ESCAPE_CAUSES:
        raise ValueError(f"unknown escape cause: {cause!r}")
    return TerminalDecision(cause)
