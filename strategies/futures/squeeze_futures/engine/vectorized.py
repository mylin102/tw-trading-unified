#!/usr/bin/env python3
"""
向量化模擬器 (Vectorized Simulator)
使用 NumPy/Numba 進行高效的向量化回測

靈感來自 vectorbt-pro 的 Portfolio 模組
"""

import numpy as np
import pandas as pd
import numba as nb
from typing import Dict, Tuple, Optional
from dataclasses import dataclass, field
from rich.console import Console

console = Console()


@dataclass
class SimulatorConfig:
    """模擬器配置"""
    initial_balance: float = 100000
    point_value: float = 10
    fee_per_side: float = 20
    exchange_fee: float = 0
    tax_rate: float = 0.00002
    max_positions: int = 2
    lots_per_trade: int = 2
    slippage: float = 1.0


@nb.njit(parallel=True, cache=True)
def simulate_trades_vectorized(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    vwap: np.ndarray,
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
    tp1_pts: float = 30,
    tp1_lots: int = 1,
    exit_on_vwap: bool = True,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    向量化交易模擬
    
    Args:
        close, high, low, vwap: 價格數據
        long_signals, short_signals: 進場信號
        config: 模擬器配置
        stop_loss_pts: 停損點數
        tp1_pts: 第一目標點數
        tp1_lots: 第一目標口數
        exit_on_vwap: VWAP 離場
    
    Returns:
        entries: 進場價格 (0=無進場)
        exits: 出場價格 (0=無出場)
        positions: 部位方向
        pnl: 每筆交易損益
        exit_reasons: 出場原因 (0=SL, 1=TP, 2=VWAP, 3=EOD)
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
    
    for i in nb.prange(n):
        # 記錄當前部位
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
                # 停損
                sl_price = entry_price - stop_loss_pts
                if low[i] <= sl_price:
                    exit_price = sl_price
                    exit_reason = 0
                
                # 分批停利
                elif not tp1_triggered and lots_held >= 2 and tp1_lots > 0:
                    tp_price = entry_price + tp1_pts
                    if high[i] >= tp_price:
                        # 部分平倉
                        pnl[i] = tp1_lots * (tp_price - entry_price) * point_value
                        pnl[i] -= calc_costs(tp_price, entry_price, point_value, fee_per_side, exchange_fee, tax_rate, slippage, tp1_lots)
                        lots_held -= tp1_lots
                        tp1_triggered = True
                        entry_price = tp_price  # 更新剩餘部位的進場價
                        continue
                
                # VWAP 離場
                elif exit_on_vwap and close[i] < vwap[i]:
                    exit_price = close[i]
                    exit_reason = 2
                
                # 收盤離場 (簡化：假設每日最後一根 K 棒)
                elif i == n - 1:
                    exit_price = close[i]
                    exit_reason = 3
            
            else:  # 空單
                # 停損
                sl_price = entry_price + stop_loss_pts
                if high[i] >= sl_price:
                    exit_price = sl_price
                    exit_reason = 0
                
                # 分批停利
                elif not tp1_triggered and lots_held >= 2 and tp1_lots > 0:
                    tp_price = entry_price - tp1_pts
                    if low[i] <= tp_price:
                        pnl[i] = tp1_lots * (entry_price - tp_price) * point_value
                        pnl[i] -= calc_costs(entry_price, tp_price, point_value, fee_per_side, exchange_fee, tax_rate, slippage, tp1_lots)
                        lots_held -= tp1_lots
                        tp1_triggered = True
                        entry_price = tp_price
                        continue
                
                # VWAP 離場
                elif exit_on_vwap and close[i] > vwap[i]:
                    exit_price = close[i]
                    exit_reason = 2
                
                # 收盤離場
                elif i == n - 1:
                    exit_price = close[i]
                    exit_reason = 3
            
            # 執行出場
            if exit_price > 0:
                exits[i] = exit_price
                exit_reasons[i] = exit_reason
                
                # 計算損益
                if position > 0:
                    pnl_pts = exit_price - entry_price
                else:
                    pnl_pts = entry_price - exit_price
                
                pnl[i] = abs(position) * pnl_pts * point_value
                pnl[i] -= calc_costs(entry_price, exit_price, point_value, fee_per_side, exchange_fee, tax_rate, slippage, abs(position))
                
                # 重置
                position = 0
                entry_price = 0
                lots_held = 0
                tp1_triggered = False
    
    return entries, exits, positions, pnl, exit_reasons


@nb.njit
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


@nb.njit(parallel=True, cache=True)
def calculate_metrics(
    pnl: np.ndarray,
    entries: np.ndarray,
    exits: np.ndarray,
    positions: np.ndarray,
    initial_balance: float,
) -> Dict[str, float]:
    """
    計算績效指標
    
    Returns:
        字典包含所有績效指標
    """
    n = len(pnl)
    
    # 過濾有效交易
    trades = pnl[pnl != 0]
    num_trades = len(trades)
    
    if num_trades == 0:
        return {
            'total_return': 0.0,
            'total_pnl': 0.0,
            'sharpe_ratio': 0.0,
            'max_drawdown': 0.0,
            'ulcer_index': 0.0,
            'recovery_factor': 0.0,
            'expectancy': 0.0,
            'win_rate': 0.0,
            'profit_factor': 0.0,
            'avg_trade': 0.0,
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
        }
    
    # 基本統計
    total_pnl = np.sum(trades)
    total_return = total_pnl / initial_balance
    
    winning = trades[trades > 0]
    losing = trades[trades < 0]
    
    num_winning = len(winning)
    num_losing = len(losing)
    
    win_rate = num_winning / num_trades * 100 if num_trades > 0 else 0
    
    gross_profit = np.sum(winning) if num_winning > 0 else 0
    gross_loss = abs(np.sum(losing)) if num_losing > 0 else 0
    
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf
    avg_trade = total_pnl / num_trades
    
    # 權益曲線
    equity = np.zeros(n + 1)
    equity[0] = initial_balance
    for i in range(n):
        equity[i + 1] = equity[i] + pnl[i]
    
    # 回撤
    drawdown = np.zeros(n + 1)
    peak = equity[0]
    for i in range(1, n + 1):
        if equity[i] > peak:
            peak = equity[i]
        drawdown[i] = peak - equity[i]
    
    max_dd = np.max(drawdown)
    
    # 夏普比率
    returns = np.diff(equity) / equity[:-1]
    if np.std(returns) > 0:
        sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252 * 78)
    else:
        sharpe = 0
    
    # 潰瘍指數
    ulcer = np.sqrt(np.mean((drawdown / initial_balance) ** 2)) * 100
    
    # 修復因子
    recovery = total_pnl / max_dd if max_dd > 0 else 0
    
    # 期望值
    expectancy = total_pnl / num_trades
    
    return {
        'total_return': total_return,
        'total_pnl': total_pnl,
        'sharpe_ratio': sharpe,
        'max_drawdown': max_dd,
        'ulcer_index': ulcer,
        'recovery_factor': recovery,
        'expectancy': expectancy,
        'win_rate': win_rate,
        'profit_factor': profit_factor,
        'avg_trade': avg_trade,
        'total_trades': num_trades,
        'winning_trades': num_winning,
        'losing_trades': num_losing,
    }


class VectorizedSimulator:
    """
    向量化交易模擬器
    
    使用方式：
    1. 初始化：傳入 OHLCV 數據和配置
    2. 生成信號：使用向量化信號函數
    3. 執行模擬：一次測試所有參數組合
    4. 分析結果：獲取績效指標和權益曲線
    """
    
    def __init__(self, df: pd.DataFrame, config: SimulatorConfig = None):
        """
        Args:
            df: OHLCV 數據（需包含 Open, High, Low, Close, Volume, vwap, score, sqz_on, mom_state）
            config: 模擬器配置
        """
        self.df = df
        self.config = config or SimulatorConfig()
        
        # 轉換為 NumPy 陣列
        self.close = df['Close'].values
        self.high = df['High'].values
        self.low = df['Low'].values
        self.vwap = df['vwap'].values if 'vwap' in df.columns else self.close
        self.score = df['score'].values if 'score' in df.columns else np.zeros(len(df))
        self.sqz_on = df['sqz_on'].values if 'sqz_on' in df.columns else np.zeros(len(df), dtype=bool)
        self.mom_state = df['mom_state'].values if 'mom_state' in df.columns else np.zeros(len(df))
        
        console.print(f"[green]初始化模擬器：{len(df)} 筆數據[/green]")
    
    def generate_signals(
        self,
        entry_score: float,
        mom_state_long: int,
        mom_state_short: int,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        生成進場信號
        
        Args:
            entry_score: 進場分數門檻
            mom_state_long: 多頭動能條件
            mom_state_short: 空頭動能條件
        
        Returns:
            long_signals, short_signals
        """
        long_signals = (
            (~self.sqz_on) & 
            (self.score >= entry_score) & 
            (self.close > self.vwap) & 
            (self.mom_state >= mom_state_long)
        )
        
        short_signals = (
            (~self.sqz_on) & 
            (self.score <= -entry_score) & 
            (self.close < self.vwap) & 
            (self.mom_state <= mom_state_short)
        )
        
        return long_signals, short_signals
    
    def run(
        self,
        entry_score: float = 30,
        mom_state_long: int = 2,
        mom_state_short: int = 1,
        stop_loss_pts: float = 30,
        tp1_pts: float = 30,
        tp1_lots: int = 1,
        exit_on_vwap: bool = True,
    ) -> Dict:
        """
        執行單一回測
        
        Args:
            entry_score: 進場分數
            mom_state_long: 多頭動能條件
            mom_state_short: 空頭動能條件
            stop_loss_pts: 停損點數
            tp1_pts: 第一目標點數
            tp1_lots: 第一目標口數
            exit_on_vwap: VWAP 離場
        
        Returns:
            回測結果字典
        """
        # 1. 生成信號
        long_signals, short_signals = self.generate_signals(
            entry_score, mom_state_long, mom_state_short
        )
        
        # 2. 執行模擬
        entries, exits, positions, pnl, exit_reasons = simulate_trades_vectorized(
            self.close, self.high, self.low, self.vwap,
            long_signals, short_signals,
            self.config.initial_balance,
            self.config.point_value,
            self.config.fee_per_side,
            self.config.exchange_fee,
            self.config.tax_rate,
            self.config.max_positions,
            self.config.lots_per_trade,
            self.config.slippage,
            stop_loss_pts, tp1_pts, tp1_lots, exit_on_vwap,
        )
        
        # 3. 計算指標
        metrics = calculate_metrics(
            pnl, entries, exits, positions,
            self.config.initial_balance,
        )
        
        # 4. 計算權益曲線
        equity_curve = self.config.initial_balance + np.cumsum(pnl)
        drawdown = np.maximum.accumulate(equity_curve) - equity_curve
        
        # 5. 返回結果
        return {
            'params': {
                'entry_score': entry_score,
                'mom_state_long': mom_state_long,
                'mom_state_short': mom_state_short,
                'stop_loss_pts': stop_loss_pts,
                'tp1_pts': tp1_pts,
                'tp1_lots': tp1_lots,
                'exit_on_vwap': exit_on_vwap,
            },
            'metrics': metrics,
            'results': {
                'entries': entries,
                'exits': exits,
                'positions': positions,
                'pnl': pnl,
                'exit_reasons': exit_reasons,
                'equity_curve': equity_curve,
                'drawdown': drawdown,
            },
        }
    
    def run_param_grid(
        self,
        param_grid: Dict[str, list],
        progress_callback=None,
    ) -> pd.DataFrame:
        """
        執行參數網格回測
        
        Args:
            param_grid: 參數網格字典
            progress_callback: 進度回呼
        
        Returns:
            DataFrame 包含所有結果
        """
        from itertools import product
        
        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combinations = list(product(*values))
        
        console.print(f"[yellow]測試 {len(combinations)} 種參數組合[/yellow]")
        
        results = []
        
        for i, combo in enumerate(combinations):
            params = dict(zip(keys, combo))
            result = self.run(**params)
            result['metrics']['params'] = params
            results.append(result['metrics'])
            
            if progress_callback:
                progress_callback(i + 1, len(combinations))
            elif (i + 1) % 20 == 0:
                console.print(f"[dim]進度：{i+1}/{len(combinations)}[/dim]")
        
        # 轉換為 DataFrame
        df_results = pd.DataFrame(results)
        
        # 展開 params
        params_df = pd.DataFrame([r['params'] for r in results])
        for col in params_df.columns:
            df_results[f'param_{col}'] = params_df[col]
        
        return df_results
