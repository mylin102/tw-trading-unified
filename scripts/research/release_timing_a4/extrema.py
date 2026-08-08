"""Online after-breach extrema (events-arrival-only updates) — skeletal."""


def update_extrema(extrema, event):
    raise NotImplementedError("extrema.update_extrema: worst leg/combined, adverse price/spread/z, MAE, recovery, elapsed — causal only")


def no_future_selection(extrema, future_events):
    raise NotImplementedError("extrema.no_future_selection: running extrema must never consult future events")
