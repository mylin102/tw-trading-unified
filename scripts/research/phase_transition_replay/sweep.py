"""Threshold overlap matrix — replay sweeps (research-only)."""


def threshold_overlap(thresholds, candidates):
    """For each threshold, list the candidates whose combined_net breaches
    it (<= threshold). All thresholds reported — no best-threshold
    selection."""
    rows = []
    for t in (thresholds or []):
        rows.append({
            "threshold": t,
            "hits": [c for c in (candidates or [])
                     if c.get("combined_net", 0.0) <= t],
        })
    return {"thresholds": list(thresholds or []), "rows": rows}
