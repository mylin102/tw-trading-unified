#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真實下單模組 - 風險管理器

職責:
- 部位限制檢查
- 資金檢查
- 單日虧損檢查
- 訂單大小檢查
- 熔斷機制

V-Cycle 對應:
- L3: 模組設計
- R3: 模組驗證 (tests/live_trading/test_risk_manager.py)
"""

import datetime
from typing import Optional
from pathlib import Path
import sys

# 確保能匯入同目錄下的模組
sys.path.append(str(Path(__file__).parent.parent))

from rich.console import Console
console = Console()


class RiskManager:
    """風險管理器"""
    
    def __init__(self, api=None, config: dict = None, initial_capital: float = 40000.0):
        """
        初始化風險管理器
        
        Args:
            api: Shioaji API 實例
            config: 配置字典
            initial_capital: 期初資金 (預設 40,000 TWD)
        """
        self.api = api
        self.config = config or {}
        self.initial_capital = initial_capital
        self.available_capital = initial_capital
        
        # 部位追蹤
        self.current_position = 0  # 總口數
        self.long_position = 0     # 多單口數
        self.short_position = 0    # 空單口數
        
        # 單日統計
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.daily_wins = 0
        self.daily_losses = 0
        
        # 連續虧損追蹤 (熔斷機制)
        self.consecutive_losses = 0
        self.last_trade_pnl = 0.0
        
        # 風險參數 (從配置讀取)
        self.max_position_size = self.config.get('max_position_size', 0.05)  # 5% 資金
        self.max_daily_loss = self.config.get('max_daily_loss', 0.02)        # 2% 資金
        self.max_order_quantity = self.config.get('max_order_quantity', 1)   # 1 口 (第一次實盤)
        self.max_total_exposure = self.config.get('max_total_exposure', 0.30) # 30% 資金
        self.max_consecutive_losses = self.config.get('max_consecutive_losses', 5)  # 5 次
        
        # 熔斷機制
        self.circuit_breaker = False
        self.circuit_breaker_triggered_at: Optional[datetime.datetime] = None
        
        # 每口保證金 (簡化，實際應從 API 獲取)
        # 選擇權買方不需要保證金，這裡用於計算曝險
        self.margin_per_contract = self.config.get('margin_per_contract', 20000.0)  # 選擇權買方預估 2 萬
        
        console.print("[bold green]✅ RiskManager 已初始化[/bold green]")
        console.print(f"   期初資金：{initial_capital:,.0f} TWD")
        console.print(f"   單日最大虧損：{self.max_daily_loss*100:.1f}%")
        console.print(f"   最大訂單口數：{self.max_order_quantity} 口")
        
        # 實際交易模式警告
        if self.config.get('live_trading', False):
            console.print("[bold yellow]⚠️  實際交易模式 - 請謹慎操作！[/bold yellow]")
        else:
            console.print("[dim]📝 模擬交易模式[/dim]")
    
    def check_order(self, contract, quantity: int) -> bool:
        """
        檢查訂單是否符合風險限制
        
        Args:
            contract: 合約物件
            quantity: 訂單數量
        
        Returns:
            True (符合), False (不符合)
        """
        # 1. 檢查熔斷機制
        if self.circuit_breaker:
            console.print("[red]❌ 熔斷機制已觸發，禁止下單[/red]")
            return False
        
        # 2. 檢查訂單大小
        if not self._check_order_size(quantity):
            return False
        
        # 3. 檢查總部位
        if not self._check_total_position(quantity):
            return False
        
        # 4. 檢查單日虧損
        if not self._check_daily_loss():
            return False
        
        # 5. 檢查資金 (本地計算)
        if not self._check_capital(quantity):
            return False

        # 6. 檢查帳務保證金 (API 實時查詢)
        if not self._check_account_margin():
            return False
        
        return True
    
    def update_position(self, trade):
        """
        更新部位
        
        Args:
            trade: Trade 物件 (dict with 'action', 'quantity', 'price')
        """
        if isinstance(trade, dict):
            quantity = trade.get('quantity', 0)
            action = trade.get('action', '')
            price = trade.get('price', 0)
        else:
            quantity = getattr(trade, 'quantity', 0)
            action = getattr(trade, 'action', '')
            price = getattr(trade, 'price', 0)
        
        # 更新部位
        if action == 'Buy':
            self.current_position += quantity
            self.long_position += quantity
        elif action == 'Sell':
            self.current_position = max(0, self.current_position - quantity)
            if self.short_position > 0:
                self.short_position = max(0, self.short_position - quantity)
        
        # 更新損益 (如果有 PnL 資訊)
        pnl = getattr(trade, 'pnl', trade.get('pnl', 0)) if isinstance(trade, dict) else getattr(trade, 'pnl', 0)
        if pnl:
            self._update_pnl(pnl)
        
        console.print(f"[dim]部位更新：{self.long_position}多 / {self.short_position}空[/dim]")
    
    def update_pnl(self, pnl: float):
        """
        更新損益
        
        Args:
            pnl: 損益金額
        """
        self._update_pnl(pnl)
    
    def get_position_summary(self) -> dict:
        """獲取部位摘要"""
        return {
            'current_position': self.current_position,
            'long_position': self.long_position,
            'short_position': self.short_position,
            'daily_pnl': self.daily_pnl,
            'daily_pnl_pct': (self.daily_pnl / self.initial_capital * 100) if self.initial_capital > 0 else 0,
            'daily_trades': self.daily_trades,
            'win_rate': (self.daily_wins / self.daily_trades * 100) if self.daily_trades > 0 else 0,
            'consecutive_losses': self.consecutive_losses,
            'circuit_breaker': self.circuit_breaker,
            'available_capital': self.available_capital,
            'used_margin': self.current_position * self.margin_per_contract,
            'margin_ratio': (self.current_position * self.margin_per_contract / self.initial_capital) if self.initial_capital > 0 else 0
        }
    
    def reset_daily(self):
        """重置每日統計"""
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.daily_wins = 0
        self.daily_losses = 0
        self.consecutive_losses = 0
        self.last_trade_pnl = 0.0
        self.circuit_breaker = False
        self.circuit_breaker_triggered_at = None
        
        console.print("[bold green]✅ 每日統計已重置[/bold green]")
    
    def _check_order_size(self, quantity: int) -> bool:
        """檢查訂單大小"""
        if quantity > self.max_order_quantity:
            console.print(f"[red]❌ 訂單大小超限：{quantity} > {self.max_order_quantity}[/red]")
            return False
        
        if quantity <= 0:
            console.print(f"[red]❌ 訂單數量必須大於 0[/red]")
            return False
        
        return True
    
    def _check_total_position(self, quantity: int) -> bool:
        """檢查總部位"""
        # 計算最大允許口數
        max_contracts = max(10, int(self.initial_capital * self.max_total_exposure / self.margin_per_contract))
        
        if self.current_position + quantity > max_contracts:
            console.print(f"[red]❌ 總部位超限：{self.current_position + quantity} > {max_contracts}[/red]")
            return False
        
        return True
    
    def _check_daily_loss(self) -> bool:
        """檢查單日虧損"""
        max_daily_loss_amount = self.initial_capital * self.max_daily_loss
        
        if self.daily_pnl < -max_daily_loss_amount:
            console.print(f"[red]❌ 已達單日最大虧損：{self.daily_pnl:,.0f} < -{max_daily_loss_amount:,.0f}[/red]")
            return False
        
        return True
    
    def _check_capital(self, quantity: int) -> bool:
        """檢查資金"""
        # 選擇權保證金較低，這裡簡化計算
        # 實際應從 API 獲取正確的保證金
        required_margin = quantity * self.margin_per_contract * 0.1  # 選擇權約 10%
        
        if required_margin > self.available_capital:
            console.print(f"[red]❌ 資金不足：{required_margin:,.0f} > {self.available_capital:,.0f}[/red]")
            return False
        
        return True

    def _check_account_margin(self) -> bool:
        """檢查帳務保證金 (從 API 獲取真實數據)"""
        if self.api is None or not hasattr(self.api, "get_account_margin"):
            return True # 模擬模式或無 API 時跳過
            
        try:
            margin = self.api.get_account_margin()
            # 取得可用權益數 (Equity)
            available_margin = float(margin.equity)
            
            if available_margin < self.margin_per_contract:
                console.print(f"[red]❌ 帳戶可用資金不足：{available_margin:,.0f} < {self.margin_per_contract:,.0f}[/red]")
                return False
            
            # 更新本地可用資金記錄
            self.available_capital = available_margin
            return True
        except Exception as e:
            console.print(f"[yellow]⚠️  無法獲取帳務資訊：{e}，使用本地風險檢查。[/yellow]")
            return True # 失敗時預設通過，依賴本地檢查
    
    def _update_pnl(self, pnl: float):
        """更新損益"""
        self.daily_pnl += pnl
        self.daily_trades += 1
        self.last_trade_pnl = pnl
        
        if pnl > 0:
            self.daily_wins += 1
            self.consecutive_losses = 0  # 重置連續虧損
        else:
            self.daily_losses += 1
            self.consecutive_losses += 1
            
            # 檢查熔斷機制
            if self.consecutive_losses >= self.max_consecutive_losses:
                self._trigger_circuit_breaker()
    
    def _trigger_circuit_breaker(self):
        """觸發熔斷機制"""
        self.circuit_breaker = True
        self.circuit_breaker_triggered_at = datetime.datetime.now()
        
        console.print(f"[bold red]🚨 熔斷機制已觸發！連續虧損 {self.consecutive_losses} 次[/bold red]")
        console.print(f"   觸發時間：{self.circuit_breaker_triggered_at.strftime('%Y-%m-%d %H:%M:%S')}")
        console.print(f"   請立即檢查策略與市場狀況")
