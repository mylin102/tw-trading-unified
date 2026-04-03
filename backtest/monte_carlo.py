import numpy as np
import numba as nb

@nb.njit(cache=True)
def run_monte_carlo_drawdown(trades_pnl: np.ndarray, initial_balance: float, iterations: int = 1000) -> np.ndarray:
    """
    Numba accelerated Monte Carlo Simulation.
    Randomly shuffles trade sequence to calculate potential drawdown distribution.
    """
    results = np.zeros(iterations)
    n_trades = len(trades_pnl)
    
    if n_trades == 0:
        return results

    for i in range(iterations):
        # Shuffle (NumPy shuffle is fast)
        shuffled = np.copy(trades_pnl)
        np.random.shuffle(shuffled)
        
        # Calculate max drawdown for this permutation
        equity = initial_balance + np.cumsum(shuffled)
        peak = initial_balance
        max_dd = 0.0
        for val in equity:
            if val > peak:
                peak = val
            dd = peak - val
            if dd > max_dd:
                max_dd = dd
        results[i] = max_dd
        
    return results
