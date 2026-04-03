#!/usr/bin/env python3
"""
向量化模擬器 (Vectorized Simulator) - 穩定增強版
使用 NumPy/Numba 進行高效的向量化回測，支援動態停損與動能斜率過濾。
"""

import numpy as np
import pandas as pd
import numba as nb
from typing import Dict, Tuple
from dataclasses import dataclass
from rich.console import Console

console = Console()


@dataclass
class SimulatorConfig:
    """模擬器配置"""
    initial_balance: float = 100000
    point_value: float = 50
    fee_per_side: float = 20
    exchange_fee: float = 0
    tax_rate: float = 0.00002
    max_positions: int = 2
    lots_per_trade: int = 2
    slippage: float = 1.0


@nb.njit(cache=True)
def calc_costs(
    entry: float,
    exit: float,
    point_value: float,
    fee_per_side: float,
    exchange_fee: float,
    tax_rate: float,
    slippage: float,
    lots: int,
) -> float:
    """計算交易成本"""
    fees = (fee_per_side + exchange_fee) * 2 * lots
    tax = (entry + exit) * point_value * tax_rate * lots
    slip_cost = slippage * point_value * lots
    return fees + tax + slip_cost


@nb.njit(cache=True)
def simulate_trades_vectorized(
    open_prices: np.ndarray,
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    vwap: np.ndarray,
    atr: np.ndarray,
    long_signals: np.ndarray,
    short_signals: np.ndarray,
    initial_balance: float,
    point_value: float,
    fee_per_side: float,
    exchange_fee: float,
    tax_rate: float,
    max_positions: int,
    lots_per_trade: int,
    slippage: float,
    stop_loss_pts: float = 30,
    atr_mult: float = 0.0,
    tp1_pts: float = 30,
    tp1_lots: int = 1,
    exit_on_vwap: bool = True,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    向量化交易模擬 (順序執行確保狀態正確)
    """
    n = len(close)
    entries = np.zeros(n)
    exits = np.zeros(n)
    positions = np.zeros(n)
    pnl = np.zeros(n)
    exit_reasons = np.zeros(n, dtype=nb.int8)
    
    position = 0
    entry_price = 0
    lots_held = 0
    tp1_triggered = False
    
    for i in range(n):
        positions[i] = position
        
        # 進場邏輯
        if position == 0:
            if long_signals[i]:
                position = lots_per_trade
                entry_price = close[i]
                lots_held = lots_per_trade
                tp1_triggered = False
                entries[i] = entry_price
            elif short_signals[i]:
                position = -lots_per_trade
                entry_price = close[i]
                lots_held = lots_per_trade
                tp1_triggered = False
                entries[i] = entry_price
        
        # 出場邏輯
        elif position != 0:
            exit_price = 0
            exit_reason = 0
            
            if position > 0:  # 多單
                # 停損 (ATR 或固定)
                sl_dist = atr[i] * atr_mult if atr_mult > 0 else stop_loss_pts
                sl_price = entry_price - sl_dist
                if low[i] <= sl_price:
                    exit_price = min(open_prices[i], sl_price)
                    exit_reason = 0
                
                # 分批停利
                elif not tp1_triggered and lots_held >= 2 and tp1_lots > 0:
                    tp_price = entry_price + tp1_pts
                    if high[i] >= tp_price:
                        exec_tp_price = max(open_prices[i], tp_price)
                        pnl[i] = tp1_lots * (exec_tp_price - entry_price) * point_value
                        pnl[i] -= calc_costs(exec_tp_price, entry_price, point_value, fee_per_side, exchange_fee, tax_rate, slippage, tp1_lots)
                        lots_held -= tp1_lots
                        tp1_triggered = True
                        entry_price = exec_tp_price
                        continue
                
                # VWAP 離場
                elif exit_on_vwap and close[i] < vwap[i]:
                    exit_price = close[i]
                    exit_reason = 2
                
                elif i == n - 1:
                    exit_price = close[i]
                    exit_reason = 3
            
            else:  # 空單
                sl_dist = atr[i] * atr_mult if atr_mult > 0 else stop_loss_pts
                sl_price = entry_price + sl_dist
                if high[i] >= sl_price:
                    exit_price = max(open_prices[i], sl_price)
                    exit_reason = 0
                
                elif not tp1_triggered and lots_held >= 2 and tp1_lots > 0:
                    tp_price = entry_price - tp1_pts
                    if low[i] <= tp_price:
                        exec_tp_price = min(open_prices[i], tp_price)
                        pnl[i] = tp1_lots * (entry_price - exec_tp_price) * point_value
                        pnl[i] -= calc_costs(entry_price, exec_tp_price, point_value, fee_per_side, exchange_fee, tax_rate, slippage, tp1_lots)
                        lots_held -= tp1_lots
                        tp1_triggered = True
                        entry_price = exec_tp_price
                        continue
                
                elif exit_on_vwap and close[i] > vwap[i]:
                    exit_price = close[i]
                    exit_reason = 2
                
                elif i == n - 1:
                    exit_price = close[i]
                    exit_reason = 3
            
            if exit_price > 0:
                exits[i] = exit_price
                exit_reasons[i] = exit_reason
                pnl_pts = (exit_price - entry_price) if position > 0 else (entry_price - exit_price)
                pnl[i] += lots_held * pnl_pts * point_value
                pnl[i] -= calc_costs(entry_price, exit_price, point_value, fee_per_side, exchange_fee, tax_rate, slippage, lots_held)
                position = 0
                entry_price = 0
                lots_held = 0
                tp1_triggered = False
    
    return entries, exits, positions, pnl, exit_reasons


@nb.njit(cache=True)
def calculate_metrics(
    pnl: np.ndarray,
    entries: np.ndarray,
    exits: np.ndarray,
    positions: np.ndarray,
    initial_balance: float,
) -> Dict[str, float]:
    """計算績效指標"""
    trades = pnl[pnl != 0]
    num_trades = len(trades)
    
    if num_trades == 0:
        return {'total_pnl': 0.0, 'win_rate': 0.0, 'profit_factor': 0.0, 'max_drawdown': 0.0, 'total_trades': 0}
    
    total_pnl = np.sum(trades)
    winning = trades[trades > 0]
    losing = trades[trades < 0]
    win_rate = len(winning) / num_trades * 100
    profit_factor = np.sum(winning) / abs(np.sum(losing)) if len(losing) > 0 else np.inf
    
    equity = initial_balance + np.cumsum(pnl)
    peak = initial_balance
    max_dd = 0.0
    for val in equity:
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd
        
    return {
        'total_pnl': total_pnl,
        'win_rate': win_rate,
        'profit_factor': profit_factor,
        'max_drawdown': max_dd,
        'total_trades': num_trades,
    }


class VectorizedSimulator:
    def __init__(self, df: pd.DataFrame, config: SimulatorConfig = None):
        self.df = df
        self.config = config or SimulatorConfig()
        self.open = df['Open'].values
        self.close = df['Close'].values
        self.high = df['High'].values
        self.low = df['Low'].values
        self.vwap = df['vwap'].values if 'vwap' in df.columns else self.close
        self.atr = df['atr'].values if 'atr' in df.columns else np.zeros(len(df))
        self.mom_velo = df['mom_velo'].values if 'mom_velo' in df.columns else np.zeros(len(df))
        self.score = df['score'].values if 'score' in df.columns else np.zeros(len(df))
        self.sqz_on = df['sqz_on'].values if 'sqz_on' in df.columns else np.zeros(len(df), dtype=bool)
        self.mom_state = df['mom_state'].values if 'mom_state' in df.columns else np.zeros(len(df))
        console.print(f"[green]初始化模擬器：{len(df)} 筆數據[/green]")
    
    def generate_signals(
        self, entry_score: float, mom_state_long: int, mom_state_short: int, velo_thresh: float = 0.0
    ) -> Tuple[np.ndarray, np.ndarray]:
        long_signals = (~self.sqz_on) & (self.score >= entry_score) & (self.mom_state >= mom_state_long) & (self.mom_velo >= velo_thresh)
        short_signals = (~self.sqz_on) & (self.score <= -entry_score) & (self.mom_state <= mom_state_short) & (self.mom_velo <= -velo_thresh)
        return long_signals, short_signals
    
    def run(
        self, entry_score=30, mom_state_long=2, mom_state_short=1, velo_thresh=0.0, 
        stop_loss_pts=30, atr_mult=0.0, tp1_pts=30, tp1_lots=1, exit_on_vwap=True
    ) -> Dict:
        l_sig, s_sig = self.generate_signals(entry_score, mom_state_long, mom_state_short, velo_thresh)
        ent, ext, pos, pnl, reasons = simulate_trades_vectorized(
            self.open, self.close, self.high, self.low, self.vwap, self.atr, l_sig, s_sig,
            self.config.initial_balance, self.config.point_value, self.config.fee_per_side,
            self.config.exchange_fee, self.config.tax_rate, self.config.max_positions,
            self.config.lots_per_trade, self.config.slippage, stop_loss_pts, atr_mult,
            tp1_pts, tp1_lots, exit_on_vwap
        )
        metrics = calculate_metrics(pnl, ent, ext, pos, self.config.initial_balance)
        return {'metrics': metrics, 'params': locals()}
