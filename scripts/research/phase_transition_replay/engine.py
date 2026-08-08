"""Replay engine — research-only implementation.

The engine path reuses the CANONICAL clone/execution/classify/A4 pieces
— classify.classify_outcome is the ONLY classifier truth, never
duplicated here.

Arm PnL contract (per kept event):
- event MUST carry entries {near: {price, qty}, far: {price, qty}} and
  release_leg ("near"/"far") — the SINGLE_LEG release target
- executable prices from the event quotes (LONG closes at bid, SHORT at
  ask — same convention as execution.executable_prices)
- fee netting from the prereg fee_assumptions (per_leg)
- Y0 = actual release (close release_leg)
- Y1 = atomic combined exit (close BOTH legs)
- Y2 = remain SPREAD (no close — decision-point mark 0)
- Y3 = release + dedicated controller (same closed-leg economics as Y0
  at the decision point; controller value = 0)
- intervals [Li, Ui] = net ± per-arm residual execution uncertainty
  (fee per closed leg)
- missing entries/release_leg/executable price -> fail-closed
  INDETERMINATE_DATA_QUALITY with a reason — NEVER fabricated values
"""

from scripts.research.phase_transition_replay import classify

ARMS = ("Y0", "Y1", "Y2", "Y3")


def _fail(reason):
    return ("INDETERMINATE_DATA_QUALITY", reason)


def arm_pnl(event, params):
    """Y0..Y3 net-of-fees arm PnL + 6 pairwise deltas + canonical
    classification for one kept event.

    Returns ("ok", {"arms", "intervals", "pairwise_deltas",
    "classification"}) or ("INDETERMINATE_DATA_QUALITY", reason).
    """
    entries = event.get("entries")
    if not isinstance(entries, dict) or set(entries) != {"near", "far"}:
        return _fail("entries missing/incomplete (near/far required)")
    release_leg = event.get("release_leg")
    if release_leg not in ("near", "far"):
        return _fail(f"release_leg missing/invalid: {release_leg!r}")
    fees = params.get("fee_assumptions", {})
    fee = float((fees.get("fee-v1", {}) or {}).get("per_leg", 0.0))
    m = float(params.get("m_economic", 0.0))
    quotes = event.get("quotes") or {}
    exe = {}
    for leg in ("near", "far"):
        q = quotes.get(leg) or {}
        price = q.get("bid") if q.get("close_action") == "LONG" \
            else q.get("ask")
        if price is None:
            return _fail(f"{leg} executable price missing")
        exe[leg] = float(price)

    def leg_net(leg):
        e = entries[leg]
        return (exe[leg] - float(e["price"])) * float(e["qty"])

    arms = {
        "Y0": leg_net(release_leg) - fee,
        "Y1": leg_net("near") + leg_net("far") - 2.0 * fee,
        "Y2": 0.0,
        "Y3": leg_net(release_leg) - fee,
    }
    residual = {"Y0": fee, "Y1": 2.0 * fee, "Y2": 0.0, "Y3": fee}
    intervals = {k: (arms[k] - residual[k], arms[k] + residual[k])
                 for k in ARMS}
    deltas = {}
    for i, a in enumerate(ARMS):
        for b in ARMS[i + 1:]:
            deltas[(a, b)] = abs(arms[a] - arms[b])
    label = classify.classify_outcome(
        arms["Y0"], arms["Y1"], arms["Y2"], arms["Y3"],
        M_economic=m, intervals=intervals)
    return ("ok", {"arms": arms, "intervals": intervals,
                   "pairwise_deltas": deltas, "classification": label})
