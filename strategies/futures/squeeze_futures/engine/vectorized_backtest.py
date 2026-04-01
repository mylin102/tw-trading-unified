#!/usr/bin/env python3
"""
向量化回測引擎 (Vectorized Backtest Engine)
靈感來自 vectorbt-pro，使用 NumPy/Numba 進行廣播運算

核心觀念：
- 參數廣播：一次測試所有參數組合
- 信號矩陣：將進場/出場信號視為矩陣
- 向量化計算：避免 Python 循環，使用 NumPy 廣播
"""

import numpy as np
import pandas as pd
import numba as nb
from typing import Dict, Tuple, Optional
from dataclasses import dataclass
from rich.console import Console

console = Console()


@dataclass
class BacktestConfig:
    """回測配置"""
    initial_balance: float = 100000
    point_value: int = 10
    fee_per_side: float = 20
    exchange_fee: float = 0
    tax_rate: float = 0.00002
    max_positions: int = 2
    lots_per_trade: int = 2


@nb.njit(parallel=True, cache=True)
def vectorized_entry_signals(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    vwap: np.ndarray,
    score: np.ndarray,
    sqz_on: np.ndarray,
    mom_state: np.ndarray,
    regime: np.ndarray,
    entry_score: float,
    mom_state_long: int,
    mom_state_short: int,
    regime_filter_mode: int,  # 0=loose, 1=mid, 2=strict
    use_pb: bool,
    in_pb_zone: np.ndarray,
    is_new_high: np.ndarray,
    is_new_low: np.ndarray,
    pb_confirm_bars: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    向量化進場信號生成
    
    Args:
        close, high, low, vwap: 價格數據
        score, sqz_on, mom_state, regime: 指標數據
        entry_score: 進場分數門檻
        mom_state_long: 多頭動能條件
        mom_state_short: 空頭動能條件
        regime_filter_mode: 趨勢過濾模式
        use_pb: 是否使用回測進場
        in_pb_zone, is_new_high, is_new_low: 回測相關指標
        pb_confirm_bars: 回測確認 K 棒數
    
    Returns:
        long_signals: 多頭進場信號 (bool array)
        short_signals: 空頭進場信號 (bool array)
    """
    n = len(close)
    long_signals = np.zeros(n, dtype=nb.bool_)
    short_signals = np.zeros(n, dtype=nb.bool_)
    
    for i in nb.prange(n):
        # 趨勢過濾
        can_long = True
        can_short = True
        
        if regime_filter_mode == 1:  # mid
            ema_filter = close[i] * 1.001  # 簡化
            can_long = close[i] > ema_filter * 0.998
            can_short = close[i] < ema_filter * 1.002
        elif regime_filter_mode == 2:  # strict
            ema_filter = close[i] * 1.001
            can_long = close[i] > ema_filter * 0.999
            can_short = close[i] < ema_filter * 1.001
        
        # 多頭進場條件
        sqz_buy = (not sqz_on[i]) and (score[i] >= entry_score) and (close[i] > vwap[i])
        sqz_buy = sqz_buy and (mom_state[i] >= mom_state_long)
        
        pb_buy = False
        if use_pb and i >= pb_confirm_bars:
            pb_buy = in_pb_zone[i] and (close[i] > open_price(i, close))
            if pb_buy:
                # 檢查最近 N 根是否有新高
                for j in range(i - pb_confirm_bars, i):
                    if is_new_high[j]:
                        pb_buy = True
                        break
        
        if (sqz_buy or pb_buy) and can_long:
            long_signals[i] = True
        
        # 空頭進場條件
        sqz_sell = (not sqz_on[i]) and (score[i] <= -entry_score) and (close[i] < vwap[i])
        sqz_sell = sqz_sell and (mom_state[i] <= mom_state_short)
        
        pb_sell = False
        if use_pb and i >= pb_confirm_bars:
            pb_sell = in_pb_zone[i] and (close[i] < open_price(i, close))
            if pb_sell:
                for j in range(i - pb_confirm_bars, i):
                    if is_new_low[j]:
                        pb_sell = True
                        break
        
        if (sqz_sell or pb_sell) and can_short:
            short_signals[i] = True
    
    return long_signals, short_signals


@nb.njit
def open_price(i: int, close: np.ndarray) -> float:
    """簡化：用前一根收盤價代替開盤價"""
    if i > 0:
        return close[i-1]
    return close[i]


@nb.njit(parallel=True, cache=True)
def vectorized_exit_signals(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    vwap: np.ndarray,
    entry_prices: np.ndarray,
    positions: np.ndarray,
    stop_loss_pts: float,
    tp1_pts: float,
    tp1_lots: int,
    lots_per_trade: int,
    exit_on_vwap: bool,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    向量化出場信號
    
    Args:
        close, high, low, vwap: 價格數據
        entry_prices: 進場價格
        positions: 部位方向
        stop_loss_pts: 停損點數
        tp1_pts: 第一目標點數
        tp1_lots: 第一目標口數
        lots_per_trade: 總口數
        exit_on_vwap: VWAP 離場
    
    Returns:
        exit_signals: 出場信號
        exit_prices: 出場價格
        exit_reasons: 出場原因 (0=SL, 1=TP1, 2=VWAP, 3=EOD)
    """
    n = len(close)
    exit_signals = np.zeros(n, dtype=nb.bool_)
    exit_prices = np.zeros(n)
    exit_reasons = np.zeros(n, dtype=nb.int8)
    
    for i in nb.prange(n):
        if positions[i] == 0:
            continue
        
        pos = positions[i]
        entry = entry_prices[i]
        
        # 停損檢查
        if pos > 0:  # 多單
            sl_price = entry - stop_loss_pts
            if low[i] <= sl_price:
                exit_signals[i] = True
                exit_prices[i] = sl_price
                exit_reasons[i] = 0
                continue
            
            # 分批停利
            if lots_per_trade >= 2 and tp1_lots > 0:
                tp1_price = entry + tp1_pts
                if high[i] >= tp1_price:
                    # 部分平倉（簡化：記錄為出場）
                    exit_signals[i] = True
                    exit_prices[i] = tp1_price
                    exit_reasons[i] = 1
                    continue
            
            # VWAP 離場
            if exit_on_vwap and close[i] < vwap[i]:
                exit_signals[i] = True
                exit_prices[i] = close[i]
                exit_reasons[i] = 2
                continue
        
        else:  # 空單
            sl_price = entry + stop_loss_pts
            if high[i] >= sl_price:
                exit_signals[i] = True
                exit_prices[i] = sl_price
                exit_reasons[i] = 0
                continue
            
            # 分批停利
            if lots_per_trade >= 2 and tp1_lots > 0:
                tp1_price = entry - tp1_pts
                if low[i] <= tp1_price:
                    exit_signals[i] = True
                    exit_prices[i] = tp1_price
                    exit_reasons[i] = 1
                    continue
            
            # VWAP 離場
            if exit_on_vwap and close[i] > vwap[i]:
                exit_signals[i] = True
                exit_prices[i] = close[i]
                exit_reasons[i] = 2
                continue
    
    return exit_signals, exit_prices, exit_reasons


@nb.njit(parallel=True, cache=True)
def calculate_portfolio_metrics(
    entry_prices: np.ndarray,
    exit_prices: np.ndarray,
    positions: np.ndarray,
    exit_signals: np.ndarray,
    point_value: float,
    fee_per_side: float,
    exchange_fee: float,
    tax_rate: float,
    initial_balance: float,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    計算投資組合績效指標
    
    Returns:
        pnl_cash: 每筆交易的現金損益
        pnl_cumsum: 累計損益
        equity_curve: 權益曲線
        drawdown: 回撤
    """
    n = len(entry_prices)
    pnl_cash = np.zeros(n)
    pnl_cumsum = np.zeros(n)
    equity_curve = np.zeros(n)
    drawdown = np.zeros(n)
    
    cumulative_pnl = 0.0
    peak_equity = initial_balance
    
    for i in nb.prange(n):
        if exit_signals[i] and positions[i] != 0:
            # 計算損益
            entry = entry_prices[i]
            exit_p = exit_prices[i]
            pos = positions[i]
            
            # 點數損益
            if pos > 0:
                pnl_pts = exit_p - entry
            else:
                pnl_pts = entry - exit_p
            
            # 現金損益（扣除費用）
            gross_pnl = pnl_pts * point_value
            fees = (fee_per_side + exchange_fee) * 2
            tax = (entry + exit_p) * point_value * tax_rate
            pnl_cash[i] = gross_pnl - fees - tax
            
            cumulative_pnl += pnl_cash[i]
        
        pnl_cumsum[i] = cumulative_pnl
        equity = initial_balance + cumulative_pnl
        equity_curve[i] = equity
        
        # 計算回撤
        if equity > peak_equity:
            peak_equity = equity
        drawdown[i] = peak_equity - equity
    
    return pnl_cash, pnl_cumsum, equity_curve, drawdown


def advanced_metrics(
    pnl_cash: np.ndarray,
    equity_curve: np.ndarray,
    drawdown: np.ndarray,
    initial_balance: float,
    risk_free_rate: float = 0.02,
) -> Dict[str, float]:
    """
    計算進階績效指標
    
    Returns:
        字典包含：
        - total_return: 總報酬率
        - sharpe_ratio: 夏普比率
        - max_drawdown: 最大回撤
        - ulcer_index: 潰瘍指數
        - recovery_factor: 修復因子
        - expectancy: 期望值
        - win_rate: 勝率
        - profit_factor: 盈虧比
        - avg_trade: 平均交易損益
    """
    # 過濾零值
    trades = pnl_cash[pnl_cash != 0]
    
    if len(trades) == 0:
        return {
            'total_return': 0,
            'sharpe_ratio': 0,
            'max_drawdown': 0,
            'ulcer_index': 0,
            'recovery_factor': 0,
            'expectancy': 0,
            'win_rate': 0,
            'profit_factor': 0,
            'avg_trade': 0,
        }
    
    # 總報酬率
    total_return = (equity_curve[-1] - initial_balance) / initial_balance
    
    # 夏普比率（年化）
    if len(equity_curve) > 1:
        returns = np.diff(equity_curve) / equity_curve[:-1]
        if np.std(returns) > 0:
            sharpe_ratio = np.mean(returns) / np.std(returns) * np.sqrt(252 * 78)  # 5m K 棒
        else:
            sharpe_ratio = 0
    else:
        sharpe_ratio = 0
    
    # 最大回撤
    max_dd = np.max(drawdown)
    
    # 潰瘍指數
    ulcer_index = np.sqrt(np.mean((drawdown / initial_balance) ** 2)) * 100
    
    # 修復因子
    if max_dd > 0:
        recovery_factor = (equity_curve[-1] - initial_balance) / max_dd
    else:
        recovery_factor = 0
    
    # 期望值
    winning_trades = trades[trades > 0]
    losing_trades = trades[trades < 0]
    
    if len(trades) > 0:
        expectancy = np.sum(trades) / len(trades)
    else:
        expectancy = 0
    
    # 勝率
    win_rate = len(winning_trades) / len(trades) * 100 if len(trades) > 0 else 0
    
    # 盈虧比
    gross_profit = np.sum(winning_trades) if len(winning_trades) > 0 else 0
    gross_loss = abs(np.sum(losing_trades)) if len(losing_trades) > 0 else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf
    
    # 平均交易
    avg_trade = np.mean(trades)
    
    return {
        'total_return': total_return,
        'sharpe_ratio': sharpe_ratio,
        'max_drawdown': max_dd,
        'ulcer_index': ulcer_index,
        'recovery_factor': recovery_factor,
        'expectancy': expectancy,
        'win_rate': win_rate,
        'profit_factor': profit_factor,
        'avg_trade': avg_trade,
        'total_trades': len(trades),
        'winning_trades': len(winning_trades),
        'losing_trades': len(losing_trades),
    }


class VectorizedBacktester:
    """
    向量化回測器
    
    使用方式：
    1. 準備數據：OHLCV + 指標
    2. 定義參數網格
    3. 執行回測
    4. 分析結果
    """
    
    def __init__(self, df: pd.DataFrame, config: BacktestConfig):
        """
        Args:
            df: 市場數據（需包含 Open, High, Low, Close, Volume, vwap, score, sqz_on, mom_state, regime, in_pb_zone, is_new_high, is_new_low）
            config: 回測配置
        """
        self.df = df
        self.config = config
        
        # 轉換為 NumPy 陣列
        self.close = df['Close'].values
        self.high = df['High'].values
        self.low = df['Low'].values
        self.vwap = df['vwap'].values if 'vwap' in df.columns else self.close
        self.score = df['score'].values if 'score' in df.columns else np.zeros(len(df))
        self.sqz_on = df['sqz_on'].values if 'sqz_on' in df.columns else np.zeros(len(df), dtype=bool)
        self.mom_state = df['mom_state'].values if 'mom_state' in df.columns else np.zeros(len(df))
        self.regime = df['regime'].values if 'regime' in df.columns else np.array(['NORMAL'] * len(df))
        self.in_pb_zone = df['in_pb_zone'].values if 'in_pb_zone' in df.columns else np.zeros(len(df), dtype=bool)
        self.is_new_high = df['is_new_high'].values if 'is_new_high' in df.columns else np.zeros(len(df), dtype=bool)
        self.is_new_low = df['is_new_low'].values if 'is_new_low' in df.columns else np.zeros(len(df), dtype=bool)
        
        # 轉換 regime 為數值
        self.regime_num = np.zeros(len(df), dtype=np.int8)
        for i, r in enumerate(self.regime):
            if r == 'STRONG':
                self.regime_num[i] = 1
            elif r == 'WEAK':
                self.regime_num[i] = 2
        
        console.print(f"[green]載入 {len(df)} 筆數據[/green]")
    
    def run_backtest(
        self,
        entry_score: float = 30,
        mom_state_long: int = 2,
        mom_state_short: int = 1,
        regime_filter_mode: int = 0,  # 0=loose, 1=mid, 2=strict
        use_pb: bool = True,
        pb_confirm_bars: int = 12,
        stop_loss_pts: float = 30,
        tp1_pts: float = 30,
        tp1_lots: int = 1,
        exit_on_vwap: bool = True,
    ) -> Dict:
        """
        執行單一回測
        
        Returns:
            回測結果字典
        """
        # 1. 生成進場信號
        long_signals, short_signals = vectorized_entry_signals(
            self.close, self.high, self.low, self.vwap, self.score,
            self.sqz_on, self.mom_state, self.regime_num,
            entry_score, mom_state_long, mom_state_short,
            regime_filter_mode, use_pb, self.in_pb_zone,
            self.is_new_high, self.is_new_low, pb_confirm_bars,
        )
        
        # 2. 模擬部位建立（簡化：假設立即成交）
        n = len(self.close)
        positions = np.zeros(n)
        entry_prices = np.zeros(n)
        
        position = 0
        entry_price = 0
        
        for i in range(n):
            if position == 0:
                if long_signals[i]:
                    position = self.config.lots_per_trade
                    entry_price = self.close[i]
                elif short_signals[i]:
                    position = -self.config.lots_per_trade
                    entry_price = self.close[i]
            
            positions[i] = position
            entry_prices[i] = entry_price if position != 0 else 0
        
        # 3. 生成出場信號
        exit_signals, exit_prices, exit_reasons = vectorized_exit_signals(
            self.close, self.high, self.low, self.vwap,
            entry_prices, positions,
            stop_loss_pts, tp1_pts, tp1_lots,
            self.config.lots_per_trade, exit_on_vwap,
        )
        
        # 4. 計算績效
        pnl_cash, pnl_cumsum, equity_curve, drawdown = calculate_portfolio_metrics(
            entry_prices, exit_prices, positions, exit_signals,
            self.config.point_value, self.config.fee_per_side,
            self.config.exchange_fee, self.config.tax_rate,
            self.config.initial_balance,
        )
        
        # 5. 計算進階指標
        metrics = advanced_metrics(
            pnl_cash, equity_curve, drawdown,
            self.config.initial_balance,
        )
        
        # 6. 返回結果
        return {
            'params': {
                'entry_score': entry_score,
                'mom_state_long': mom_state_long,
                'mom_state_short': mom_state_short,
                'regime_filter_mode': regime_filter_mode,
                'use_pb': use_pb,
                'stop_loss_pts': stop_loss_pts,
                'tp1_pts': tp1_pts,
                'tp1_lots': tp1_lots,
                'exit_on_vwap': exit_on_vwap,
            },
            'metrics': metrics,
            'results': {
                'pnl_cash': pnl_cash,
                'pnl_cumsum': pnl_cumsum,
                'equity_curve': equity_curve,
                'drawdown': drawdown,
                'positions': positions,
                'entry_prices': entry_prices,
                'exit_prices': exit_prices,
                'exit_signals': exit_signals,
                'exit_reasons': exit_reasons,
            },
        }
    
    def run_parameter_grid(
        self,
        param_grid: Dict[str, list],
        progress_callback=None,
    ) -> pd.DataFrame:
        """
        執行參數網格回測
        
        Args:
            param_grid: 參數網格字典
            progress_callback: 進度回呼函數
        
        Returns:
            DataFrame 包含所有參數組合的結果
        """
        from itertools import product
        
        # 生成所有參數組合
        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combinations = list(product(*values))
        
        console.print(f"[yellow]測試 {len(combinations)} 種參數組合[/yellow]")
        
        results = []
        
        for i, combo in enumerate(combinations):
            params = dict(zip(keys, combo))
            
            result = self.run_backtest(**params)
            result['metrics']['params'] = params
            results.append(result['metrics'])
            
            if progress_callback:
                progress_callback(i + 1, len(combinations))
            elif (i + 1) % 20 == 0:
                console.print(f"[dim]進度：{i+1}/{len(combinations)}[/dim]")
        
        # 轉換為 DataFrame
        df_results = pd.DataFrame(results)
        
        # 展開 params 欄位
        params_df = pd.DataFrame([r['params'] for r in results])
        for col in params_df.columns:
            df_results[f'param_{col}'] = params_df[col]
        
        return df_results
