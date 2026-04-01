import pandas as pd
from datetime import datetime
import os
from typing import Optional, List, Dict, Tuple


def calculate_ma_stop_price(
    df: pd.DataFrame,
    position: int,
    ma_type: str = "below",
    ma_length: int = 60,
    ma_ticks: int = 5,
    ma_multiplier: float = 1.0,
    use_prev_ma: bool = True,
    entry_price: float = None  # 進場價（用於確保停損在正確方向）
) -> Optional[float]:
    """
    計算 MA 動態停損價
    
    Args:
        df: 包含收盤價的 DataFrame
        position: 部位方向（>0 多單，<0 空單）
        ma_type: "below"=MA 下方/上方固定 tick，"cross"=跌破/突破 MA
        ma_length: MA 週期
        ma_ticks: MA 下方/上方幾個 tick
        ma_multiplier: MA 停損倍數（0=停用）
        use_prev_ma: 是否使用前一 bar 的 MA（避免未來函數）
        entry_price: 進場價（可選，用於確保停損在正確方向）
    
    Returns:
        停損價格，若停用則返回 None
    """
    if ma_multiplier <= 0 or len(df) < ma_length:
        return None
    
    # 計算 MA - 使用前一 bar 避免未來函數
    if use_prev_ma and len(df) > ma_length:
        ma = df['Close'].rolling(window=ma_length).mean().iloc[-2]
    else:
        ma = df['Close'].rolling(window=ma_length).mean().iloc[-1]
    
    if pd.isna(ma):
        return None
    
    if ma_type == "below":
        # MA 下方/上方固定 tick 數
        offset = ma_ticks * ma_multiplier
        if position > 0:  # 多單：MA 下方
            stop_price = ma - offset
            # 確保停損價低於進場價（如果有提供）
            if entry_price is not None:
                stop_price = min(stop_price, entry_price - 1)
        else:  # 空單：MA 上方
            stop_price = ma + offset
            # 確保停損價高於進場價（如果有提供）
            if entry_price is not None:
                stop_price = max(stop_price, entry_price + 1)
        return stop_price
    else:  # "cross"
        # 直接跌破/突破 MA
        return ma


class PaperTrader:
    """
    模擬交易器，支援 SQLite 持久化
    
    功能:
    - 記憶體交易記錄 (trades list)
    - SQLite 持久化 (可選)
    - 權益曲線快照 (可選)
    """
    
    def __init__(
        self,
        ticker="TMF",
        initial_balance=100000,
        point_value=10,
        fee_per_side=20,
        exchange_fee_per_side=0,
        tax_rate=0.0,
        db_path: Optional[str] = None,
        snapshot_interval: int = 1800,  # 30 分鐘
    ):
        self.ticker = ticker
        self.balance = initial_balance
        self.position = 0
        self.entry_price = 0
        self.entry_time = None
        self.trades = []
        self.point_value = point_value
        self.fee_per_side = fee_per_side
        self.exchange_fee_per_side = exchange_fee_per_side
        self.tax_rate = tax_rate
        self.current_stop_loss = None
        self.be_triggered = False
        self.be_points = None
        
        # SQLite 持久化
        self.db = None
        self.snapshot_interval = snapshot_interval
        self._last_snapshot_time = None
        self._entry_score = None  # 記錄進場時的 MTF score
        
        if db_path:
            self._init_persistence(db_path)
    
    def _init_persistence(self, db_path: str):
        """初始化 SQLite 持久化"""
        from squeeze_futures.database.db_manager import DatabaseManager
        self.db = DatabaseManager(db_path)
        self.db.log_system_event(
            level='INFO',
            module='PaperTrader',
            message=f'Trader initialized with db={db_path}',
            details=f'ticker={self.ticker}, initial_balance={self.balance}'
        )
    
    def _record_trade_to_db(self, trade: Dict):
        """將交易記錄寫入資料庫"""
        if self.db is None:
            return
        
        try:
            # ENTRY 記錄
            if trade['type'] == 'ENTRY':
                self.db.record_trade({
                    'ticker': trade['ticker'],
                    'direction': trade['direction'],
                    'type': 'ENTRY',
                    'entry_time': trade['entry_time'],
                    'entry_price': trade['entry_price'],
                    'lots': trade['lots'],
                    'pnl_cash': 0,  # ENTRY 時 PnL 為 0
                    'entry_score': self._entry_score,
                })
            # EXIT / PARTIAL_EXIT 記錄
            else:
                self.db.record_trade({
                    'ticker': trade['ticker'],
                    'direction': trade['direction'],
                    'type': trade['type'],
                    'entry_time': trade['entry_time'],
                    'exit_time': trade['exit_time'],
                    'entry_price': trade['entry_price'],
                    'exit_price': trade['exit_price'],
                    'lots': trade['lots'],
                    'pnl_points': trade['pnl_points'],
                    'gross_pnl_cash': trade['gross_pnl_cash'],
                    'broker_fee': trade['broker_fee'],
                    'exchange_fee': trade['exchange_fee'],
                    'tax_cost': trade['tax_cost'],
                    'total_cost': trade['total_cost'],
                    'pnl_cash': trade['pnl_cash'],
                    'exit_reason': trade.get('exit_reason'),
                })
        except Exception as e:
            if self.db:
                self.db.log_system_event(
                    level='ERROR',
                    module='PaperTrader',
                    message=f'Failed to record trade: {str(e)}',
                    details=str(trade)
                )
    
    def _maybe_save_snapshot(self, current_time: datetime, price: float):
        """定期儲存權益快照"""
        if self.db is None:
            return
        
        # 檢查是否達到快照間隔
        if self._last_snapshot_time is None:
            self._last_snapshot_time = current_time
            return
        
        elapsed = (current_time - self._last_snapshot_time).total_seconds()
        if elapsed >= self.snapshot_interval:
            unrealized_pnl = 0
            if self.position != 0:
                unrealized_pnl = (price - self.entry_price) * self.position * self.point_value
            
            total_equity = self.balance + unrealized_pnl
            
            self.db.save_equity_snapshot(
                timestamp=current_time,
                balance=self.balance,
                position=self.position,
                unrealized_pnl=unrealized_pnl,
                total_equity=total_equity,
                market_price=price
            )
            self._last_snapshot_time = current_time
    
    def get_db_trade_history(self) -> pd.DataFrame:
        """從資料庫取得交易歷史"""
        if self.db is None:
            return pd.DataFrame()
        
        trades = self.db.get_trade_history()
        if not trades:
            return pd.DataFrame()
        
        return pd.DataFrame(trades)
    
    def get_db_performance_summary(self, start_date: str = None, end_date: str = None) -> Dict:
        """從資料庫取得績效摘要"""
        if self.db is None:
            return {}
        return self.db.get_performance_summary(start_date, end_date)

    def execute_signal(self, signal: str, price: float, timestamp: datetime, lots=1, max_lots=1, stop_loss=None, break_even_trigger=None, exit_reason: str = None):
        if signal == "BUY":
            if self.position < max_lots:
                if self.position < 0: self.execute_signal("EXIT", price, timestamp)
                if self.position == 0:
                    self.entry_price, self.entry_time, self.be_triggered = price, timestamp, False
                    self.current_stop_loss = price - stop_loss if stop_loss else None
                    self.be_points = break_even_trigger
                else:
                    self.entry_price = ((self.entry_price * self.position) + (price * lots)) / (self.position + lots)
                self.position += lots
                
                # 記錄 ENTRY 到資料庫
                if self.db:
                    trade_record = {
                        'ticker': self.ticker, 'direction': 'LONG', 'type': 'ENTRY',
                        'entry_time': timestamp, 'entry_price': price, 'lots': lots,
                    }
                    self._record_trade_to_db(trade_record)
                
                return f"Entry LONG {lots} at {price}"

        elif signal == "SELL":
            if abs(self.position) < max_lots:
                if self.position > 0: self.execute_signal("EXIT", price, timestamp)
                if self.position == 0:
                    self.entry_price, self.entry_time, self.be_triggered = price, timestamp, False
                    self.current_stop_loss = price + stop_loss if stop_loss else None
                    self.be_points = break_even_trigger
                else:
                    self.entry_price = ((self.entry_price * abs(self.position)) + (price * lots)) / (abs(self.position) + lots)
                self.position -= lots
                
                # 記錄 ENTRY 到資料庫
                if self.db:
                    trade_record = {
                        'ticker': self.ticker, 'direction': 'SHORT', 'type': 'ENTRY',
                        'entry_time': timestamp, 'entry_price': price, 'lots': lots,
                    }
                    self._record_trade_to_db(trade_record)
                
                return f"Entry SHORT {lots} at {price}"

        elif (signal == "EXIT" or signal == "PARTIAL_EXIT") and self.position != 0:
            lots_to_exit = lots if signal == "PARTIAL_EXIT" else abs(self.position)
            lots_to_exit = min(lots_to_exit, abs(self.position))

            pnl_pts = (price - self.entry_price) * (1 if self.position > 0 else -1)
            broker_fee = self.fee_per_side * 2 * lots_to_exit
            exchange_fee = self.exchange_fee_per_side * 2 * lots_to_exit
            tax_cost = ((self.entry_price + price) * self.point_value * self.tax_rate) * lots_to_exit
            total_cost = broker_fee + exchange_fee + tax_cost
            pnl_cash = (pnl_pts * self.point_value * lots_to_exit) - total_cost

            direction = "LONG" if self.position > 0 else "SHORT"
            trade_record = {
                "ticker": self.ticker, "entry_time": self.entry_time, "exit_time": timestamp,
                "direction": direction, "entry_price": self.entry_price, "exit_price": price,
                "lots": lots_to_exit, "pnl_points": pnl_pts, "gross_pnl_cash": pnl_pts * self.point_value * lots_to_exit,
                "broker_fee": broker_fee, "exchange_fee": exchange_fee, "tax_cost": tax_cost,
                "total_cost": total_cost, "pnl_cash": pnl_cash, "type": signal,
                "exit_reason": exit_reason,
            }
            self.trades.append(trade_record)
            self.balance += pnl_cash
            
            # 記錄 EXIT 到資料庫
            if self.db:
                self._record_trade_to_db(trade_record)

            if signal == "EXIT" or lots_to_exit == abs(self.position):
                self.position, self.entry_price, self.current_stop_loss = 0, 0, None
            else:
                self.position = (abs(self.position) - lots_to_exit) * (1 if self.position > 0 else -1)
            return f"{signal} {lots_to_exit} at {price}, PnL: {pnl_cash:.0f}"

        return None

    def update_trailing_stop(self, current_price: float):
        if self.position == 0 or not self.be_points or self.be_triggered: return False
        pnl = (current_price - self.entry_price) * (1 if self.position > 0 else -1)
        if pnl >= self.be_points:
            self.current_stop_loss = self.entry_price + (2 * (1 if self.position > 0 else -1))
            self.be_triggered = True; return True
        return False

    def check_stop_loss(self, price: float, timestamp: datetime):
        if self.position > 0 and self.current_stop_loss and price <= self.current_stop_loss:
            return self.execute_signal("EXIT", self.current_stop_loss, timestamp)
        if self.position < 0 and self.current_stop_loss and price >= self.current_stop_loss:
            return self.execute_signal("EXIT", self.current_stop_loss, timestamp)
        return None

    def get_performance_report(self):
        if not self.trades: return "No trades."
        df = pd.DataFrame(self.trades)
        return (
            f"# 📊 Report\n"
            f"- **PnL**: {df['pnl_cash'].sum():+,.0f} TWD\n"
            f"- **WinRate**: {(df['pnl_cash']>0).mean()*100:.1f}%\n"
            f"- **Total Cost**: {df['total_cost'].sum():,.0f} TWD\n\n"
            f"{df.to_markdown()}"
        )

    def save_report(self):
        os.makedirs("exports/simulations", exist_ok=True)
        path = f"exports/simulations/report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        with open(path, "w") as f: f.write(self.get_performance_report())
        return path
