#!/usr/bin/env python3
"""Local-level Kalman filter for spread equilibrium estimation."""
import numpy as np
import pandas as pd


def kalman_local_level(spread: pd.Series, Q: float = 0.01, R: float = 1.0) -> pd.DataFrame:
    """
    Local-level Kalman filter for spread equilibrium.

    State:  μ_t = μ_{t-1} + η_t,      η_t ~ N(0, Q)
    Obs:    S_t = μ_t + ε_t,           ε_t ~ N(0, R)

    Returns DataFrame with columns:
      kalman_mu: posterior equilibrium estimate
      kalman_z:  standardized innovation = (S_t - μ_pred) / sqrt(P_pred + R)
      kalman_innovation: raw innovation S_t - μ_pred
      kalman_sigma: sqrt(innovation_variance)
    """
    arr = spread.values
    n = len(arr)
    mu = np.zeros(n)
    P = np.zeros(n)
    innov = np.zeros(n)
    innov_var = np.zeros(n)
    std_innov = np.zeros(n)

    first_valid = 0
    while first_valid < n and pd.isna(arr[first_valid]):
        first_valid += 1
    if first_valid >= n:
        return pd.DataFrame({"kalman_mu": mu, "kalman_z": std_innov,
                             "kalman_innovation": innov, "kalman_sigma": np.sqrt(innov_var)})

    mu[first_valid] = float(arr[first_valid])
    P[first_valid] = R
    innov[first_valid] = 0.0
    innov_var[first_valid] = R
    std_innov[first_valid] = 0.0

    for t in range(first_valid + 1, n):
        if pd.isna(arr[t]):
            mu[t] = mu[t - 1]
            P[t] = P[t - 1] + Q
            innov[t] = 0.0
            innov_var[t] = P[t] + R
            std_innov[t] = 0.0
            continue

        # Predict
        mu_pred = mu[t - 1]
        P_pred = P[t - 1] + Q

        # Innovation
        innov[t] = float(arr[t]) - mu_pred
        innov_var[t] = P_pred + R
        std_innov[t] = innov[t] / np.sqrt(innov_var[t]) if innov_var[t] > 1e-12 else 0.0

        # Update
        K = P_pred / innov_var[t]
        mu[t] = mu_pred + K * innov[t]
        P[t] = (1.0 - K) * P_pred

    return pd.DataFrame({
        "kalman_mu": mu,
        "kalman_z": std_innov,
        "kalman_innovation": innov,
        "kalman_sigma": np.sqrt(innov_var),
    })
