"""Online/causal reversal detection — A4 engine.

Reversal triggers fire only on events already seen (causal); |z| reversal,
velocity sign+confirmation and acceleration/deceleration are supported.
"""


def reversal_trigger(state, event):
    """Causal reversal detection on one arriving event.

    Returns a trigger dict when a pre-registered reversal condition fires,
    else None. Never uses future information.
    """
    if not state.get("armed", True):
        return None
    z = event.get("z")
    if z is not None and abs(z) >= 2.0:
        return {"triggered": True, "kind": "z_reversal", "z": z,
                "replay_seq": event.get("replay_seq")}
    vel = event.get("velocity_sign")
    if vel is not None and event.get("velocity_confirmed"):
        return {"triggered": True, "kind": "velocity_confirm",
                "sign": vel, "replay_seq": event.get("replay_seq")}
    accel = event.get("acceleration")
    if accel is not None and abs(accel) >= 0.5:
        return {"triggered": True, "kind": "acceleration",
                "accel": accel, "replay_seq": event.get("replay_seq")}
    return None
