#!/usr/bin/env python3
"""
TSM (台積電 ADR) 數據下載與分析
用於夜盤交易信號確認

TSM 與台指期相關性：
- TSM 佔台指期權重約 30%
- TSM 夜盤交易時間：21:30-04:00 (台北時間)
- 高度相關 (>0.8)
"""

import sys
import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from rich.console import Console

console = Console()


def download_tsm_data(period: str = "5d", interval: str = "5m") -> pd.DataFrame:
    """
    下載 TSM (台積電 ADR) 數據
    
    數據源：
    - 主要：Yahoo Finance (免費，即時 15 分鐘延遲)
    - 備援：Shioaji API (目前不支援美股)
    
    Args:
        period: 期間 (1d, 5d, 1mo, 3mo, 6mo, 1y)
        interval: 週期 (1m, 5m, 15m, 30m, 1h)
    
    Returns:
        DataFrame with OHLCV data
    """
    console.print(f"[dim]從 Yahoo Finance 下載 TSM ({interval}, {period})...[/dim]")
    
    try:
        import yfinance as yf
        
        ticker = yf.Ticker("TSM")
        df = ticker.history(period=period, interval=interval)
        
        if df.empty:
            console.print("[yellow]⚠ 未獲取到 TSM 數據[/yellow]")
            return None
        
        # 標準化欄位
        df = df.rename(columns={
            'Open': 'Open',
            'High': 'High',
            'Low': 'Low',
            'Close': 'Close',
            'Volume': 'Volume',
        })
        
        df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
        
        console.print(f"[green]✓ 載入 {len(df)} 筆 TSM K 棒 (Yahoo Finance)[/green]")
        console.print("[dim]注意：Yahoo Finance 數據有 15 分鐘延遲[/dim]")
        return df
        
    except ImportError:
        console.print("[red]✗ yfinance 未安裝[/red]")
        console.print("[dim]執行：pip install yfinance[/dim]")
        return None
    except Exception as e:
        console.print(f"[red]✗ 下載錯誤：{e}[/red]")
        return None


def calculate_tsm_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    計算 TSM 技術指標
    
    Args:
        df: TSM OHLCV 數據
    
    Returns:
        DataFrame with indicators
    """
    if df is None or df.empty:
        return None
    
    result = df.copy()
    
    # EMA
    result['ema_20'] = result['Close'].rolling(window=20).mean()
    result['ema_60'] = result['Close'].rolling(window=60).mean()
    
    # RSI
    delta = result['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    result['rsi'] = 100 - (100 / (1 + rs))
    
    # MACD
    exp1 = result['Close'].ewm(span=12, adjust=False).mean()
    exp2 = result['Close'].ewm(span=26, adjust=False).mean()
    result['macd'] = exp1 - exp2
    result['macd_signal'] = result['macd'].ewm(span=9, adjust=False).mean()
    
    # 趨勢判斷
    result['trend'] = 0
    result.loc[result['Close'] > result['ema_20'], 'trend'] = 1   # 多頭
    result.loc[result['Close'] < result['ema_20'], 'trend'] = -1  # 空頭
    
    # 動能判斷
    result['momentum'] = 0
    result.loc[result['rsi'] > 50, 'momentum'] = 1   # 多頭動能
    result.loc[result['rsi'] < 50, 'momentum'] = -1  # 空頭動能
    
    return result


def get_tsm_signal(df: pd.DataFrame) -> dict:
    """
    獲取 TSM 交易信號
    
    Args:
        df: TSM 數據 (含指標)
    
    Returns:
        信號字典
    """
    if df is None or df.empty:
        return {'signal': 'NEUTRAL', 'confidence': 0}
    
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest
    
    signal_score = 0
    reasons = []
    
    # 1. 趨勢判斷 (EMA)
    if latest['trend'] == 1:
        signal_score += 1
        reasons.append("Price > EMA20 (多頭)")
    elif latest['trend'] == -1:
        signal_score -= 1
        reasons.append("Price < EMA20 (空頭)")
    
    # 2. EMA 交叉
    if latest['ema_20'] > latest['ema_60'] and prev['ema_20'] <= prev['ema_60']:
        signal_score += 2
        reasons.append("EMA 黃金交叉")
    elif latest['ema_20'] < latest['ema_60'] and prev['ema_20'] >= prev['ema_60']:
        signal_score -= 2
        reasons.append("EMA 死亡交叉")
    
    # 3. RSI 判斷
    if latest['rsi'] > 70:
        reasons.append(f"RSI 超買 ({latest['rsi']:.1f})")
    elif latest['rsi'] < 30:
        reasons.append(f"RSI 超賣 ({latest['rsi']:.1f})")
    elif latest['rsi'] > 50:
        signal_score += 0.5
        reasons.append(f"RSI 多頭 ({latest['rsi']:.1f})")
    else:
        signal_score -= 0.5
        reasons.append(f"RSI 空頭 ({latest['rsi']:.1f})")
    
    # 4. MACD 判斷
    if latest['macd'] > latest['macd_signal']:
        signal_score += 1
        reasons.append("MACD 多頭")
    else:
        signal_score -= 1
        reasons.append("MACD 空頭")
    
    # 5. 價格動能
    price_change = (latest['Close'] - prev['Close']) / prev['Close'] * 100
    if price_change > 1:
        signal_score += 1
        reasons.append(f"價格上漲 +{price_change:.2f}%")
    elif price_change < -1:
        signal_score -= 1
        reasons.append(f"價格下跌 {price_change:.2f}%")
    
    # 綜合判斷
    if signal_score >= 3:
        signal = "STRONG_BUY"
        confidence = min(signal_score / 5, 1.0)
    elif signal_score >= 1:
        signal = "BUY"
        confidence = min(signal_score / 5, 0.8)
    elif signal_score <= -3:
        signal = "STRONG_SELL"
        confidence = min(abs(signal_score) / 5, 1.0)
    elif signal_score <= -1:
        signal = "SELL"
        confidence = min(abs(signal_score) / 5, 0.8)
    else:
        signal = "NEUTRAL"
        confidence = 0.5
    
    return {
        'signal': signal,
        'confidence': confidence,
        'score': signal_score,
        'reasons': reasons,
        'price': latest['Close'],
        'change': price_change,
        'rsi': latest['rsi'],
        'trend': latest['trend'],
    }


def analyze_tsm_correlation(tsm_df: pd.DataFrame, twii_df: pd.DataFrame) -> dict:
    """
    分析 TSM 與台指期的相關性
    
    Args:
        tsm_df: TSM 數據
        twii_df: 台指期數據
    
    Returns:
        相關性分析結果
    """
    if tsm_df is None or twii_df is None:
        return {'correlation': 0}
    
    # 對齊時間索引
    tsm_close = tsm_df['Close'].resample('1h').last()
    twii_close = twii_df['Close'].resample('1h').last()
    
    # 計算相關性
    correlation = tsm_close.corr(twii_close)
    
    return {
        'correlation': correlation if not pd.isna(correlation) else 0,
        'tsm_bars': len(tsm_df),
        'twii_bars': len(twii_df),
    }


def print_tsm_report(signal_data: dict, correlation_data: dict = None):
    """
    打印 TSM 分析報告
    
    Args:
        signal_data: TSM 信號數據
        correlation_data: 相關性數據
    """
    console.print("\n[bold blue]╔" + "═" * 60 + "╗[/bold blue]")
    console.print("[bold blue]║[/bold blue]" + " " * 20 + "TSM ANALYSIS REPORT" + " " * 19 + "[bold blue]║[/bold blue]")
    console.print("[bold blue]╚" + "═" * 60 + "╝[/bold blue]\n")
    
    # 信號摘要
    signal_color = {
        'STRONG_BUY': 'bold green',
        'BUY': 'green',
        'NEUTRAL': 'yellow',
        'SELL': 'red',
        'STRONG_SELL': 'bold red',
    }
    
    signal_style = signal_color.get(signal_data['signal'], 'white')
    
    console.print(Panel(
        f"[bold {signal_style}]{signal_data['signal']}[/bold {signal_style}]\n"
        f"Confidence: {signal_data['confidence']:.0%}\n"
        f"Score: {signal_data['score']:+.1f}",
        title="📊 TSM Signal",
        border_style=signal_style,
    ))
    
    # 價格資訊
    console.print(f"\n[bold]Price:[/bold] ${signal_data['price']:.2f}")
    console.print(f"[bold]Change:[/bold] {signal_data['change']:+.2f}%")
    console.print(f"[bold]RSI:[/bold] {signal_data['rsi']:.1f}")
    console.print(f"[bold]Trend:[/bold] {'📈 Bullish' if signal_data['trend'] == 1 else '📉 Bearish' if signal_data['trend'] == -1 else '➡️ Neutral'}")
    
    # 信號原因
    console.print(f"\n[bold]Signal Reasons:[/bold]")
    for reason in signal_data['reasons']:
        console.print(f"  • {reason}")
    
    # 相關性
    if correlation_data:
        console.print(f"\n[bold]TSM-TWII Correlation:[/bold] {correlation_data['correlation']:.2f}")
    
    console.print()


def main():
    """主函數"""
    console.print("[bold blue]╔" + "═" * 60 + "╗[/bold blue]")
    console.print("[bold blue]║[/bold blue]" + " " * 18 + "TSM DATA DOWNLOADER" + " " * 21 + "[bold blue]║[/bold blue]")
    console.print("[bold blue]╚" + "═" * 60 + "╝[/bold blue]\n")
    
    # 下載 TSM 數據
    tsm_df = download_tsm_data(period="5d", interval="5m")
    
    if tsm_df is None:
        console.print("[red]✗ 無法載入 TSM 數據[/red]")
        return
    
    # 計算指標
    console.print("\n[dim]計算技術指標...[/dim]")
    tsm_df = calculate_tsm_indicators(tsm_df)
    
    # 獲取信號
    console.print("[dim]分析交易信號...[/dim]")
    signal_data = get_tsm_signal(tsm_df)
    
    # 打印報告
    print_tsm_report(signal_data)
    
    # 保存數據
    output_dir = Path("data/tsm")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"TSM_5m_{timestamp}.csv"
    tsm_df.to_csv(output_file)
    
    console.print(f"[green]✓ 數據已保存至：{output_file}[/green]\n")
    
    return tsm_df, signal_data


if __name__ == "__main__":
    main()
