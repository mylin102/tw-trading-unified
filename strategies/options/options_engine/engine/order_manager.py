#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⚠️ 遺留程式碼 - 選擇權訂單管理器 (Shioaji API 整合版)
====================================================

⚠️ 注意：此模組已不再使用，系統統一使用 core/order_management/order_manager.py
⚠️ 保留此檔案僅供歷史參考，請勿在新開發中使用

原始說明：
真實下單模組 - 訂單管理器 (Shioaji API 整合版)

職責:
- 訂單生成與提交 (支援 LMT, MKT, STP)
- 移動停損 (Trailing Stop)
- 訂單狀態追蹤
- 訂單生命周期管理 (改單/撤單)
- 訂單取消與修改

V-Cycle 對應:
- L3: 模組設計
- R3: 模組驗證 (tests/live_trading/test_order_manager.py)

Shioaji API 參考:
- 期貨：api.Contracts.Futures.TMF (微台指)
- 選擇權：api.Contracts.Options.TXO
- 訂單類型：LMT (限價), MKT (市價), STP (停損單)
- 委託條件：ROD (當日有效), IOC (立即成交否則取消)

⚠️ 遷移說明：
- 新開發請使用 core/order_management/order_manager.py
- 此檔案中的功能已整合到核心 OrderManager
- 如需類似功能，請參考核心系統的實現
"""

import datetime
from typing import Dict, Optional, Callable
from pathlib import Path
import sys

# 確保能匯入同目錄下的模組
sys.path.append(str(Path(__file__).parent.parent))

from rich.console import Console
console = Console()


class OrderError(Exception):
    """訂單錯誤基類"""
    pass


class OrderRejectedError(OrderError):
    """訂單被拒絕"""
    def __init__(self, reason: str, order_info: dict = None):
        self.reason = reason
        self.order_info = order_info or {}
        super().__init__(f"Order rejected: {reason}")


class OrderTimeoutError(OrderError):
    """訂單超時"""
    def __init__(self, trade_id: str, timeout_secs: int):
        self.trade_id = trade_id
        self.timeout_secs = timeout_secs
        super().__init__(f"Order {trade_id} timeout after {timeout_secs}s")


class RiskLimitError(OrderError):
    """風險限制超限"""
    def __init__(self, limit_type: str, current: float, limit: float):
        self.limit_type = limit_type
        self.current = current
        self.limit = limit
        super().__init__(f"Risk limit exceeded: {limit_type} ({current:.2f}/{limit:.2f})")


class OrderManager:
    """訂單管理器 (支援 Shioaji API)"""
    
    def __init__(
        self,
        broker_adapter,
        risk_manager,
        logger,
        config: dict = None
    ):
        """
        初始化訂單管理器
        
        Args:
            broker_adapter: 券商適配器 (ShioajiBrokerAdapter)
            risk_manager: 風險管理器
            logger: 訂單日誌
            config: 配置字典
        """
        self.broker = broker_adapter
        self.risk = risk_manager
        self.logger = logger
        self.config = config or {}
        
        # 訂單狀態追蹤
        self.pending_orders: Dict[str, any] = {}
        self.filled_orders: Dict[str, any] = {}
        self.cancelled_orders: Dict[str, any] = {}
        
        # 移動停損追蹤 (Trailing Stop)
        self.trailing_stops: Dict[str, dict] = {}
        
        # 選擇權停損追蹤 (Option Stop Loss)
        self.option_stops: Dict[str, dict] = {}  # trade_id -> {entry_price, highest_price, stop_pts, current_stop}
        
        # 配置
        self.require_confirmation = self.config.get('require_confirmation', False)
        self.max_order_retries = self.config.get('max_order_retries', 1)
        self.order_timeout_secs = self.config.get('order_timeout_secs', 30)
        self.trailing_stop_interval = self.config.get('trailing_stop_interval', 5)  # 每 5 點更新一次
        
        # 回調函數
        self.on_fill_callback: Optional[Callable] = None
        self.on_reject_callback: Optional[Callable] = None
        self.on_cancel_callback: Optional[Callable] = None
        
        # 狀態
        self.stop_new_orders = False  # 停止新訂單 (緊急停止用)
        
        console.print("[bold green]✅ OrderManager 已初始化 (Shioaji API)[/bold green]")
    
    def set_callbacks(
        self,
        on_fill: Callable = None,
        on_reject: Callable = None,
        on_cancel: Callable = None
    ):
        """設置回調函數"""
        self.on_fill_callback = on_fill
        self.on_reject_callback = on_reject
        self.on_cancel_callback = on_cancel
    
    def submit_entry(
        self,
        contract,
        quantity: int,
        signal_info: dict,
        price_type: str = 'LMT'
    ) -> Optional[any]:
        """
        提交進場訂單
        
        Args:
            contract: 合約物件 (Shioaji Contract)
            quantity: 數量
            signal_info: 訊號資訊 {'score', 'side', 'price_mtx'}
            price_type: 價格類型 'LMT' (限價), 'MKT' (市價), 'STP' (停損)
        
        Returns:
            Trade 物件，失敗返回 None
        
        Raises:
            RiskLimitError: 風險限制超限
            OrderError: 訂單錯誤
        """
        # 檢查是否停止新訂單
        if self.stop_new_orders:
            console.print("[yellow]⚠️  已停止新訂單[/yellow]")
            return None
        
        # 1. 風險檢查
        if not self.risk.check_order(contract, quantity):
            raise RiskLimitError(
                "order_check",
                quantity,
                self.risk.max_order_quantity
            )
        
        # 2. 高風險訂單確認
        if self.require_confirmation or quantity > 3:
            console.print(f"[yellow]⚠️  確認訂單：{quantity}口 {contract.code}[/yellow]")
            if not self._confirm_order():
                console.print("[yellow]訂單已取消[/yellow]")
                return None
        
        # 3. 生成訂單
        order = self.broker.build_option_order(
            action='Buy',
            price=self._calculate_entry_price(contract),
            quantity=quantity,
            option_right=contract.option_right,
            price_type=price_type
        )
        
        # 4. 記錄訂單
        if self.logger:
            self.logger.log_order('ENTRY', order, signal_info)
        
        # 5. 提交訂單
        try:
            trade = self.broker.place_order(contract, order)
            self.pending_orders[trade.id] = {
                'trade': trade,
                'submitted_at': datetime.datetime.now(),
                'retries': 0,
                'signal_info': signal_info,
                'entry_price': order.price
            }
            
            console.print(f"[green]✅ 訂單已提交：{trade.id}[/green]")
            return trade
            
        except Exception as e:
            console.print(f"[red]❌ 下單失敗：{e}[/red]")
            if self.logger:
                self.logger.log_error('SUBMIT_FAILED', str(e), {'contract': contract.code})
            raise OrderError(f"Failed to submit order: {e}")
    
    def submit_exit(
        self,
        contract,
        quantity: int,
        reason: str = 'EXIT',
        price_type: str = 'LMT'
    ) -> Optional[any]:
        """
        提交出場訂單
        
        Args:
            contract: 合約物件
            quantity: 數量
            reason: 出場原因 (TP/SL/TIME)
            price_type: 價格類型
        
        Returns:
            Trade 物件，失敗返回 None
        """
        # 出場不檢查風險限制 (必須執行)
        
        # 1. 生成訂單
        order = self.broker.build_option_order(
            action='Sell',
            price=self._calculate_exit_price(contract),
            quantity=quantity,
            option_right=contract.option_right,
            price_type=price_type
        )
        
        # 2. 記錄訂單
        if self.logger:
            self.logger.log_order('EXIT', order, {'reason': reason})
        
        # 3. 提交訂單
        try:
            trade = self.broker.place_order(contract, order)
            self.pending_orders[trade.id] = {
                'trade': trade,
                'submitted_at': datetime.datetime.now(),
                'retries': 0,
                'reason': reason
            }
            
            console.print(f"[green]✅ 出場訂單已提交：{trade.id} ({reason})[/green]")
            return trade
            
        except Exception as e:
            console.print(f"[red]❌ 出場訂單失敗：{e}[/red]")
            if self.logger:
                self.logger.log_error('EXIT_FAILED', str(e), {'contract': contract.code})
            raise OrderError(f"Failed to submit exit order: {e}")
    
    def submit_option_entry(
        self,
        contract,
        quantity: int,
        signal_info: dict,
        option_type: str = 'C'  # 'C' or 'P'
    ) -> Optional[any]:
        """
        提交選擇權買方進場訂單
        
        選擇權買方特點:
        - 最大損失 = 權利金
        - 不需要保證金
        - 適合動能突破策略
        
        Args:
            contract: 選擇權合約 (Shioaji Options Contract)
            quantity: 數量 (口)
            signal_info: 訊號資訊 {'score', 'side', 'price_mtx'}
            option_type: 選擇權類型 'C' (Call) or 'P' (Put)
        
        Returns:
            Trade 物件，失敗返回 None
        """
        console.print(f"[bold blue]📊 選擇權買方進場：{option_type} {contract.code}[/bold blue]")
        
        # 使用一般進場邏輯
        return self.submit_entry(
            contract=contract,
            quantity=quantity,
            signal_info=signal_info,
            price_type='LMT'  # 選擇權只用限價單
        )
    
    def setup_option_stop_loss(
        self,
        trade_id: str,
        entry_premium: float,
        stop_loss_pct: float = 0.30,
        take_profit_pct: float = 0.50
    ):
        """
        設置選擇權買方停損停利 (固定百分比)
        
        選擇權買方不適合移動停損 (因為時間價值衰減)
        建議使用固定百分比停損
        
        Args:
            trade_id: 訂單 ID
            entry_premium: 進場權利金
            stop_loss_pct: 停損百分比 (預設 30%)
            take_profit_pct: 停利百分比 (預設 50%)
        """
        self.option_stops[trade_id] = {
            'entry_premium': entry_premium,
            'stop_loss': entry_premium * (1 - stop_loss_pct),
            'take_profit': entry_premium * (1 + take_profit_pct),
            'stop_loss_pct': stop_loss_pct,
            'take_profit_pct': take_profit_pct
        }
        
        console.print(
            f"[dim]💰 選擇權停損設定：進場={entry_premium:.0f}, "
            f"停損={self.option_stops[trade_id]['stop_loss']:.0f} (-{stop_loss_pct*100:.0f}%), "
            f"停利={self.option_stops[trade_id]['take_profit']:.0f} (+{take_profit_pct*100:.0f}%)[/dim]"
        )
    
    def check_option_exit(self, trade_id: str, current_premium: float) -> Optional[str]:
        """
        檢查選擇權是否該出場
        
        Args:
            trade_id: 訂單 ID
            current_premium: 當前權利金價格
        
        Returns:
            'TP' (停利), 'SL' (停損), None (繼續持有)
        """
        if trade_id not in self.option_stops:
            return None
        
        stop_info = self.option_stops[trade_id]
        
        # 停利檢查
        if current_premium >= stop_info['take_profit']:
            console.print(f"[green]✅ 選擇權停利觸發：{current_premium:.0f} >= {stop_info['take_profit']:.0f}[/green]")
            return 'TP'
        
        # 停損檢查
        if current_premium <= stop_info['stop_loss']:
            console.print(f"[red]❌ 選擇權停損觸發：{current_premium:.0f} <= {stop_info['stop_loss']:.0f}[/red]")
            return 'SL'
        
        return None
    
    def setup_trailing_stop(
        self,
        trade_id: str,
        entry_price: float,
        stop_loss_pts: float = 60,
        take_profit_pts: float = 50
    ):
        """
        設置移動停損 (Trailing Stop)
        
        Shioaji API 實作:
        - 使用 STP (停損單) 類型
        - 每獲利 trailing_stop_interval 點更新一次停損價
        - 避免頻繁改單導致 API 限流
        
        Args:
            trade_id: 訂單 ID
            entry_price: 進場價格
            stop_loss_pts: 初始停損點數 (預設 60 點)
            take_profit_pts: 停利點數 (預設 50 點)
        """
        self.trailing_stops[trade_id] = {
            'entry_price': entry_price,
            'highest_price': entry_price,
            'stop_pts': stop_loss_pts,
            'take_profit_pts': take_profit_pts,
            'current_stop': entry_price - stop_loss_pts,
            'last_update_price': entry_price
        }
        
        console.print(f"[dim]📍 移動停損已設置：進場={entry_price}, 停損={stop_loss_pts}點[/dim]")
    
    def update_trailing_stop(self, trade_id: str, current_price: float) -> Optional[bool]:
        """
        更新移動停損 (由行情回調函數調用)
        
        Shioaji API: api.update_order(trade, price=new_stop_price, qty=1)
        
        Args:
            trade_id: 訂單 ID
            current_price: 當前價格
        
        Returns:
            True if updated, False otherwise
        """
        if trade_id not in self.trailing_stops:
            return False
        
        ts = self.trailing_stops[trade_id]
        
        # 1. 更新最高價
        if current_price > ts['highest_price']:
            ts['highest_price'] = current_price
            
            # 2. 計算新的停損價
            new_stop = ts['highest_price'] - ts['stop_pts']
            
            # 3. 只有當新的停損價移動超過 interval 點才更新 (避免 API 限流)
            if new_stop > ts['current_stop'] + self.trailing_stop_interval:
                ts['current_stop'] = new_stop
                ts['last_update_price'] = current_price
                
                # 4. 呼叫 Shioaji API 改單
                if trade_id in self.pending_orders:
                    trade = self.pending_orders[trade_id]['trade']
                    try:
                        # 使用 api.update_order 改價，而不是先撤後下
                        self.broker.api.update_order(trade, price=new_stop, qty=trade.quantity)
                        console.print(f"[dim]📍 停損上移至 {new_stop:.0f} (最高價 {ts['highest_price']:.0f})[/dim]")
                        return True
                    except Exception as e:
                        console.print(f"[red]❌ 改單失敗：{e}[/red]")
                        return False
        
        return False
    
    def cancel_order(self, trade_id: str) -> bool:
        """
        取消訂單
        
        Shioaji API: api.cancel_order(trade)
        
        Args:
            trade_id: 訂單 ID
        
        Returns:
            是否成功取消
        """
        if trade_id not in self.pending_orders:
            console.print(f"[yellow]⚠️  訂單不存在：{trade_id}[/yellow]")
            return False
        
        pending = self.pending_orders[trade_id]
        trade = pending['trade']
        
        try:
            result = self.broker.cancel_order(trade)
            
            if result:
                self.cancelled_orders[trade_id] = pending
                del self.pending_orders[trade_id]
                
                # 同時清除移動停損
                if trade_id in self.trailing_stops:
                    del self.trailing_stops[trade_id]
                
                console.print(f"[green]✅ 訂單已取消：{trade_id}[/green]")
                
                if self.logger:
                    self.logger.log_cancellation(trade_id)
                
                if self.on_cancel_callback:
                    self.on_cancel_callback(trade_id)
                
                return True
            else:
                console.print(f"[red]❌ 取消失敗：{trade_id}[/red]")
                return False
                
        except Exception as e:
            console.print(f"[red]❌ 取消訂單錯誤：{e}[/red]")
            return False
    
    def on_order_event(self, trade, status: str):
        """
        訂單事件回調 (由 Broker 調用)
        
        Shioaji API: trade.status.status
        - 'Filled': 成交
        - 'Rejected': 被拒絕
        - 'Cancelled': 已取消
        - 'PartiallyFilled': 部分成交
        
        Args:
            trade: Trade 物件
            status: 狀態
        """
        trade_id = getattr(trade, 'id', str(trade))
        
        if status == 'Filled':
            # 訂單成交
            if trade_id in self.pending_orders:
                pending = self.pending_orders[trade_id]
                self.filled_orders[trade_id] = {
                    **pending,
                    'filled_at': datetime.datetime.now(),
                    'fill_price': getattr(trade, 'price', 0),
                    'fill_quantity': getattr(trade, 'quantity', 0)
                }
                del self.pending_orders[trade_id]
                
                # 更新部位
                if self.risk:
                    self.risk.update_position(trade)
                
                # 記錄成交
                if self.logger:
                    self.logger.log_fill(trade)
                
                console.print(f"[green]✅ 訂單成交：{trade_id} @ {trade.price}[/green]")
                
                # 回調
                if self.on_fill_callback:
                    self.on_fill_callback(trade)
        
        elif status == 'Rejected':
            # 訂單被拒絕
            console.print(f"[red]❌ 訂單被拒絕：{trade_id}[/red]")
            
            if self.logger:
                self.logger.log_rejection(trade, getattr(trade, 'reject_reason', 'Unknown'))
            
            if self.on_reject_callback:
                self.on_reject_callback(trade_id)
        
        elif status == 'Cancelled':
            # 訂單被取消
            console.print(f"[yellow]⚠️  訂單被取消：{trade_id}[/yellow]")
            
            if trade_id in self.pending_orders:
                self.cancelled_orders[trade_id] = self.pending_orders[trade_id]
                del self.pending_orders[trade_id]
            
            if self.logger:
                self.logger.log_cancellation(trade_id)
        
        elif status == 'Timeout':
            # 訂單超時
            console.print(f"[yellow]⚠️  訂單超時：{trade_id}[/yellow]")
            
            if self.logger:
                self.logger.log_timeout(trade_id, self.order_timeout_secs)
    
    def check_order_timeout(self) -> list:
        """檢查超時訂單"""
        timeout_ids = []
        now = datetime.datetime.now()
        
        for trade_id, pending in list(self.pending_orders.items()):
            submitted_at = pending.get('submitted_at')
            if submitted_at:
                elapsed = (now - submitted_at).total_seconds()
                if elapsed > self.order_timeout_secs:
                    timeout_ids.append(trade_id)
                    self.on_order_event(pending['trade'], 'Timeout')
        
        return timeout_ids
    
    def get_position_summary(self) -> dict:
        """獲取部位摘要"""
        return {
            'pending_count': len(self.pending_orders),
            'filled_count': len(self.filled_orders),
            'cancelled_count': len(self.cancelled_orders),
            'current_position': self.risk.current_position if self.risk else 0,
            'daily_pnl': self.risk.daily_pnl if self.risk else 0.0,
            'trailing_stops_active': len(self.trailing_stops)
        }
    
    def _calculate_entry_price(self, contract) -> float:
        """計算進場價格 (Shioaji: Ask + aggressive_ticks)"""
        ask_price = getattr(contract, 'ask_price', 0) or 0.0
        aggressive_ticks = self.broker.aggressive_ticks if self.broker else 2
        tick_size = self.broker.tick_size if self.broker else 1.0
        return max(0.0, ask_price + (aggressive_ticks * tick_size))
    
    def _calculate_exit_price(self, contract) -> float:
        """計算出場價格 (Shioaji: Bid - aggressive_ticks)"""
        bid_price = getattr(contract, 'bid_price', 0) or 0.0
        aggressive_ticks = self.broker.aggressive_ticks if self.broker else 2
        tick_size = self.broker.tick_size if self.broker else 1.0
        return max(tick_size, bid_price - (aggressive_ticks * tick_size))
    
    def _confirm_order(self) -> bool:
        """訂單確認 (簡化版本)"""
        # TODO: 整合 Line/Email 通知確認
        return True  # 預設確認
    
    def reset_daily(self):
        """重置每日統計"""
        self.pending_orders.clear()
        self.filled_orders.clear()
        self.cancelled_orders.clear()
        self.trailing_stops.clear()
        self.option_stops.clear()
        
        if self.risk:
            self.risk.reset_daily()
        
        console.print("[bold green]✅ 每日統計已重置[/bold green]")
