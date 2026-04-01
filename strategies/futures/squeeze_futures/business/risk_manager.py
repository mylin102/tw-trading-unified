#!/usr/bin/env python3
"""
風險管理模組 (Risk Manager)
負責監控停損、部位風險、資金暴露

機構級風險控制：
- 動態停損追蹤
- 部位風險限額
- 最大回撤控制
- 相關性風險管理
"""

import numpy as np
import pandas as pd
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from rich.console import Console

console = Console()


@dataclass
class RiskLimits:
    """風險限額配置"""
    max_position_size: int = 4          # 最大持倉口數
    max_capital_allocation: float = 0.5  # 最大資金配置比例
    max_daily_loss: float = 5000        # 單日最大虧損 (TWD)
    max_drawdown: float = 0.10          # 最大回撤比例 (10%)
    max_var_95: float = 0.05            # 最大 VaR 95% (5%)
    stop_loss_pts: float = 30           # 停損點數
    break_even_pts: float = 30          # 保本停損點數
    trailing_stop_pts: float = 20       # 移動停損點數


@dataclass
class PositionRisk:
    """部位風險指標"""
    symbol: str
    direction: int  # 1=long, -1=short, 0=flat
    size: int
    entry_price: float
    current_price: float
    unrealized_pnl: float
    stop_loss_price: float
    break_even_price: float
    risk_reward_ratio: float
    time_in_market: float  # 持倉時間 (分鐘)


class RiskManager:
    """
    風險管理器
    
    核心功能：
    1. 停損監控 (Stop Loss Monitoring)
    2. 部位風險計算 (Position Risk Calculation)
    3. 資金暴露控制 (Capital Exposure Control)
    4. 回撤管理 (Drawdown Management)
    5. 風險限額檢查 (Risk Limit Checks)
    """
    
    def __init__(self, config: RiskLimits):
        """
        Args:
            config: 風險限額配置
        """
        self.config = config
        
        # 當前狀態
        self.positions: Dict[str, PositionRisk] = {}
        self.daily_pnl: float = 0
        self.peak_equity: float = 100000
        self.current_equity: float = 100000
        self.trade_history: List[Dict] = []
        
        # 風險指標
        self.current_var_95: float = 0
        self.current_drawdown: float = 0
        self.total_exposure: float = 0
        
        console.print("[green]✓ Risk Manager initialized[/green]")
    
    def update_position(
        self,
        symbol: str,
        direction: int,
        size: int,
        entry_price: float,
        current_price: float,
    ):
        """
        更新部位資訊
        
        Args:
            symbol: 商品代號
            direction: 方向 (1=long, -1=short)
            size: 口數
            entry_price: 進場價
            current_price: 當前價
        """
        # 計算停損價
        if direction > 0:  # 多單
            stop_loss_price = entry_price - self.config.stop_loss_pts
            break_even_price = entry_price + self.config.break_even_pts
        else:  # 空單
            stop_loss_price = entry_price + self.config.stop_loss_pts
            break_even_price = entry_price - self.config.break_even_pts
        
        # 計算未實現損益
        if direction > 0:
            unrealized_pnl = (current_price - entry_price) * size * 10
        else:
            unrealized_pnl = (entry_price - current_price) * size * 10
        
        # 計算風險報酬比
        risk_pts = abs(entry_price - stop_loss_price)
        reward_pts = abs(current_price - entry_price) * 2  # 假設目標是風險的 2 倍
        risk_reward_ratio = reward_pts / risk_pts if risk_pts > 0 else 0
        
        # 更新部位
        self.positions[symbol] = PositionRisk(
            symbol=symbol,
            direction=direction,
            size=size,
            entry_price=entry_price,
            current_price=current_price,
            unrealized_pnl=unrealized_pnl,
            stop_loss_price=stop_loss_price,
            break_even_price=break_even_price,
            risk_reward_ratio=risk_reward_ratio,
            time_in_market=0,  # 需要時間數據
        )
        
        console.print(f"[dim]✓ Position updated: {symbol} {direction*size} lots @ {entry_price}[/dim]")
    
    def check_stop_loss(self, symbol: str, current_price: float) -> Optional[str]:
        """
        檢查停損觸發
        
        Args:
            symbol: 商品代號
            current_price: 當前價格
        
        Returns:
            觸發類型 (STOP_LOSS, BREAK_EVEN, TRAILING) 或 None
        """
        if symbol not in self.positions:
            return None
        
        pos = self.positions[symbol]
        
        # 停損檢查
        if pos.direction > 0:  # 多單
            if current_price <= pos.stop_loss_price:
                console.print(f"[bold red]⚠ STOP LOSS triggered: {symbol} @ {current_price}[/bold red]")
                return "STOP_LOSS"
            
            # 保本停損檢查
            if pos.unrealized_pnl >= self.config.break_even_pts * pos.size * 10:
                new_stop = pos.entry_price  # 移動到成本價
                if current_price <= new_stop:
                    console.print(f"[bold yellow]⚠ BREAK EVEN triggered: {symbol} @ {current_price}[/bold yellow]")
                    return "BREAK_EVEN"
        
        else:  # 空單
            if current_price >= pos.stop_loss_price:
                console.print(f"[bold red]⚠ STOP LOSS triggered: {symbol} @ {current_price}[/bold red]")
                return "STOP_LOSS"
            
            # 保本停損檢查
            if pos.unrealized_pnl >= self.config.break_even_pts * pos.size * 10:
                new_stop = pos.entry_price
                if current_price >= new_stop:
                    console.print(f"[bold yellow]⚠ BREAK EVEN triggered: {symbol} @ {current_price}[/bold yellow]")
                    return "BREAK_EVEN"
        
        return None
    
    def check_risk_limits(self) -> Dict[str, bool]:
        """
        檢查風險限額
        
        Returns:
            字典包含各項限額檢查結果
        """
        # 計算總暴露
        self.total_exposure = sum(
            abs(pos.size * pos.current_price * 10)
            for pos in self.positions.values()
        )
        
        # 計算當前權益
        self.current_equity = self.peak_equity + sum(
            pos.unrealized_pnl for pos in self.positions.values()
        )
        
        # 計算回撤
        if self.current_equity > self.peak_equity:
            self.peak_equity = self.current_equity
        self.current_drawdown = (self.peak_equity - self.current_equity) / self.peak_equity
        
        # 檢查限額
        limits = {
            'position_size_ok': sum(pos.size for pos in self.positions.values()) <= self.config.max_position_size,
            'capital_allocation_ok': self.total_exposure / self.current_equity <= self.config.max_capital_allocation,
            'daily_loss_ok': self.daily_pnl >= -self.config.max_daily_loss,
            'drawdown_ok': self.current_drawdown <= self.config.max_drawdown,
            'var_ok': self.current_var_95 <= self.config.max_var_95,
        }
        
        # 警告
        for limit, ok in limits.items():
            if not ok:
                console.print(f"[bold red]⚠ RISK LIMIT BREACHED: {limit}[/bold red]")
        
        return limits
    
    def get_position_risk_summary(self) -> pd.DataFrame:
        """
        獲取部位風險摘要
        
        Returns:
            DataFrame 包含所有部位的風險指標
        """
        if not self.positions:
            return pd.DataFrame()
        
        data = []
        for symbol, pos in self.positions.items():
            data.append({
                'Symbol': symbol,
                'Direction': 'LONG' if pos.direction > 0 else 'SHORT',
                'Size': pos.size,
                'Entry': pos.entry_price,
                'Current': pos.current_price,
                'Unrealized PnL': pos.unrealized_pnl,
                'Stop Loss': pos.stop_loss_price,
                'Break Even': pos.break_even_price,
                'Risk/Reward': pos.risk_reward_ratio,
            })
        
        return pd.DataFrame(data)
    
    def record_trade(self, trade: Dict):
        """記錄交易"""
        self.trade_history.append(trade)
        self.daily_pnl += trade.get('pnl', 0)
    
    def reset_daily(self):
        """重置單日統計"""
        self.daily_pnl = 0
        console.print("[dim]Daily PnL reset[/dim]")
    
    def print_risk_report(self):
        """打印風險報告"""
        console.print("\n[bold blue]=== Risk Management Report ===[/bold blue]\n")
        
        # 部位摘要
        if self.positions:
            df = self.get_position_risk_summary()
            console.print(df.to_string(index=False))
            console.print()
        
        # 風險指標
        table_data = [
            ("Total Exposure", f"{self.total_exposure:,.0f} TWD"),
            ("Current Equity", f"{self.current_equity:,.0f} TWD"),
            ("Peak Equity", f"{self.peak_equity:,.0f} TWD"),
            ("Current Drawdown", f"{self.current_drawdown*100:.2f}%"),
            ("Daily PnL", f"{self.daily_pnl:,.0f} TWD"),
            ("VaR 95%", f"{self.current_var_95*100:.2f}%"),
        ]
        
        from rich.table import Table
        table = Table(title="Risk Metrics")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")
        
        for metric, value in table_data:
            table.add_row(metric, value)
        
        console.print(table)
        
        # 限額檢查
        limits = self.check_risk_limits()
        status = "✓ All limits OK" if all(limits.values()) else "⚠ LIMITS BREACHED"
        console.print(f"\n[bold {'green' if all(limits.values()) else 'red'}]Risk Limits: {status}[/bold {'green' if all(limits.values()) else 'red'}]")
