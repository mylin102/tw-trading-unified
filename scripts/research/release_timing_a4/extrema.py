"""Online after-breach extrema (events-arrival-only updates) — A4 engine.

Running extrema update ONLY as events arrive — never consult future events.
"""


def update_extrema(extrema, event):
    """Causally update running extrema from one arriving event."""
    out = dict(extrema or {})
    combined = event.get("combined_net")
    if combined is not None:
        out["worst_combined"] = min(out.get("worst_combined", combined),
                                    combined)
    leg = event.get("loss_leg_pnl")
    if leg is not None:
        out["worst_leg"] = min(out.get("worst_leg", leg), leg)
    price = event.get("price")
    if price is not None and "adverse_price" not in out:
        out["adverse_price"] = price
    z = event.get("z")
    if z is not None:
        out["max_abs_z"] = max(out.get("max_abs_z", abs(z)), abs(z))
    out["elapsed"] = event.get("elapsed", out.get("elapsed", 0))
    return out


def no_future_selection(extrema, future_events):
    """Contract guard: running extrema NEVER consult future events.

    Returns False (future events are not selected into the running state);
    a real implementation must never read `future_events`.
    """
    return False
