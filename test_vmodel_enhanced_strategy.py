#!/usr/bin/env python3
"""
V模型測試：使用實際數據回測增強版均值回歸策略
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import warnings
warnings.filterwarnings('ignore')

# 導入策略
from strategies.stocks.entry_strategies import (
    strategy_stock_mean_reversion,
    strategy_stock_mean_reversion_enhanced
)
from strategies.stocks.multi_timeframe import analyze_market_condition, should_trade_based_on_tf

def load_stock_data(ticker="1590"):
    """加載股票數據"""
    file_path = f"data/taifex_raw/STOCK_{ticker}_5m.csv"
    try:
        df = pd.read_csv(file_path)
        
        # 標準化列名
        if 'Date' in df.columns:
            df['timestamp'] = pd.to_datetime(df['Date'])
        elif 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        else:
            # 嘗試找到時間列
            time_cols = [col for col in df.columns if 'time' in col.lower() or 'date' in col.lower()]
            if time_cols:
                df['timestamp'] = pd.to_datetime(df[time_cols[0]])
            else:
                df['timestamp'] = pd.date_range(start='2026-01-01', periods=len(df), freq='5min')
        
        # 設置索引
        df = df.set_index('timestamp')
        
        # 確保必要的列存在
        required_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
        for col in required_cols:
            if col not in df.columns:
                print(f"警告: 缺少列 {col}")
                if col == 'Volume':
                    df[col] = 1000  # 默認值
                else:
                    df[col] = df['Close'] if 'Close' in df.columns else 100
        
        # 計算技術指標
        df = calculate_indicators(df)
        
        print(f"✅ 加載 {ticker} 數據: {len(df)} 行, 時間範圍: {df.index[0]} 到 {df.index[-1]}")
        return df
    
    except Exception as e:
        print(f"❌ 加載數據失敗: {e}")
        return None

def calculate_indicators(df):
    """計算技術指標"""
    # 布林帶
    df['bb_middle'] = df['Close'].rolling(window=20).mean()
    df['bb_std'] = df['Close'].rolling(window=20).std()
    df['bb_upper'] = df['bb_middle'] + 2 * df['bb_std']
    df['bb_lower'] = df['bb_middle'] - 2 * df['bb_std']
    
    # ATR
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(window=14).mean()
    
    # EMA
    df['ema_20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['ema_60'] = df['Close'].ewm(span=60, adjust=False).mean()
    
    return df

def backtest_strategy(df, strategy_func, strategy_name):
    """回測策略"""
    print(f"\n=== 回測 {strategy_name} ===")
    
    trades = []
    position = 0
    entry_price = 0
    entry_time = None
    
    for i in range(20, len(df)):  # 從第20行開始（需要足夠數據計算指標）
        current_data = df.iloc[:i+1].copy()
        current_row = df.iloc[i]
        
        # 準備state
        state = {
            "df_5m": current_data,
            "last_5m": current_row
        }
        
        # 執行策略
        cfg = {}  # 空配置
        signal = strategy_func(state, cfg)
        
        if signal:
            action = signal.get("action")
            reason = signal.get("reason", "")
            
            if action == "BUY" and position == 0:
                # 開倉
                position = 1
                entry_price = current_row['Close']
                entry_time = df.index[i]
                trades.append({
                    'time': entry_time,
                    'action': 'BUY',
                    'price': entry_price,
                    'reason': reason,
                    'strategy': strategy_name
                })
                print(f"  {entry_time}: BUY @ {entry_price:.2f} - {reason}")
                
            elif action == "SELL" and position == 1:
                # 平倉
                exit_price = current_row['Close']
                pnl_pct = (exit_price - entry_price) / entry_price * 100
                position = 0
                trades.append({
                    'time': df.index[i],
                    'action': 'SELL',
                    'price': exit_price,
                    'pnl_pct': pnl_pct,
                    'reason': reason,
                    'strategy': strategy_name
                })
                print(f"  {df.index[i]}: SELL @ {exit_price:.2f} ({pnl_pct:+.2f}%) - {reason}")
    
    # 如果最後還有持倉，強制平倉
    if position == 1 and len(df) > 0:
        exit_price = df.iloc[-1]['Close']
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        trades.append({
            'time': df.index[-1],
            'action': 'SELL',
            'price': exit_price,
            'pnl_pct': pnl_pct,
            'reason': 'FORCE_CLOSE',
            'strategy': strategy_name
        })
        print(f"  {df.index[-1]}: FORCE SELL @ {exit_price:.2f} ({pnl_pct:+.2f}%)")
    
    return trades

def analyze_results(trades, strategy_name, initial_capital=100000):
    """分析回測結果"""
    if not trades:
        print(f"\n❌ {strategy_name}: 沒有交易信號")
        return None
    
    # 將交易轉換為DataFrame
    trades_df = pd.DataFrame(trades)
    
    # 計算累積收益
    capital = initial_capital
    capital_history = [capital]
    time_history = [trades_df.iloc[0]['time'] - timedelta(minutes=5)]  # 開始時間
    
    for i in range(0, len(trades_df), 2):
        if i + 1 < len(trades_df):
            buy_trade = trades_df.iloc[i]
            sell_trade = trades_df.iloc[i + 1]
            
            if buy_trade['action'] == 'BUY' and sell_trade['action'] == 'SELL':
                # 計算收益
                pnl_pct = sell_trade.get('pnl_pct', 0)
                trade_return = capital * (pnl_pct / 100) * 0.2  # 假設20%倉位
                capital += trade_return
                
                capital_history.append(capital)
                time_history.append(sell_trade['time'])
    
    # 計算統計數據
    winning_trades = [t for t in trades if t.get('action') == 'SELL' and t.get('pnl_pct', 0) > 0]
    losing_trades = [t for t in trades if t.get('action') == 'SELL' and t.get('pnl_pct', 0) <= 0]
    
    total_trades = len(winning_trades) + len(losing_trades)
    win_rate = len(winning_trades) / total_trades * 100 if total_trades > 0 else 0
    
    avg_win = np.mean([t['pnl_pct'] for t in winning_trades]) if winning_trades else 0
    avg_loss = np.mean([t['pnl_pct'] for t in losing_trades]) if losing_trades else 0
    profit_factor = abs(sum([t['pnl_pct'] for t in winning_trades]) / sum([t['pnl_pct'] for t in losing_trades])) if losing_trades else float('inf')
    
    max_drawdown = 0
    peak = initial_capital
    for cap in capital_history:
        if cap > peak:
            peak = cap
        drawdown = (peak - cap) / peak * 100
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    
    print(f"\n📊 {strategy_name} 回測結果:")
    print(f"   總交易次數: {total_trades}")
    print(f"   勝率: {win_rate:.1f}%")
    print(f"   平均獲利: {avg_win:.2f}%")
    print(f"   平均虧損: {avg_loss:.2f}%")
    print(f"   獲利因子: {profit_factor:.2f}")
    print(f"   最大回撤: {max_drawdown:.2f}%")
    print(f"   最終資金: {capital:,.0f} TWD (+{(capital - initial_capital)/initial_capital*100:.1f}%)")
    
    return {
        'strategy': strategy_name,
        'total_trades': total_trades,
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': profit_factor,
        'max_drawdown': max_drawdown,
        'final_capital': capital,
        'return_pct': (capital - initial_capital) / initial_capital * 100,
        'capital_history': capital_history,
        'time_history': time_history
    }

def plot_results(results_list, ticker):
    """繪製回測結果圖表"""
    fig, axes = plt.subplots(2, 2, figsize=(15, 10))
    
    # 1. 資金曲線
    ax1 = axes[0, 0]
    for result in results_list:
        if result and 'capital_history' in result and 'time_history' in result:
            ax1.plot(result['time_history'], result['capital_history'], 
                    label=f"{result['strategy']} ({result['return_pct']:.1f}%)", 
                    linewidth=2)
    ax1.set_title(f'{ticker} - 資金曲線比較')
    ax1.set_xlabel('時間')
    ax1.set_ylabel('資金 (TWD)')
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    
    # 2. 勝率比較
    ax2 = axes[0, 1]
    strategies = []
    win_rates = []
    for result in results_list:
        if result:
            strategies.append(result['strategy'])
            win_rates.append(result['win_rate'])
    
    if strategies:
        bars = ax2.bar(strategies, win_rates, color=['blue', 'green'])
        ax2.set_title('勝率比較')
        ax2.set_ylabel('勝率 (%)')
        ax2.set_ylim(0, 100)
        
        # 在柱狀圖上顯示數值
        for bar, rate in zip(bars, win_rates):
            height = bar.get_height()
            ax2.text(bar.get_x() + bar.get_width()/2., height + 1,
                    f'{rate:.1f}%', ha='center', va='bottom')
    
    # 3. 獲利因子比較
    ax3 = axes[1, 0]
    profit_factors = []
    for result in results_list:
        if result:
            profit_factors.append(min(result['profit_factor'], 10))  # 限制顯示範圍
    
    if strategies:
        bars = ax3.bar(strategies, profit_factors, color=['blue', 'green'])
        ax3.set_title('獲利因子比較')
        ax3.set_ylabel('獲利因子')
        ax3.axhline(y=1.0, color='red', linestyle='--', alpha=0.5)
        
        # 在柱狀圖上顯示數值
        for bar, pf in zip(bars, profit_factors):
            height = bar.get_height()
            ax3.text(bar.get_x() + bar.get_width()/2., height + 0.1,
                    f'{pf:.2f}', ha='center', va='bottom')
    
    # 4. 最大回撤比較
    ax4 = axes[1, 1]
    drawdowns = []
    for result in results_list:
        if result:
            drawdowns.append(result['max_drawdown'])
    
    if strategies:
        bars = ax4.bar(strategies, drawdowns, color=['blue', 'green'])
        ax4.set_title('最大回撤比較')
        ax4.set_ylabel('最大回撤 (%)')
        
        # 在柱狀圖上顯示數值
        for bar, dd in zip(bars, drawdowns):
            height = bar.get_height()
            ax4.text(bar.get_x() + bar.get_width()/2., height + 0.1,
                    f'{dd:.2f}%', ha='center', va='bottom')
    
    plt.tight_layout()
    plt.savefig(f'vmodel_backtest_{ticker}.png', dpi=150, bbox_inches='tight')
    print(f"\n📈 圖表已保存: vmodel_backtest_{ticker}.png")
    plt.show()

def main():
    """主函數"""
    print("=" * 60)
    print("V模型測試：增強版均值回歸策略 vs 基礎版")
    print("=" * 60)
    
    # 測試的股票代碼
    tickers = ['1590', '2330', '1216']  # 選擇幾個有數據的股票
    
    for ticker in tickers:
        print(f"\n🎯 測試股票: {ticker}")
        
        # 加載數據
        df = load_stock_data(ticker)
        if df is None or len(df) < 100:
            print(f"  跳過 {ticker}: 數據不足")
            continue
        
        # 回測基礎版策略
        basic_trades = backtest_strategy(df, strategy_stock_mean_reversion, "基礎版均值回歸")
        basic_result = analyze_results(basic_trades, "基礎版均值回歸")
        
        # 回測增強版策略
        enhanced_trades = backtest_strategy(df, strategy_stock_mean_reversion_enhanced, "增強版均值回歸")
        enhanced_result = analyze_results(enhanced_trades, "增強版均值回歸")
        
        # 繪製比較圖表
        results_list = [basic_result, enhanced_result]
        plot_results(results_list, ticker)
        
        # 輸出詳細比較
        print(f"\n📋 {ticker} 策略比較摘要:")
        print("  策略           | 交易次數 | 勝率   | 平均獲利 | 平均虧損 | 獲利因子 | 最大回撤 | 最終報酬")
        print("  " + "-" * 85)
        
        for result in results_list:
            if result:
                print(f"  {result['strategy']:15} | {result['total_trades']:8} | {result['win_rate']:6.1f}% | "
                      f"{result['avg_win']:8.2f}% | {result['avg_loss']:8.2f}% | {result['profit_factor']:9.2f} | "
                      f"{result['max_drawdown']:8.2f}% | {result['return_pct']:7.1f}%")
        
        print()

if __name__ == "__main__":
    main()