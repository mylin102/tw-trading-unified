"""Phase-transition classification — canonical frozen-precedence classifier.

Canonical arm mapping (A4/replay shared, MUST stay explicit):
- Y0 = Actual release (what the system actually did)
- Y1 = Atomic combined exit (R2)
- Y2 = Remain SPREAD (R1 — continue waiting)
- Y3 = Release + dedicated controller (R0 with A4)

Families:
- F_N (normal family) = {Y1, Y2} — arms that do NOT release
- F_R (release family) = {Y0, Y3} — arms that release

Frozen precedence (contract, 2026-08-08):
1. evidence gate — data_quality != "ok" -> INDETERMINATE_DATA_QUALITY
2. MANAGEMENT_BAD (conservative): lower(Y3)-upper(Y0) > M_economic AND
   lower(Y3) >= upper(F_N)-M_economic (finer label, precedes family
   beneficial)
3. HARMFUL iff lower(F_N)-upper(F_R) > M_economic
4. BENEFICIAL iff lower(F_R)-upper(F_N) > M_economic
5. overlap / equality-at-threshold -> INCONCLUSIVE_NEUTRAL

Yi are NET of path-specific fees/tax; [Li,Ui] = residual execution
uncertainty ONLY (no cost double-counting).
"""


def _family_interval(arms):
    """max lower / max upper across the arms of one family."""
    lows = [a[0] for a in arms]
    ups = [a[1] for a in arms]
    return (max(lows), max(ups))


def family_intervals(normal_arms, release_arms):
    """(F_N, F_R) interval-dominance bounds.

    normal_arms = [(L1,U1), (L2,U2)]; release_arms = [(L0,U0), (L3,U3)].
    """
    f_n = _family_interval(normal_arms)
    f_r = _family_interval(release_arms)
    return {"F_N": f_n, "F_R": f_r}


def interval_bounds(net_pnl, execution_uncertainty):
    """[Li, Ui] = net ± residual execution uncertainty (costs already in
    net_pnl — never re-added)."""
    return (net_pnl - execution_uncertainty,
            net_pnl + execution_uncertainty)


def uncertainty_bound(i, j, shared_cost=0.0, per_arm_cost=0.0):
    """Pairwise execution-uncertainty bound U_δ(i,j).

    Identical shared costs between two arms CANCEL exactly — only the
    per-arm component contributes to the pairwise difference.
    """
    return per_arm_cost


def materiality(i, j, M_economic=0.0, U_delta=0.0):
    """M_ij = max(M_economic, U_δ(i,j))."""
    return max(M_economic, U_delta)


def classify_outcome(Y0, Y1, Y2, Y3, actual=None, data_quality="ok",
                     M_economic=25.0, M_30=None, M_3no_release=None,
                     intervals=None):
    """Frozen-precedence classification (canonical API — the ONLY classifier
    truth for replay and A4 reports)."""
    if data_quality != "ok":
        return "INDETERMINATE_DATA_QUALITY"
    iv = intervals or {k: (locals()[k], locals()[k]) for k in ("Y0", "Y1",
                                                               "Y2", "Y3")}
    (l0, u0), (l1, u1), (l2, u2), (l3, u3) = (
        iv["Y0"], iv["Y1"], iv["Y2"], iv["Y3"])
    f_n = (max(l1, l2), max(u1, u2))
    f_r = (max(l0, l3), max(u0, u3))
    M = float(M_economic)
    # 2) MANAGEMENT_BAD precedes family beneficial
    if l3 - u0 > M and l3 >= f_n[1] - M:
        return "RELEASE_OK_MANAGEMENT_BAD"
    # 3) HARMFUL / 4) BENEFICIAL via interval dominance
    if f_n[0] - f_r[1] > M:
        return "RELEASE_HARMFUL"
    if f_r[0] - f_n[1] > M:
        return "RELEASE_BENEFICIAL"
    # 5) overlap / equality at threshold -> neutral
    return "INCONCLUSIVE_NEUTRAL"
