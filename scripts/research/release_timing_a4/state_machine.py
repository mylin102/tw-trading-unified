"""NORMAL -> RELEASE_ARMED -> decision (R0..R3) — skeletal."""


def transition(state, event):
    raise NotImplementedError("state_machine.transition: NORMAL/RELEASE_ARMED with safety escapes")


def safety_escape(cause):
    raise NotImplementedError("state_machine.safety_escape: combined-loss floor / max adverse / max wait / quote|data-quality / lifecycle|pending")
