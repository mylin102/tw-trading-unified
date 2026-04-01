#!/usr/bin/env python3
"""
資金控制模組 (Capital Manager)
負責資金配置、部位 sizing、風險預算

機構級資金管理：
- Kelly 公式優化
- 風險平價配置
- 動態部位調整
- 資金使用效率
"""

import numpy as np
from typing import Dict, Optional, List
from dataclasses import dataclass
from rich.console import Console

console = Console()


@dataclass
class CapitalConfig:
    """資金配置"""
    initial_capital: float = 100000      # 初始資金
    max_capital_usage: float = 0.5       # 最大資金使用率 (50%)
    risk_per_trade: float = 0.02         # 每筆交易風險 (2%)
    max_position_value: float = 50000    # 單一部位最大價值
    min_position_value: float = 5000     # 單一部位最小價值
    margin_per_lot: float = 25000        # 每口保證金 (TMF)


@dataclass
class PositionSizing:
    """部位計算結果"""
    lots: int
    position_value: float
    risk_amount: float
    risk_reward_ratio: float
    kelly_fraction: float


class CapitalManager:
    """
    資金管理器
    
    核心功能：
    1. 部位計算 (Position Sizing)
    2. Kelly 公式優化
    3. 資金使用率監控
    4. 風險預算分配
    5. 動態調整建議
    """
    
    def __init__(self, config: CapitalConfig):
        """
        Args:
            config: 資金配置
        """
        self.config = config
        
        # 當前狀態
        self.current_capital: float = config.initial_capital
        self.allocated_capital: float = 0
        self.positions: Dict[str, PositionSizing] = {}
        
        # 績效追蹤
        self.trade_results: List[float] = []
        self.win_rate: float = 0.5
        self.avg_win: float = 0
        self.avg_loss: float = 0
        
        console.print("[green]✓ Capital Manager initialized[/green]")
    
    def calculate_position_size(
        self,
        symbol: str,
        entry_price: float,
        stop_loss_price: float,
        point_value: float = 10,
    ) -> PositionSizing:
        """
        計算最佳部位大小
        
        使用三種方法：
        1. 固定風險法 (Fixed Risk)
        2. Kelly 公式法
        3. 資金限制法
        
        Args:
            symbol: 商品代號
            entry_price: 進場價
            stop_loss_price: 停損價
            point_value: 每點價值
        
        Returns:
            PositionSizing 結果
        """
        # 1. 計算風險點數
        risk_pts = abs(entry_price - stop_loss_price)
        if risk_pts == 0:
            risk_pts = 30  # 預設風險
        
        # 2. 固定風險法：每筆交易風險固定為資金的 2%
        risk_amount = self.current_capital * self.config.risk_per_trade
        lots_by_risk = int(risk_amount / (risk_pts * point_value))
        
        # 3. Kelly 公式法
        kelly_fraction = self._calculate_kelly()
        lots_by_kelly = int((self.current_capital * kelly_fraction) / (entry_price * point_value))
        
        # 4. 資金限制法
        max_lots_by_capital = int(
            self.current_capital * self.config.max_capital_usage / 
            self.config.margin_per_lot
        )
        
        # 5. 部位價值限制
        max_lots_by_value = int(
            self.config.max_position_value / (entry_price * point_value)
        )
        min_lots_by_value = int(
            self.config.min_position_value / (entry_price * point_value)
        )
        
        # 6. 取最小值 (最保守)
        lots = min(
            lots_by_risk,
            lots_by_kelly,
            max_lots_by_capital,
            max_lots_by_value,
        )
        
        # 確保不低於最小
        lots = max(lots, min_lots_by_value)
        
        # 確保至少 1 口
        lots = max(lots, 1)
        
        # 計算結果
        position_value = lots * entry_price * point_value
        risk_reward_ratio = risk_pts * 2 / risk_pts if risk_pts > 0 else 0  # 假設目標是風險的 2 倍
        
        result = PositionSizing(
            lots=lots,
            position_value=position_value,
            risk_amount=risk_amount,
            risk_reward_ratio=risk_reward_ratio,
            kelly_fraction=kelly_fraction,
        )
        
        self.positions[symbol] = result
        
        console.print(
            f"[dim]Position sizing for {symbol}:\n"
            f"  Entry: {entry_price:.0f}, Stop: {stop_loss_price:.0f}\n"
            f"  Risk pts: {risk_pts}, Lots: {lots}\n"
            f"  Position value: {position_value:,.0f} TWD\n"
            f"  Risk amount: {risk_amount:,.0f} TWD\n"
            f"  Kelly fraction: {kelly_fraction:.1%}[/dim]"
        )
        
        return result
    
    def _calculate_kelly(self) -> float:
        """
        計算 Kelly 分數
        
        Kelly 公式：
        f* = (p * b - q) / b
        
        其中：
        - p = 勝率
        - q = 敗率 = 1 - p
        - b = 盈虧比 = 平均獲利 / 平均虧損
        
        使用半 Kelly (更保守)
        """
        if len(self.trade_results) < 10:
            # 數據不足，使用預設 10%
            return 0.1
        
        # 計算勝率
        wins = [t for t in self.trade_results if t > 0]
        losses = [t for t in self.trade_results if t < 0]
        
        if not wins or not losses:
            return 0.1
        
        p = len(wins) / len(self.trade_results)
        q = 1 - p
        
        # 計算盈虧比
        avg_win = np.mean(wins)
        avg_loss = abs(np.mean(losses))
        
        if avg_loss == 0:
            return 0.1
        
        b = avg_win / avg_loss
        
        # Kelly 公式
        kelly = (p * b - q) / b
        
        # 限制範圍 (0-50%)
        kelly = max(0, min(kelly, 0.5))
        
        # 使用半 Kelly (更保守)
        kelly *= 0.5
        
        return kelly
    
    def record_trade_result(self, pnl: float):
        """
        記錄交易結果
        
        Args:
            pnl: 損益 (TWD)
        """
        self.trade_results.append(pnl)
        self.current_capital += pnl
        
        # 更新績效統計 (最近 50 筆)
        if len(self.trade_results) > 50:
            self.trade_results = self.trade_results[-50:]
        
        # 更新勝率
        wins = [t for t in self.trade_results if t > 0]
        losses = [t for t in self.trade_results if t < 0]
        
        if self.trade_results:
            self.win_rate = len(wins) / len(self.trade_results)
        
        if wins:
            self.avg_win = np.mean(wins)
        if losses:
            self.avg_loss = abs(np.mean(losses))
    
    def check_capital_usage(self) -> Dict[str, float]:
        """
        檢查資金使用率
        
        Returns:
            字典包含各項資金指標
        """
        total_allocated = sum(p.position_value for p in self.positions.values())
        
        usage_ratio = total_allocated / self.current_capital
        available_capital = self.current_capital - total_allocated
        
        # 保證金檢查
        total_lots = sum(p.lots for p in self.positions.values())
        required_margin = total_lots * self.config.margin_per_lot
        
        return {
            'total_capital': self.current_capital,
            'allocated_capital': total_allocated,
            'available_capital': available_capital,
            'usage_ratio': usage_ratio,
            'max_usage_ratio': self.config.max_capital_usage,
            'required_margin': required_margin,
            'free_margin': self.current_capital * self.config.max_capital_usage - required_margin,
        }
    
    def get_position_recommendation(
        self,
        symbol: str,
        signal_confidence: float,
    ) -> Dict[str, any]:
        """
        獲取部位建議
        
        Args:
            symbol: 商品代號
            signal_confidence: 信號信心度 (0-1)
        
        Returns:
            建議字典
        """
        # 根據信心度調整風險
        adjusted_risk = self.config.risk_per_trade * signal_confidence
        
        # 計算建議部位
        temp_config = CapitalConfig(
            initial_capital=self.current_capital,
            risk_per_trade=adjusted_risk,
        )
        
        # 返回建議
        return {
            'symbol': symbol,
            'confidence': signal_confidence,
            'adjusted_risk': adjusted_risk,
            'recommendation': 'REDUCE' if signal_confidence < 0.5 else 'MAINTAIN' if signal_confidence < 0.7 else 'INCREASE',
        }
    
    def print_capital_report(self):
        """打印資金報告"""
        from rich.table import Table
        
        console.print("\n[bold blue]=== Capital Management Report ===[/bold blue]\n")
        
        # 資金使用
        usage = self.check_capital_usage()
        
        table = Table(title="Capital Usage")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")
        
        table.add_row("Total Capital", f"{usage['total_capital']:,.0f} TWD")
        table.add_row("Allocated", f"{usage['allocated_capital']:,.0f} TWD")
        table.add_row("Available", f"{usage['available_capital']:,.0f} TWD")
        table.add_row("Usage Ratio", f"{usage['usage_ratio']:.1%}")
        table.add_row("Max Usage", f"{usage['max_usage_ratio']:.1%}")
        table.add_row("Required Margin", f"{usage['required_margin']:,.0f} TWD")
        table.add_row("Free Margin", f"{usage['free_margin']:,.0f} TWD")
        
        console.print(table)
        
        # 績效統計
        if self.trade_results:
            console.print(f"\n[bold]Performance Statistics:[/bold]")
            console.print(f"  Total Trades: {len(self.trade_results)}")
            console.print(f"  Win Rate: {self.win_rate:.1%}")
            console.print(f"  Avg Win: {self.avg_win:,.0f} TWD")
            console.print(f"  Avg Loss: {self.avg_loss:,.0f} TWD")
            console.print(f"  Kelly Fraction: {self._calculate_kelly():.1%}")
        
        # 部位建議
        if self.positions:
            console.print(f"\n[bold]Current Positions:[/bold]")
            for symbol, pos in self.positions.items():
                console.print(f"  {symbol}: {pos.lots} lots, {pos.position_value:,.0f} TWD")
