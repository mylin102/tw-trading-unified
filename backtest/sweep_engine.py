import pandas as pd
import numpy as np
import copy
from itertools import product
from typing import Dict, List, Any, Tuple
from backtest.signal_generator import generate_signals
from strategies.futures.squeeze_futures.engine.vectorized import simulate_trades_vectorized, calculate_metrics

def update_cfg_with_params(cfg: Dict[str, Any], strategy_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    new_cfg = copy.deepcopy(cfg)
    if "strategy" not in new_cfg:
        new_cfg["strategy"] = {}
    if strategy_name not in new_cfg["strategy"]:
        new_cfg["strategy"][strategy_name] = {}

    for k, v in params.items():
        if k == "entry_score":
            new_cfg["strategy"]["entry_score"] = v
        elif k == "atr_mult":
            new_cfg["strategy"][strategy_name]["atr_mult"] = v
        else:
            new_cfg["strategy"][strategy_name][k] = v
    return new_cfg

def run_grid_sweep(
    df: pd.DataFrame,
    strategy_name: str,
    sweep_params: Dict[str, List[Any]],
    base_cfg: Dict[str, Any],
    initial_balance: float = 100000.0
) -> Tuple[pd.DataFrame, Dict[str, np.ndarray]]:
    """
    Run grid sweep and return (summary_df, trades_pnl_by_combo_index).
    """
    param_names = list(sweep_params.keys())
    param_values = list(sweep_params.values())
    combinations = list(product(*param_values))
    
    results = []
    trades_dict = {} # Key: combo_index, Value: array of trade PnLs
    
    open_arr = df["Open"].values
    high_arr = df["High"].values
    low_arr = df["Low"].values
    close_arr = df["Close"].values
    vwap_arr = df["vwap"].values if "vwap" in df.columns else np.zeros(len(df))
    atr_arr = df["atr"].values if "atr" in df.columns else np.full(len(df), 30.0)

    for i, combo in enumerate(combinations):
        current_params = dict(zip(param_names, combo))
        cfg = update_cfg_with_params(base_cfg, strategy_name, current_params)
        
        longs, shorts = generate_signals(df, strategy_name, cfg)
        
        atr_mult = current_params.get("atr_mult", cfg["strategy"].get(strategy_name, {}).get("atr_mult", 2.0))
        
        # Run simulation with FULL 16 arguments
        entries, exits, positions, pnl, reasons = simulate_trades_vectorized(
            open_arr, close_arr, high_arr, low_arr, vwap_arr, atr_arr,
            longs, shorts, 
            initial_balance=initial_balance,
            point_value=10.0,
            fee_per_side=10.0,
            exchange_fee=2.0,
            tax_rate=0.00002,
            max_positions=1,
            lots_per_trade=1,
            slippage=1.0,
            stop_loss_pts=30,
            atr_mult=atr_mult,
            tp1_pts=30,
            tp1_lots=1,
            exit_on_vwap=True
        )
        
        # Capture actual trade PnLs (non-zero entries)
        actual_trades = pnl[pnl != 0]
        trades_dict[str(i)] = actual_trades
        
        metrics = calculate_metrics(pnl, np.zeros(1), np.zeros(1), np.zeros(1), initial_balance)
        row = current_params.copy()
        row.update(metrics)
        row["combo_idx"] = str(i)
        results.append(row)
        
    return pd.DataFrame(results), trades_dict
