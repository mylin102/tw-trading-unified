#!/usr/bin/env python3
"""P0+P1 優化回測驗證 - 精簡版"""

import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import yaml
import os

sys.path.append('.')

def load_stock_data(stock_code):
    """加載股票數據"""
    csv_file = f'data/taifex_raw/STOCK_{stock_code}_5m.csv'
    if not os.path.exists(csv_file):
        return None
    
    df = pd.read_csv(csv_file)
    
    # 檢查並重命名時間列
    if 'datetime' in df.columns:
        df = df.rename(columns={'datetime': 'timestamp'})
    elif 'Date' in df.columns:
        df = df.rename(columns={'Date': 'timestamp'})
    
    if 'timestamp' not in df.columns:
        print(f"❌ {stock_code}: 缺少timestamp列，現有列: {list(df.columns)}")
        return None
    
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df = df.set_index('timestamp')
    
    # 確保必要的列存在
    required_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
    for col in required_cols:
        if col not in df.columns:
            print(f"❌ {stock_code}: 缺少{col}列")
            return None
    
    return df

def calculate_indicators(df):
    """計算技術指標"""
    from strategies.options.options_engine.engine.indicators import calculate_stock_squeeze
    
    try:
        df = calculate_stock_squeeze(df)
    except Exception as e:
        print(f"指標計算錯誤: {e}")
    
    # 添加簡單的移動平均線
    df['ma20'] = df['Close'].rolling(20).mean()
    df['ma60'] = df['Close'].rolling(60).mean()
    
    # 添加ATR
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()
    
    return df

def simulate_trades(df, stock_code, config):
    """模擬交易"""
    from strategies.stocks.entry_strategies import strategy_stock_mean_reversion
    from strategies.stocks.exit_enhancer import get_exit_enhancer
    
    trades = []
    positions = {}
    enhancer = get_exit_enhancer()
    
    capital = 100000  # 初始資金
    position_size = 20000  # 每筆交易金額
    cash = capital
    
    for i in range(60, len(df)):  # 從第60根K棒開始（確保有足夠的指標數據）
        current_bar = df.iloc[i]
        current_time = df.index[i]
        
        # 檢查持倉的出場信號
        for ticker in list(positions.keys()):
            pos = positions[ticker]
            exit_signal = enhancer.check_exit_signals(
                ticker=ticker,
                current_price=current_bar['Close'],
                current_time=current_time,
                bars_passed=1
            )
            
            if exit_signal:
                # 執行賣出
                entry_price = pos['entry_price']
                exit_price = current_bar['Close']
                qty = pos['qty']
                
                pnl = (exit_price - entry_price) * qty
                cash += exit_price * qty
                
                trades.append({
                    'stock': ticker,
                    'action': 'SELL',
                    'price': exit_price,
                    'qty': qty,
                    'time': current_time,
                    'reason': exit_signal['reason'],
                    'pnl': pnl
                })
                
                del positions[ticker]
                enhancer.remove_position(ticker)
        
        # 如果沒有持倉，檢查買入信號
        if len(positions) == 0:
            # 準備策略狀態
            state = {
                "df_5m": df.iloc[:i+1],
                "last_5m": current_bar
            }
            
            # 執行策略
            signal = strategy_stock_mean_reversion(state, config)
            
            if signal and signal['action'] == 'BUY':
                # 計算可買數量
                price = current_bar['Close']
                qty = int(position_size / price)
                
                if qty > 0 and cash >= price * qty:
                    # 執行買入
                    cash -= price * qty
                    positions[stock_code] = {
                        'entry_price': price,
                        'qty': qty,
                        'entry_time': current_time
                    }
                    
                    # 記錄到出場增強器
                    entry_data = {
                        "entry_price": price,
                        "stop_loss": signal.get('stop_loss', price * 0.95),
                        "take_profit": signal.get('take_profit', price * 1.15),
                        "trailing_start_pct": signal.get('metadata', {}).get('trailing_start_pct', 0.075),
                        "trailing_stop_pct": signal.get('metadata', {}).get('trailing_stop_pct', 0.02),
                        "max_holding_bars": 30
                    }
                    enhancer.update_position(stock_code, entry_data)
                    
                    trades.append({
                        'stock': stock_code,
                        'action': 'BUY',
                        'price': price,
                        'qty': qty,
                        'time': current_time,
                        'reason': signal['reason']
                    })
    
    # 平倉所有持倉
    for ticker, pos in positions.items():
        last_price = df.iloc[-1]['Close']
        pnl = (last_price - pos['entry_price']) * pos['qty']
        cash += last_price * pos['qty']
        
        trades.append({
            'stock': ticker,
            'action': 'SELL',
            'price': last_price,
            'qty': pos['qty'],
            'time': df.index[-1],
            'reason': 'END_OF_BACKTEST',
            'pnl': pnl
        })
    
    return trades, cash

def calculate_metrics(trades, initial_capital, final_capital):
    """計算績效指標"""
    if len(trades) == 0:
        return {
            'total_trades': 0,
            'win_rate': 0,
            'total_pnl': 0,
            'sharpe_ratio': 0,
            'max_drawdown': 0
        }
    
    # 只計算有PNL的交易（賣出）
    sell_trades = [t for t in trades if 'pnl' in t]
    
    if len(sell_trades) == 0:
        return {
            'total_trades': len(trades),
            'win_trades': 0,
            'loss_trades': 0,
            'win_rate': 0,
            'total_pnl': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'profit_factor': 0
        }
    
    # 計算勝率
    win_trades = [t for t in sell_trades if t['pnl'] > 0]
    loss_trades = [t for t in sell_trades if t['pnl'] < 0]
    
    win_rate = len(win_trades) / len(sell_trades) if len(sell_trades) > 0 else 0
    
    # 計算總損益
    total_pnl = sum(t['pnl'] for t in sell_trades)
    
    # 計算平均獲利/虧損
    avg_win = np.mean([t['pnl'] for t in win_trades]) if win_trades else 0
    avg_loss = np.mean([t['pnl'] for t in loss_trades]) if loss_trades else 0
    
    # 計算獲利因子
    gross_profit = sum(t['pnl'] for t in win_trades) if win_trades else 0
    gross_loss = abs(sum(t['pnl'] for t in loss_trades)) if loss_trades else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
    
    return {
        'total_trades': len(trades),
        'completed_trades': len(sell_trades),
        'win_trades': len(win_trades),
        'loss_trades': len(loss_trades),
        'win_rate': win_rate,
        'total_pnl': total_pnl,
        'return_pct': (final_capital - initial_capital) / initial_capital * 100,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': profit_factor
    }

def main():
    """主函數"""
    print("P0+P1 優化回測驗證")
    print("=" * 60)
    
    # 加載配置
    with open('config/stocks.yaml', 'r') as f:
        config = yaml.safe_load(f)
    
    stock_config = config['stocks']
    watchlist = stock_config['watchlist']
    
    # 策略配置
    strategy_config = {
        'stop_loss_pct': stock_config.get('stop_loss_pct', 0.05),
        'take_profit_pct': stock_config.get('take_profit_pct', 0.15),
        'atr_stop_multiplier': stock_config.get('atr_mult', 2.0),
        'trailing_stop_pct': stock_config.get('trailing_stop_pct', 0.02)
    }
    
    # 風險控制配置
    risk_config = stock_config.get('risk_control', {})
    strategy_config.update(risk_config)
    
    print(f"測試股票數量: {len(watchlist)}")
    print(f"策略配置: {strategy_config}")
    
    all_results = []
    
    # 測試每支股票
    for stock_code in watchlist[:5]:  # 先測試前5支
        print(f"\n測試股票: {stock_code}")
        
        # 加載數據
        df = load_stock_data(stock_code)
        if df is None:
            print(f"  ❌ 數據不可用")
            continue
        
        print(f"  數據期間: {df.index[0]} 到 {df.index[-1]}")
        print(f"  數據筆數: {len(df)}")
        
        # 計算指標
        df = calculate_indicators(df)
        
        # 模擬交易
        trades, final_cash = simulate_trades(df, stock_code, strategy_config)
        
        # 計算績效
        metrics = calculate_metrics(trades, 100000, final_cash)
        
        print(f"  交易次數: {metrics['total_trades']}")
        print(f"  完成交易: {metrics['completed_trades']}")
        print(f"  勝率: {metrics['win_rate']:.1%}")
        print(f"  總損益: {metrics['total_pnl']:,.0f} TWD")
        print(f"  報酬率: {metrics['return_pct']:.1f}%")
        
        all_results.append({
            'stock': stock_code,
            'metrics': metrics,
            'trades': trades
        })
    
    # 匯總結果
    print("\n" + "=" * 60)
    print("匯總結果:")
    
    if all_results:
        total_trades = sum(r['metrics']['total_trades'] for r in all_results)
        completed_trades = sum(r['metrics']['completed_trades'] for r in all_results)
        win_trades = sum(r['metrics']['win_trades'] for r in all_results)
        total_pnl = sum(r['metrics']['total_pnl'] for r in all_results)
        
        avg_win_rate = np.mean([r['metrics']['win_rate'] for r in all_results])
        
        print(f"總交易次數: {total_trades}")
        print(f"完成交易: {completed_trades}")
        print(f"勝率: {avg_win_rate:.1%}")
        print(f"總損益: {total_pnl:,.0f} TWD")
        
        # 分析出場原因
        if all_results:
            all_trades = []
            for result in all_results:
                all_trades.extend(result['trades'])
            
            sell_trades = [t for t in all_trades if t['action'] == 'SELL' and 'reason' in t]
            if sell_trades:
                print("\n出場原因分析:")
                reasons = {}
                for trade in sell_trades:
                    reason = trade['reason']
                    reasons[reason] = reasons.get(reason, 0) + 1
                
                for reason, count in sorted(reasons.items(), key=lambda x: x[1], reverse=True):
                    print(f"  {reason}: {count}次")
    
    print("\n回測完成!")

if __name__ == "__main__":
    main()