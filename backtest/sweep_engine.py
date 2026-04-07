# Version: 2.0.4 (Portfolio Grid Sweep)
import pandas as pd
import numpy as np
import copy
from itertools import product
from typing import Dict, List, Any, Tuple
from backtest.signal_generator import generate_signals
from strategies.futures.squeeze_futures.engine.vectorized import simulate_trades_vectorized, calculate_metrics
from backtest.stock_engine import simulate_stock_trades

REASON_MAP = {
    1: "偵察兵進場", 2: "主軍加碼", 3: "硬性止損", 4: "目標止盈", 
    5: "移動停損", 6: "訊號出場", 7: "收盤平倉"
}

def update_cfg_with_params(cfg: Dict[str, Any], strategy_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    new_cfg = copy.deepcopy(cfg)
    if "strategy" not in new_cfg:
        new_cfg["strategy"] = {}
    if strategy_name not in new_cfg["strategy"]:
        new_cfg["strategy"][strategy_name] = {}

    for k, v in params.items():
        # Counter-VWAP specific params
        if k in ["confirm_bars", "atr_sl_mult"]:
            if "counter_mode" not in new_cfg["strategy"]:
                new_cfg["strategy"]["counter_mode"] = {}
            new_cfg["strategy"]["counter_mode"][k] = v
        # Generic strategy params
        elif k in ["entry_score", "bb_std"]:
            new_cfg["strategy"][k] = v
        # PSAR specific
        elif k in ["acceleration", "sma_length"]:
            if "psar_breakout" not in new_cfg["strategy"]:
                new_cfg["strategy"]["psar_breakout"] = {}
            new_cfg["strategy"]["psar_breakout"][k] = v
        # Vol-Squeeze specific
        elif k in ["vol_multiplier"]:
            new_cfg["strategy"][k] = v
        # Generic: stop loss, atr mult, etc
        else:
            new_cfg[k] = v
    return new_cfg

def run_portfolio_grid_sweep(
    all_dfs: Dict[str, pd.DataFrame],
    strategy_name: str,
    sweep_params: Dict[str, List[Any]],
    base_cfg: Dict[str, Any],
    capital_per_trade: float = 10000.0
) -> Tuple[pd.DataFrame, Dict[int, np.ndarray]]:
    """
    真正的 Vectorbt 風格全域掃描。
    測試每一組參數對「整個資產組合」的總影響。
    回傳: (results_df, trades_dict)
    """
    param_names = list(sweep_params.keys())
    param_values = list(sweep_params.values())
    combinations = list(product(*param_values))
    
    # Pre-compute signals once per ticker (signals don't depend on SL/TP/TS)
    cached_signals = {}
    for ticker, df in all_dfs.items():
        cfg = update_cfg_with_params(base_cfg, strategy_name, {})
        longs, shorts = generate_signals(df, strategy_name, cfg)
        trading_days = (df.index.year * 10000 + df.index.month * 100 + df.index.day).values
        cached_signals[ticker] = (longs, shorts, trading_days,
                                  df["Close"].values, df["High"].values, df["Low"].values)

    results = []
    trades_dict = {}
    
    for idx, combo in enumerate(combinations):
        current_params = dict(zip(param_names, combo))
        
        portfolio_pnl = 0.0
        portfolio_trades = 0
        winning_assets = 0
        all_pnl_vecs = []
        
        for ticker, df in all_dfs.items():
            longs, shorts, trading_days, close, high, low = cached_signals[ticker]
            
            ent, ext, pos, pnl, reasons, qtys = simulate_stock_trades(
                close, high, low,
                trading_days, longs, shorts,
                1000000.0, capital_per_trade,
                current_params.get("stop_loss_pct", 0.03),
                current_params.get("take_profit_pct", 0.05),
                current_params.get("trailing_stop_pct", 0.015)
            )
            
            asset_pnl = np.sum(pnl)
            portfolio_pnl += asset_pnl
            portfolio_trades += len(pnl[pnl != 0])
            if asset_pnl > 0:
                winning_assets += 1
            all_pnl_vecs.append(pnl[pnl != 0])
        
        row = current_params.copy()
        row.update({
            "combo_idx": idx,
            "Total_PnL": portfolio_pnl,
            "Total_Trades": portfolio_trades,
            "Winning_Assets": winning_assets,
            "Profitable_Ratio": (winning_assets / len(all_dfs)) * 100 if all_dfs else 0
        })
        results.append(row)
        
        # Store all non-zero trades for this combination (for Monte Carlo)
        if all_pnl_vecs:
            trades_dict[idx] = np.concatenate(all_pnl_vecs)
        else:
            trades_dict[idx] = np.array([])
        
    return pd.DataFrame(results), trades_dict

def run_multi_asset_backtest(
    all_dfs: Dict[str, pd.DataFrame],
    strategy_name: str,
    cfg: Dict[str, Any],
    capital_per_trade: float = 10000.0
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    all_summary = []
    all_ledger = []
    for ticker, df in all_dfs.items():
        if df.empty: continue
        longs, shorts = generate_signals(df, strategy_name, cfg)
        trading_days = (df.index.year * 10000 + df.index.month * 100 + df.index.day).values
        ent, ext, pos, pnl, reasons, qtys = simulate_stock_trades(
            df["Close"].values, df["High"].values, df["Low"].values,
            trading_days, longs, shorts,
            1000000.0, capital_per_trade,
            cfg.get("stop_loss_pct", 0.03),
            cfg.get("take_profit_pct", 0.05),
            cfg.get("trailing_stop_pct", 0.015)
        )
        last_entry_price = 0.0
        for i in range(len(pnl)):
            if ent[i] > 0: last_entry_price = ent[i]
            if pnl[i] != 0:
                all_ledger.append({"標的": ticker, "時間": df.index[i], "進場價": round(last_entry_price, 2), "出場價": round(ext[i], 2), "股數": qtys[i], "損益": round(pnl[i], 0), "原因": REASON_MAP.get(reasons[i], "未知")})
        total_pnl = np.sum(pnl)
        if total_pnl != 0:
            all_summary.append({"ticker": ticker, "pnl": total_pnl, "trades": len(pnl[pnl != 0]), "win_rate": (np.sum(pnl > 0) / len(pnl[pnl != 0])) * 100 if len(pnl[pnl != 0]) > 0 else 0})
    return pd.DataFrame(all_summary), pd.DataFrame(all_ledger)
