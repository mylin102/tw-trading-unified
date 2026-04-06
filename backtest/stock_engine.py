import numpy as np
import numba as nb
from typing import Tuple, Dict

@nb.njit(cache=True)
def calc_stock_costs(price: float, qty: int, is_buy: bool) -> float:
    amount = price * qty
    fee = max(20.0, amount * 0.0005) 
    tax = 0.0 if is_buy else (amount * 0.003)
    return fee + tax

@nb.njit(cache=True)
def simulate_stock_trades(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    trading_day: np.ndarray,
    long_signals: np.ndarray,
    short_signals: np.ndarray,
    initial_balance: float,
    capital_per_trade: float,
    stop_loss_pct: float = 0.03,
    take_profit_pct: float = 0.05,
    trailing_stop_pct: float = 0.015,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    台股回測引擎 v3: 增加 reason 紀錄 (1:Entry, 2:Scale, 3:Stop, 4:TP, 5:Trailing, 6:Signal, 7:Time)
    """
    n = len(close)
    positions = np.zeros(n)
    pnl = np.zeros(n)
    entries = np.zeros(n)
    exits = np.zeros(n)
    reasons = np.zeros(n, dtype=nb.int32)
    
    current_pos = 0 
    qty_odd = 0
    qty_round = 0
    entry_price_avg = 0.0
    entry_day_odd = 0
    entry_day_round = 0
    max_price = 0.0
    
    for i in range(n):
        curr_day = trading_day[i]
        
        if long_signals[i]:
            if current_pos == 0:
                qty_odd = 10 
                current_pos = 1
                entry_price_avg = close[i]
                entry_day_odd = curr_day
                max_price = close[i]
                entries[i] = close[i]
                reasons[i] = 1 # SCOUT_ENTRY
            elif current_pos == 1:
                qty_round = int(capital_per_trade // close[i])
                current_pos = 2
                entry_price_avg = (entry_price_avg * qty_odd + close[i] * qty_round) / (qty_odd + qty_round)
                entry_day_round = curr_day
                entries[i] = close[i]
                reasons[i] = 2 # SCALE_UP

        elif current_pos > 0:
            exit_price = 0.0
            r_code = 0
            if high[i] > max_price: max_price = high[i]
            
            can_sell_odd = (curr_day != entry_day_odd)
            
            if can_sell_odd:
                # 判斷優先級
                if low[i] <= max_price * (1 - trailing_stop_pct):
                    exit_price = max_price * (1 - trailing_stop_pct)
                    r_code = 5 # TRAILING_STOP
                elif low[i] <= entry_price_avg * (1 - stop_loss_pct):
                    exit_price = entry_price_avg * (1 - stop_loss_pct)
                    r_code = 3 # HARD_STOP
                elif high[i] >= entry_price_avg * (1 + take_profit_pct):
                    exit_price = entry_price_avg * (1 + take_profit_pct)
                    r_code = 4 # TAKE_PROFIT
                elif short_signals[i]:
                    exit_price = close[i]
                    r_code = 6 # SIGNAL_EXIT
                elif i == n - 1:
                    exit_price = close[i]
                    r_code = 7 # FINAL_EXIT
            
            if exit_price > 0:
                exits[i] = exit_price
                reasons[i] = r_code
                trade_pnl = (exit_price - entry_price_avg) * (qty_odd + qty_round)
                pnl[i] = trade_pnl - calc_stock_costs(exit_price, qty_odd + qty_round, False)
                current_pos = 0; qty_odd = 0; qty_round = 0; entry_price_avg = 0.0
        
        positions[i] = current_pos
        
    return entries, exits, positions, pnl, reasons

def calculate_stock_metrics(pnl: np.ndarray, initial_balance: float) -> Dict[str, float]:
    trades = pnl[pnl != 0]
    num_trades = len(trades)
    if num_trades == 0:
        return {'total_pnl': 0.0, 'win_rate': 0.0, 'total_trades': 0.0}
    
    return {
        'total_pnl': float(np.sum(trades)),
        'win_rate': float(np.sum(trades > 0) / num_trades * 100),
        'total_trades': float(num_trades)
    }
