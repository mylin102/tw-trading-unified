"""Causal classification (interval dominance) — skeletal contract.

Freeze-classifier amendment (codex): each Yi is NET of path-specific
deterministic fees/tax; interval [Li,Ui] carries residual execution
uncertainty ONLY. F_N=[max(L1,L2),max(U1,U2)], F_R=[max(L0,L3),max(U0,U3)].
HARMFUL iff lower(F_N)-upper(F_R)>M_economic; BENEFICIAL iff reverse;
overlap => neutral. MANAGEMENT_BAD conservative precedence.
"""


def interval_bounds(net_pnl, execution_uncertainty):
    raise NotImplementedError("classify.interval_bounds: [Li,Ui] from net PnL + residual execution uncertainty")


def family_intervals(normal_arms, release_arms):
    raise NotImplementedError("classify.family_intervals: F_N / F_R interval dominance")


def uncertainty_bound(i, j, shared_cost=0.0, per_arm_cost=0.0):
    raise NotImplementedError("classify.uncertainty_bound: pairwise U_delta(i,j); shared costs cancel")


def materiality(i, j):
    raise NotImplementedError("classify.materiality: M_ij = max(M_economic, U_delta(i,j))")


def classify_outcome(Y0, Y1, Y2, Y3, actual, data_quality,
                     M_economic, M_30=None, M_3no_release=None):
    raise NotImplementedError(
        "classify.classify_outcome: frozen precedence — 1 INDETERMINATE, "
        "2 MANAGEMENT_BAD (lower(Y3)-upper(Y0)>M_economic AND "
        "lower(Y3)>=upper(F_N)-M_economic), 3 HARMFUL / 4 BENEFICIAL via "
        "F_N/F_R interval dominance, 5 neutral")
