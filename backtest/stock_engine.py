# Version: 4.1.0 (Buy-side fees + stop price fix)
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
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    台股回測引擎 v4: 增加 qty 紀錄
    回傳: (entries, exits, positions, pnl, reasons, quantities)
    """
    n = len(close)
    positions = np.zeros(n)
    pnl = np.zeros(n)
    entries = np.zeros(n)
    exits = np.zeros(n)
    reasons = np.zeros(n, dtype=nb.int32)
    quantities = np.zeros(n, dtype=nb.int32)
    
    current_pos = 0 
    qty_odd = 0
    qty_round = 0
    entry_price_avg = 0.0
    entry_day_odd = 0
    entry_day_round = 0
    max_price = 0.0
    entry_buy_cost = 0.0  # 累計買方手續費
    
    for i in range(n):
        curr_day = trading_day[i]
        
        if long_signals[i]:
            if current_pos == 0:
                qty_odd = 10 
                current_pos = 1
                entry_price_avg = close[i]
                entry_day_odd = curr_day
                max_price = close[i]
                entry_buy_cost = calc_stock_costs(close[i], qty_odd, True)
                entries[i] = close[i]
                reasons[i] = 1 
                quantities[i] = qty_odd
            elif current_pos == 1:
                qty_round = int(capital_per_trade // close[i])
                current_pos = 2
                entry_price_avg = (entry_price_avg * qty_odd + close[i] * qty_round) / (qty_odd + qty_round)
                entry_day_round = curr_day
                entry_buy_cost += calc_stock_costs(close[i], qty_round, True)
                entries[i] = close[i]
                reasons[i] = 2 
                quantities[i] = qty_round

        elif current_pos > 0:
            exit_price = 0.0
            r_code = 0
            if high[i] > max_price: max_price = high[i]
            
            can_sell_odd = (curr_day != entry_day_odd)
            
            if can_sell_odd:
                trailing_price = max_price * (1 - trailing_stop_pct)
                hard_stop_price = entry_price_avg * (1 - stop_loss_pct)
                
                # 取 trailing 和 hard stop 中較高的出場價，限制最大虧損
                if low[i] <= trailing_price or low[i] <= hard_stop_price:
                    if low[i] <= trailing_price and low[i] <= hard_stop_price:
                        # 兩者都觸發，取較高價
                        if trailing_price >= hard_stop_price:
                            exit_price = trailing_price; r_code = 5
                        else:
                            exit_price = hard_stop_price; r_code = 3
                    elif low[i] <= trailing_price:
                        exit_price = trailing_price; r_code = 5
                    else:
                        exit_price = hard_stop_price; r_code = 3
                elif high[i] >= entry_price_avg * (1 + take_profit_pct):
                    exit_price = entry_price_avg * (1 + take_profit_pct)
                    r_code = 4 
                elif short_signals[i]:
                    exit_price = close[i]
                    r_code = 6 
            
            # --- 最終強制出場 (不受當沖限制) ---
            if i == n - 1 and exit_price == 0.0:
                exit_price = close[i]
                r_code = 7 
            
            if exit_price > 0:
                exits[i] = exit_price
                reasons[i] = r_code
                total_qty = qty_odd + qty_round
                quantities[i] = total_qty
                trade_pnl = (exit_price - entry_price_avg) * total_qty
                sell_cost = calc_stock_costs(exit_price, total_qty, False)
                pnl[i] = trade_pnl - entry_buy_cost - sell_cost
                current_pos = 0; qty_odd = 0; qty_round = 0; entry_price_avg = 0.0; entry_buy_cost = 0.0
        
        positions[i] = current_pos
        
    return entries, exits, positions, pnl, reasons, quantities

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
