#!/usr/bin/env python3
"""
信號生成器 (Signal Generator)
負責開盤買進、進場信號、出場信號

信號類型：
- 開盤信號 (Open Signal)
- Squeeze 信號
- 回測信號 (Pullback Signal)
- MTF 對齊信號
"""

import numpy as np
import pandas as pd
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass
from datetime import datetime, time
from rich.console import Console

console = Console()


@dataclass
class Signal:
    """交易信號"""
    symbol: str
    direction: int  # 1=buy, -1=sell, 0=exit
    signal_type: str  # 'OPEN', 'SQUEEZE', 'PULLBACK', 'MTF'
    price: float
    timestamp: datetime
    score: float
    confidence: float  # 0-1
    reason: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@dataclass
class SignalConfig:
    """信號配置"""
    entry_score_threshold: float = 30
    mom_state_long: int = 2
    mom_state_short: int = 1
    use_open_signal: bool = True
    use_squeeze: bool = True
    use_pullback: bool = True
    use_mtf: bool = True
    pb_confirmation_bars: int = 12
    open_signal_time: time = time(8, 45)  # 開盤時間


class SignalGenerator:
    """
    信號生成器
    
    核心功能：
    1. 開盤信號 (Open Signal)
    2. Squeeze 信號
    3. 回測信號 (Pullback)
    4. MTF 對齊信號
    5. 信號過濾與確認
    """
    
    def __init__(self, config: SignalConfig):
        """
        Args:
            config: 信號配置
        """
        self.config = config
        self.signal_history: List[Signal] = []
        self.last_signal: Optional[Signal] = None
        
        console.print("[green]✓ Signal Generator initialized[/green]")
    
    def generate_open_signal(
        self,
        symbol: str,
        df_5m: pd.DataFrame,
        df_15m: pd.DataFrame,
    ) -> Optional[Signal]:
        """
        生成開盤信號
        
        邏輯：
        - 開盤後 5 分鐘內
        - 價格 > 開盤價 (多頭) 或 < 開盤價 (空頭)
        - 配合 MTF 對齊
        
        Args:
            symbol: 商品代號
            df_5m: 5 分鐘 K 棒
            df_15m: 15 分鐘 K 棒
        
        Returns:
            Signal 或 None
        """
        if not self.config.use_open_signal:
            return None
        
        if df_5m.empty or df_15m.empty:
            return None
        
        current_time = df_5m.index[-1].time()
        
        # 檢查是否在開盤時間附近 (8:45-9:00)
        if not (time(8, 45) <= current_time <= time(9, 0)):
            return None
        
        # 取得開盤價
        day_open = df_5m.iloc[0]['Open'] if len(df_5m) > 0 else None
        if day_open is None:
            return None
        
        current_price = df_5m.iloc[-1]['Close']
        
        # 多頭信號：價格 > 開盤價
        if current_price > day_open:
            direction = 1
            reason = f"Price ({current_price:.0f}) > Open ({day_open:.0f})"
            confidence = min((current_price - day_open) / day_open * 1000, 1.0)
        
        # 空頭信號：價格 < 開盤價
        elif current_price < day_open:
            direction = -1
            reason = f"Price ({current_price:.0f}) < Open ({day_open:.0f})"
            confidence = min((day_open - current_price) / day_open * 1000, 1.0)
        
        else:
            return None
        
        # 計算 MTF 對齊
        from squeeze_futures.engine.indicators import calculate_mtf_alignment
        
        mtf_data = {'5m': df_5m, '15m': df_15m}
        alignment = calculate_mtf_alignment(mtf_data)
        score = alignment.get('score', 0)
        
        # 調整方向基於 MTF
        if direction > 0 and score < 0:
            direction = 0  # 取消多頭信號
        elif direction < 0 and score > 0:
            direction = 0  # 取消空頭信號
        
        if direction == 0:
            return None
        
        signal = Signal(
            symbol=symbol,
            direction=direction,
            signal_type='OPEN',
            price=current_price,
            timestamp=df_5m.index[-1],
            score=score,
            confidence=confidence,
            reason=reason,
        )
        
        self._record_signal(signal)
        console.print(f"[bold {'green' if direction > 0 else 'red'}]"
                     f"📊 OPEN SIGNAL: {symbol} {'BUY' if direction > 0 else 'SELL'} "
                     f"@ {current_price:.0f} (confidence: {confidence:.0%})[/bold {'green' if direction > 0 else 'red'}]")
        
        return signal
    
    def generate_squeeze_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
    ) -> Optional[Signal]:
        """
        生成 Squeeze 信號
        
        邏輯：
        - Squeeze 釋放 (sqz_on = False)
        - MTF 分數超過門檻
        - 動能狀態符合條件
        
        Args:
            symbol: 商品代號
            df: 已計算指標的 DataFrame
        
        Returns:
            Signal 或 None
        """
        if not self.config.use_squeeze:
            return None
        
        if df.empty:
            return None
        
        row = df.iloc[-1]
        
        # 檢查 Squeeze 釋放
        if row.get('sqz_on', True):
            return None
        
        # 檢查 MTF 分數
        score = row.get('score', 0)
        mom_state = row.get('mom_state', 0)
        
        # 多頭信號
        if score >= self.config.entry_score_threshold and mom_state >= self.config.mom_state_long:
            signal = Signal(
                symbol=symbol,
                direction=1,
                signal_type='SQUEEZE',
                price=row['Close'],
                timestamp=df.index[-1],
                score=score,
                confidence=min(score / 100, 1.0),
                reason=f"Squeeze release, score={score:.1f}, mom_state={mom_state}",
            )
            self._record_signal(signal)
            console.print(f"[bold green]🔥 SQUEEZE BUY: {symbol} @ {row['Close']:.0f} (score: {score:.1f})[/bold green]")
            return signal
        
        # 空頭信號
        elif score <= -self.config.entry_score_threshold and mom_state <= self.config.mom_state_short:
            signal = Signal(
                symbol=symbol,
                direction=-1,
                signal_type='SQUEEZE',
                price=row['Close'],
                timestamp=df.index[-1],
                score=score,
                confidence=min(abs(score) / 100, 1.0),
                reason=f"Squeeze release, score={score:.1f}, mom_state={mom_state}",
            )
            self._record_signal(signal)
            console.print(f"[bold red]🔥 SQUEEZE SELL: {symbol} @ {row['Close']:.0f} (score: {score:.1f})[/bold red]")
            return signal
        
        return None
    
    def generate_pullback_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
    ) -> Optional[Signal]:
        """
        生成回測信號
        
        邏輯：
        - 創 N 根 K 棒新高/新低
        - 價格回測到 EMA 區間
        - 在回測區間內進場
        
        Args:
            symbol: 商品代號
            df: 已計算指標的 DataFrame
        
        Returns:
            Signal 或 None
        """
        if not self.config.use_pullback:
            return None
        
        if df.empty:
            return None
        
        row = df.iloc[-1]
        n = self.config.pb_confirmation_bars
        
        # 多頭回測
        if row.get('in_bull_pb_zone', False):
            # 檢查最近 N 根是否有新高
            if len(df) >= n:
                recent_highs = df['is_new_high'].iloc[-n:-1]
                if recent_highs.any():
                    signal = Signal(
                        symbol=symbol,
                        direction=1,
                        signal_type='PULLBACK',
                        price=row['Close'],
                        timestamp=df.index[-1],
                        score=row.get('score', 0),
                        confidence=0.7,
                        reason=f"Bull pullback zone, recent high confirmed",
                    )
                    self._record_signal(signal)
                    console.print(f"[bold green]📉 PULLBACK BUY: {symbol} @ {row['Close']:.0f}[/bold green]")
                    return signal
        
        # 空頭回測
        if row.get('in_bear_pb_zone', False):
            # 檢查最近 N 根是否有新低
            if len(df) >= n:
                recent_lows = df['is_new_low'].iloc[-n:-1]
                if recent_lows.any():
                    signal = Signal(
                        symbol=symbol,
                        direction=-1,
                        signal_type='PULLBACK',
                        price=row['Close'],
                        timestamp=df.index[-1],
                        score=row.get('score', 0),
                        confidence=0.7,
                        reason=f"Bear pullback zone, recent low confirmed",
                    )
                    self._record_signal(signal)
                    console.print(f"[bold red]📈 PULLBACK SELL: {symbol} @ {row['Close']:.0f}[/bold red]")
                    return signal
        
        return None
    
    def generate_exit_signal(
        self,
        symbol: str,
        position_direction: int,
        current_price: float,
        stop_loss_price: Optional[float],
        take_profit_price: Optional[float],
    ) -> Optional[Signal]:
        """
        生成出場信號
        
        Args:
            symbol: 商品代號
            position_direction: 持倉方向
            current_price: 當前價格
            stop_loss_price: 停損價
            take_profit_price: 停利價
        
        Returns:
            Signal 或 None
        """
        reason = None
        
        # 停損檢查
        if position_direction > 0 and stop_loss_price and current_price <= stop_loss_price:
            reason = f"Stop loss @ {stop_loss_price:.0f}"
        elif position_direction < 0 and stop_loss_price and current_price >= stop_loss_price:
            reason = f"Stop loss @ {stop_loss_price:.0f}"
        
        # 停利檢查
        elif take_profit_price:
            if position_direction > 0 and current_price >= take_profit_price:
                reason = f"Take profit @ {take_profit_price:.0f}"
            elif position_direction < 0 and current_price <= take_profit_price:
                reason = f"Take profit @ {take_profit_price:.0f}"
        
        if reason:
            signal = Signal(
                symbol=symbol,
                direction=0,  # 平倉
                signal_type='EXIT',
                price=current_price,
                timestamp=datetime.now(),
                score=0,
                confidence=1.0,
                reason=reason,
            )
            self._record_signal(signal)
            console.print(f"[bold yellow]⚠ EXIT SIGNAL: {symbol} @ {current_price:.0f} ({reason})[/bold yellow]")
            return signal
        
        return None
    
    def _record_signal(self, signal: Signal):
        """記錄信號"""
        self.signal_history.append(signal)
        self.last_signal = signal
    
    def get_signal_summary(self) -> pd.DataFrame:
        """獲取信號摘要"""
        if not self.signal_history:
            return pd.DataFrame()
        
        data = []
        for sig in self.signal_history[-50:]:  # 最近 50 筆
            data.append({
                'Time': sig.timestamp,
                'Symbol': sig.symbol,
                'Type': sig.signal_type,
                'Direction': 'BUY' if sig.direction > 0 else 'SELL' if sig.direction < 0 else 'EXIT',
                'Price': sig.price,
                'Score': sig.score,
                'Confidence': f"{sig.confidence:.0%}",
                'Reason': sig.reason,
            })
        
        return pd.DataFrame(data)
    
    def print_signal_report(self):
        """打印信號報告"""
        console.print("\n[bold blue]=== Signal Generator Report ===[/bold blue]\n")
        
        if self.signal_history:
            df = self.get_signal_summary()
            console.print(df.tail(10).to_string(index=False))
            
            # 統計
            total_signals = len(self.signal_history)
            buy_signals = len([s for s in self.signal_history if s.direction > 0])
            sell_signals = len([s for s in self.signal_history if s.direction < 0])
            avg_confidence = np.mean([s.confidence for s in self.signal_history])
            
            console.print(f"\nTotal Signals: {total_signals}")
            console.print(f"Buy Signals: {buy_signals}")
            console.print(f"Sell Signals: {sell_signals}")
            console.print(f"Avg Confidence: {avg_confidence:.0%}")
        else:
            console.print("[dim]No signals generated yet[/dim]")
