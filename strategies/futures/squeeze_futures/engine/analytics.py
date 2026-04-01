#!/usr/bin/env python3
"""
量化分析模組 (Quant Analytics)
負責績效分析、風險指標和報告生成

靈感來自 vectorbt-pro 的 Analytics 模組
"""

import numpy as np
import pandas as pd
from typing import Dict, Optional, List
from dataclasses import dataclass
from rich.console import Console
from rich.table import Table

console = Console()


@dataclass
class TradeStats:
    """交易統計"""
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float
    avg_trade: float
    expectancy: float


@dataclass
class RiskMetrics:
    """風險指標"""
    total_return: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown: float
    max_drawdown_pct: float
    ulcer_index: float
    ulcer_index_annualized: float
    recovery_factor: float
    var_95: float
    cvar_95: float


@dataclass
class PerformanceMetrics:
    """績效指標"""
    total_pnl: float
    total_return_pct: float
    profit_factor: float
    avg_trade: float
    best_trade: float
    worst_trade: float
    avg_holding_period: float
    trades_per_day: float


class QuantAnalytics:
    """
    量化分析器
    
    功能：
    - 績效指標計算
    - 風險分析
    - 交易統計
    - 報告生成
    """
    
    def __init__(
        self,
        equity_curve: np.ndarray,
        pnl: np.ndarray,
        trades: Optional[pd.DataFrame] = None,
        initial_balance: float = 100000,
        risk_free_rate: float = 0.02,
    ):
        """
        Args:
            equity_curve: 權益曲線
            pnl: 每筆交易損益
            trades: 交易明細 (可選)
            initial_balance: 初始資金
            risk_free_rate: 無風險利率
        """
        self.equity_curve = np.array(equity_curve)
        self.pnl = np.array(pnl)
        self.trades = trades
        self.initial_balance = initial_balance
        self.risk_free_rate = risk_free_rate
        
        # 計算回報
        self.returns = np.diff(self.equity_curve) / self.equity_curve[:-1]
        
        # 計算回撤
        self.drawdown = self._calculate_drawdown()
    
    def _calculate_drawdown(self) -> np.ndarray:
        """計算回撤"""
        peak = np.maximum.accumulate(self.equity_curve)
        drawdown = peak - self.equity_curve
        return drawdown
    
    def get_performance_metrics(self) -> PerformanceMetrics:
        """獲取績效指標"""
        trades = self.pnl[self.pnl != 0]
        
        if len(trades) == 0:
            return PerformanceMetrics(
                total_pnl=0,
                total_return_pct=0,
                profit_factor=0,
                avg_trade=0,
                best_trade=0,
                worst_trade=0,
                avg_holding_period=0,
                trades_per_day=0,
            )
        
        total_pnl = np.sum(trades)
        total_return_pct = total_pnl / self.initial_balance * 100
        
        winning = trades[trades > 0]
        losing = trades[trades < 0]
        
        gross_profit = np.sum(winning) if len(winning) > 0 else 0
        gross_loss = abs(np.sum(losing)) if len(losing) > 0 else 0
        
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf
        
        return PerformanceMetrics(
            total_pnl=total_pnl,
            total_return_pct=total_return_pct,
            profit_factor=profit_factor,
            avg_trade=np.mean(trades),
            best_trade=np.max(trades),
            worst_trade=np.min(trades),
            avg_holding_period=0,  # 需要時間數據
            trades_per_day=len(trades) / max(len(self.equity_curve) / 78, 1),  # 假設 5m K 棒
        )
    
    def get_risk_metrics(self) -> RiskMetrics:
        """獲取風險指標"""
        if len(self.returns) == 0:
            return RiskMetrics(
                total_return=0,
                sharpe_ratio=0,
                sortino_ratio=0,
                calmar_ratio=0,
                max_drawdown=0,
                max_drawdown_pct=0,
                ulcer_index=0,
                ulcer_index_annualized=0,
                recovery_factor=0,
                var_95=0,
                cvar_95=0,
            )
        
        # 總報酬
        total_return = (self.equity_curve[-1] - self.initial_balance) / self.initial_balance
        
        # 夏普比率
        if np.std(self.returns) > 0:
            sharpe = (np.mean(self.returns) - self.risk_free_rate / 252 / 78) / np.std(self.returns)
            sharpe *= np.sqrt(252 * 78)  # 年化
        else:
            sharpe = 0
        
        # 索提諾比率
        downside_returns = self.returns[self.returns < 0]
        if len(downside_returns) > 0 and np.std(downside_returns) > 0:
            sortino = (np.mean(self.returns) - self.risk_free_rate / 252 / 78) / np.std(downside_returns)
            sortino *= np.sqrt(252 * 78)
        else:
            sortino = 0
        
        # 最大回撤
        max_dd = np.max(self.drawdown)
        max_dd_pct = max_dd / self.initial_balance * 100
        
        # Calmar 比率
        if max_dd > 0:
            calmar = total_return / max_dd
        else:
            calmar = 0
        
        # 潰瘍指數 (Ulcer Index) - 衡量回撤的深度與持續時間
        # 公式：sqrt(mean(drawdown_pct^2))
        drawdown_pct = self.drawdown / self.initial_balance
        ulcer_index = np.sqrt(np.mean(drawdown_pct ** 2)) * 100
        
        # 年化潰瘍指數 (假設 5m K 棒，一年約 252*78 根)
        ulcer_index_annualized = ulcer_index * np.sqrt(252 * 78 / len(self.equity_curve))
        
        # 修復因子 (Recovery Factor) = 淨利 / 最大回撤
        recovery = total_return / max_dd if max_dd > 0 else 0
        
        # VaR 95%
        var_95 = np.percentile(self.returns, 5) * 100 if len(self.returns) > 0 else 0
        
        # CVaR 95% (Expected Shortfall)
        cvar_95 = np.mean(self.returns[self.returns <= np.percentile(self.returns, 5)]) * 100 if len(self.returns) > 0 else 0
        
        return RiskMetrics(
            total_return=total_return,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            max_drawdown=max_dd,
            max_drawdown_pct=max_dd_pct,
            ulcer_index=ulcer_index,
            ulcer_index_annualized=ulcer_index_annualized,
            recovery_factor=recovery,
            var_95=var_95,
            cvar_95=cvar_95,
        )
    
    def get_trade_stats(self) -> TradeStats:
        """獲取交易統計"""
        trades = self.pnl[self.pnl != 0]
        
        if len(trades) == 0:
            return TradeStats(
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                win_rate=0,
                avg_win=0,
                avg_loss=0,
                largest_win=0,
                largest_loss=0,
                avg_trade=0,
                expectancy=0,
            )
        
        winning = trades[trades > 0]
        losing = trades[trades < 0]
        
        return TradeStats(
            total_trades=len(trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            win_rate=len(winning) / len(trades) * 100,
            avg_win=np.mean(winning) if len(winning) > 0 else 0,
            avg_loss=np.mean(losing) if len(losing) > 0 else 0,
            largest_win=np.max(winning) if len(winning) > 0 else 0,
            largest_loss=np.min(losing) if len(losing) > 0 else 0,
            avg_trade=np.mean(trades),
            expectancy=np.sum(trades) / len(trades),
        )
    
    def print_report(self):
        """打印分析報告"""
        console.print("[bold blue]=== 量化分析報告 ===[/bold blue]\n")
        
        # 績效指標
        perf = self.get_performance_metrics()
        
        table = Table(title="績效指標")
        table.add_column("指標", style="cyan")
        table.add_column("數值", justify="right")
        
        table.add_row("總損益", f"{perf.total_pnl:,.0f} TWD")
        table.add_row("總報酬率", f"{perf.total_return_pct:.2f}%")
        table.add_row("盈虧比", f"{perf.profit_factor:.2f}")
        table.add_row("平均交易", f"{perf.avg_trade:,.0f} TWD")
        table.add_row("最佳交易", f"{perf.best_trade:,.0f} TWD")
        table.add_row("最差交易", f"{perf.worst_trade:,.0f} TWD")
        table.add_row("日均交易", f"{perf.trades_per_day:.1f}")
        
        console.print(table)
        
        # 風險指標
        console.print()
        risk = self.get_risk_metrics()
        
        table = Table(title="風險指標")
        table.add_column("指標", style="cyan")
        table.add_column("數值", justify="right")
        
        table.add_row("夏普比率", f"{risk.sharpe_ratio:.2f}")
        table.add_row("索提諾比率", f"{risk.sortino_ratio:.2f}")
        table.add_row("Calmar 比率", f"{risk.calmar_ratio:.2f}")
        table.add_row("最大回撤", f"{risk.max_drawdown:,.0f} TWD ({risk.max_drawdown_pct:.1f}%)")
        table.add_row("潰瘍指數", f"{risk.ulcer_index:.2f}")
        table.add_row("修復因子", f"{risk.recovery_factor:.2f}")
        table.add_row("VaR 95%", f"{risk.var_95:.2f}%")
        table.add_row("CVaR 95%", f"{risk.cvar_95:.2f}%")
        
        console.print(table)
        
        # 交易統計
        console.print()
        stats = self.get_trade_stats()
        
        table = Table(title="交易統計")
        table.add_column("指標", style="cyan")
        table.add_column("數值", justify="right")
        
        table.add_row("總交易次數", f"{stats.total_trades}")
        table.add_row("獲利次數", f"{stats.winning_trades}")
        table.add_row("虧損次數", f"{stats.losing_trades}")
        table.add_row("勝率", f"{stats.win_rate:.1f}%")
        table.add_row("平均獲利", f"{stats.avg_win:,.0f} TWD")
        table.add_row("平均虧損", f"{stats.avg_loss:,.0f} TWD")
        table.add_row("最大獲利", f"{stats.largest_win:,.0f} TWD")
        table.add_row("最大虧損", f"{stats.largest_loss:,.0f} TWD")
        table.add_row("期望值", f"{stats.expectancy:,.0f} TWD")
        
        console.print(table)
    
    def to_dict(self) -> Dict:
        """轉換為字典"""
        return {
            'performance': {
                'total_pnl': self.get_performance_metrics().total_pnl,
                'total_return_pct': self.get_performance_metrics().total_return_pct,
                'profit_factor': self.get_performance_metrics().profit_factor,
                'avg_trade': self.get_performance_metrics().avg_trade,
            },
            'risk': {
                'sharpe_ratio': self.get_risk_metrics().sharpe_ratio,
                'sortino_ratio': self.get_risk_metrics().sortino_ratio,
                'max_drawdown': self.get_risk_metrics().max_drawdown,
                'ulcer_index': self.get_risk_metrics().ulcer_index,
                'recovery_factor': self.get_risk_metrics().recovery_factor,
            },
            'trades': {
                'total_trades': self.get_trade_stats().total_trades,
                'win_rate': self.get_trade_stats().win_rate,
                'expectancy': self.get_trade_stats().expectancy,
            },
        }
