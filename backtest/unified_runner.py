#!/usr/bin/env python3
"""
Unified Backtest Runner V2 — 公平比較所有策略

Features:
- 逐 bar 模擬真實進出場
- 統一手續費/稅率計算
- 自動發現 Registry 所有策略
- 比較 CANSLIM, Futures Plugins, Stock Strategies
- 輸出 CSV + Markdown 報告

Usage:
    python3 backtest/unified_runner.py
"""
import sys
sys.path.insert(0, '.')

import os
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional
import yaml

# Internal imports
from core.strategy_registry import StrategyRegistry
from core.strategy_context import StrategyContext, PositionView, MarketData
from core.signal import Signal

# ==================== Configuration ====================
START_DATE = '2020-01-01'
END_DATE = '2026-04-01'
INITIAL_CAPITAL = 1_000_000
FEE_RATE = 0.001425
TAX_RATE = 0.003
STOP_LOSS_PTS = 60  # Futures stop-loss in points
TAKE_PROFIT_PTS = 100  # Futures take-profit in points
POINT_VALUE = 200  # TMF point value
MAX_STOCKS = 15

# ==================== Simple Backtest Engine ====================
class SimpleBacktestEngine:
    """Simple backtester that tracks position and PnL correctly."""
    def __init__(self, initial_capital=INITIAL_CAPITAL, point_value=POINT_VALUE, fee_rate=FEE_RATE, tax_rate=TAX_RATE):
        self.initial_capital = initial_capital
        self.point_value = point_value
        self.fee_rate = fee_rate
        self.tax_rate = tax_rate
        
        self.cash = initial_capital
        self.position = 0  # +1 long, -1 short, 0 flat
        self.entry_price = 0.0
        self.entry_date = None
        self.trades = []
        self.equity_curve = []
        self.trade_num = 0

    def _open_position(self, ticker, price, date, direction=1):
        """Open position. direction: 1=long, -1=short"""
        lots = 1  # 1 contract for futures
        margin = lots * 170000  # TMF margin per contract (TWD)
        fee = lots * price * self.point_value * self.fee_rate

        if self.cash < margin + fee:
            return False  # Insufficient funds

        self.cash -= (margin + fee)
        self.position = direction
        self.entry_price = price
        self.entry_date = date
        self.trade_num += 1

        action = "BUY" if direction > 0 else "SELL"
        self.trades.append({
            'num': self.trade_num, 'ticker': ticker, 'action': action,
            'date': date, 'price': price, 'lots': lots, 'cost': round(margin + fee, 0),
        })
        return True

    def _close_position(self, ticker, price, date, reason=""):
        """Close position and calculate PnL"""
        if self.position == 0:
            return

        lots = 1

        # Calculate PnL (without fees/tax yet)
        if self.position > 0:  # Long
            pnl = (price - self.entry_price) * lots * self.point_value
        else:  # Short
            pnl = (self.entry_price - price) * lots * self.point_value

        # Fees and tax on exit
        fee = lots * price * self.point_value * self.fee_rate
        tax = lots * price * self.point_value * self.tax_rate

        # Get back margin + PnL - fees/tax
        self.cash += (170000 + pnl - fee - tax)  # Return margin + PnL - fees

        self.trade_num += 1
        self.trades.append({
            'num': self.trade_num, 'ticker': ticker, 'action': 'EXIT',
            'date': date, 'price': price, 'lots': lots,
            'pnl': round(pnl - fee - tax, 0), 'pnl_pct': round(pnl / (self.entry_price * lots * self.point_value) * 100, 2),
            'reason': reason, 'entry_price': self.entry_price,
            'holding_days': (date - self.entry_date).days if self.entry_date else 0,
        })

        # Reset position
        self.position = 0
        self.entry_price = 0.0
        self.entry_date = None

    def _check_exit_conditions(self, ticker, price, date):
        """Check stop-loss and take-profit conditions"""
        if self.position == 0:
            return None
        
        if self.position > 0:  # Long
            if price <= self.entry_price - STOP_LOSS_PTS:
                return 'STOP_LOSS'
            if price >= self.entry_price + TAKE_PROFIT_PTS:
                return 'TAKE_PROFIT'
        else:  # Short
            if price >= self.entry_price + STOP_LOSS_PTS:
                return 'STOP_LOSS'
            if price <= self.entry_price - TAKE_PROFIT_PTS:
                return 'TAKE_PROFIT'
        return None

    def run(self, df, strategy, strategy_name="unknown", config=None):
        """Run backtest on DataFrame with OHLCV + indicator columns"""
        if config is None:
            config = {}

        n = len(df)
        if n < 50:
            return {}

        # Initialize strategy
        ctx_init = StrategyContext(
            market=MarketData(last_bar={}, df_5m=df.iloc[:50]),
            position=PositionView(),
            config=config,
            bar_counter=0
        )
        strategy.init(ctx_init)

        signal_count = 0
        trade_count = 0

        # Run bar-by-bar
        for i in range(50, n):
            date = df.index[i]
            bar = df.iloc[i]
            bar_dict = bar.to_dict()

            # 1. Check exit conditions
            if self.position != 0:
                exit_signal = self._check_exit_conditions("TMF", bar['Close'], date)
                if exit_signal:
                    self._close_position("TMF", bar['Close'], date, exit_signal)
                    trade_count += 1
                    continue

            # 2. Get strategy signal
            ctx = StrategyContext(
                market=MarketData(last_bar=bar_dict, df_5m=df.iloc[max(0,i-100):i+1]),
                position=PositionView(
                    size=self.position,
                    entry_price=self.entry_price,
                ),
                config=config,
                bar_counter=i
            )
            sig = strategy.on_bar(ctx)

            if sig:
                signal_count += 1

            # 3. Execute signal
            if sig and self.position == 0:
                if sig.action == "BUY":
                    self._open_position("TMF", bar['Close'], date, direction=1)
                    trade_count += 1
                elif sig.action == "SELL":
                    self._open_position("TMF_SHORT", bar['Close'], date, direction=-1)
                    trade_count += 1
            
            # 4. Record equity
            port_value = self.cash
            if self.position != 0:
                # Add back margin (it's collateral, not spent)
                port_value += 170000  # margin_per_contract
                # Add unrealized PnL
                if self.position > 0:
                    port_value += (bar['Close'] - self.entry_price) * self.point_value
                else:
                    port_value += (self.entry_price - bar['Close']) * self.point_value
            self.equity_curve.append({'date': date, 'equity': port_value})
        
        # Close any remaining position
        if self.position != 0:
            self._close_position("TMF", df.iloc[-1]['Close'], df.index[-1], "END_OF_TEST")
        
        return self._calculate_metrics()

    def _calculate_metrics(self):
        """Calculate performance metrics"""
        if not self.equity_curve:
            return {}
        
        eq_df = pd.DataFrame(self.equity_curve).set_index('date')
        if len(eq_df) < 2:
            return {}
        
        eq = eq_df['equity']
        total_ret = eq.iloc[-1] / self.initial_capital - 1
        years = (eq.index[-1] - eq.index[0]).days / 365.25
        cagr = (1 + total_ret) ** (1/years) - 1 if years > 0 else 0
        
        daily_ret = eq.pct_change().dropna()
        sharpe = daily_ret.mean() / daily_ret.std() * np.sqrt(252) if daily_ret.std() > 0 else 0
        
        # Max drawdown
        rolling_max = eq.cummax()
        drawdown = (eq - rolling_max) / rolling_max
        max_dd = drawdown.min()
        
        # Trade statistics
        exits = [t for t in self.trades if t.get('action') == 'EXIT']
        if exits:
            wins = [t for t in exits if t.get('pnl', 0) > 0]
            losses = [t for t in exits if t.get('pnl', 0) <= 0]
            win_rate = len(wins) / len(exits) if exits else 0
            gp = sum(t.get('pnl', 0) for t in wins)
            gl = abs(sum(t.get('pnl', 0) for t in losses))
            pf = gp / gl if gl > 0 else float('inf')
        else:
            win_rate = 0
            pf = 0
        
        return {
            'CAGR': cagr,
            'Total Return': total_ret,
            'Sharpe': sharpe,
            'Max Drawdown': max_dd,
            'Win Rate': win_rate,
            'Profit Factor': pf,
            'Trades': len(exits),
            'Years': years,
        }

# ==================== Unified Runner ====================
def run_unified_backtest():
    """Run all strategies and generate comparison report."""
    print("=" * 70)
    print("🚀 統一回測引擎 V2 — 公平比較所有策略")
    print("=" * 70)
    print(f"期間: {START_DATE} → {END_DATE}")
    print(f"初始資金: ${INITIAL_CAPITAL:,.0f}")
    print(f"停損: {STOP_LOSS_PTS} pts | 停利: {TAKE_PROFIT_PTS} pts")
    print()

    results = []
    reg = StrategyRegistry()
    reg.discover()

    # 1. Load TMF historical data (used for all futures strategies)
    print("📊 [1/3] Loading TMF historical data...")
    tmf_csv = Path("data/tmf_full_2026.csv")
    if not tmf_csv.exists():
        print(f"  ❌ {tmf_csv} not found")
        return None
    
    df_tmf = pd.read_csv(tmf_csv, parse_dates=["timestamp"], index_col="timestamp")
    df_tmf = df_tmf.resample("5min").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum"
    }).dropna()
    
    # Calculate indicators
    from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze
    df_tmf = calculate_futures_squeeze(df_tmf)
    
    print(f"  ✅ Loaded {len(df_tmf)} 5m bars ({df_tmf.index[0].date()} → {df_tmf.index[-1].date()})")

    # 2. Run Futures Plugin Strategies
    print("\n📊 [2/3] Running Futures Plugins...")
    with open("config/futures.yaml") as f:
        cfg = yaml.safe_load(f)

    for item in reg.list_all():
        if item.get("asset_class") != "futures" or not item.get("available"):
            continue
        name = item["name"]
        strategy = reg.get(name)
        if strategy is None:
            continue

        try:
            bt = SimpleBacktestEngine(INITIAL_CAPITAL)
            metrics = bt.run(df_tmf, strategy, name, cfg)
            
            if metrics:
                results.append({
                    'strategy': name,
                    'asset_class': 'futures',
                    'cagr': metrics.get('CAGR', 0),
                    'total_return': metrics.get('Total Return', 0),
                    'sharpe': metrics.get('Sharpe', 0),
                    'max_dd': metrics.get('Max Drawdown', 0),
                    'win_rate': metrics.get('Win Rate', 0),
                    'profit_factor': metrics.get('Profit Factor', 0),
                    'trades': metrics.get('Trades', 0),
                    'years': metrics.get('Years', 0),
                })
                print(f"  ✅ {name}: CAGR={metrics.get('CAGR', 0):.2%}  PF={metrics.get('Profit Factor', 0):.2f}  Trades={metrics.get('Trades', 0)}")
            else:
                print(f"  ⚠️ {name}: No results (no signals generated)")
        except Exception as e:
            print(f"  ❌ {name} error: {e}")
            import traceback
            traceback.print_exc()

    # 3. Run CANSLIM
    print("\n📊 [3/3] Running CANSLIM (Technical)...")
    try:
        import yfinance as yf
        from scripts.backtest_canslim import Backtester as CANSLIMBacktester
        
        UNIVERSE = ['2330', '2317', '2454', '2308', '2303', '1303', '2881', '2882', '2884', '2886',
                    '2891', '2892', '2880', '5871', '2801', '1402', '1101', '1102', '1216', '1326',
                    '2002', '2007', '2324', '2353', '2354', '2357', '2360', '2382', '2395', '2409',
                    '2412', '2427', '2448', '2492', '3008', '3021', '3034', '3044', '3045', '3105',
                    '3231', '3259', '3311', '3324', '3413', '3443', '3481', '3532', '3533', '3661',
                    '3694', '4938', '4943', '6153', '6201', '6223', '6239', '6271', '6415', '6505',
                    '6533', '6669', '8070', '8163', '9910', '1525', '1590', '4583', '4768', '2233',
                    '2049', '2059', '2207', '2368', '3711', '2379', '1707']
        
        price_data = {}
        for t in UNIVERSE:
            try:
                df = yf.Ticker(f"{t}.TW").history(start=START_DATE, end=END_DATE, auto_adjust=False)
                if len(df) > 60:
                    price_data[t] = df[['Open', 'High', 'Low', 'Close', 'Volume']]
            except: pass
        
        if price_data:
            bt = CANSLIMBacktester(INITIAL_CAPITAL)
            metrics = bt.run(price_data)
            if metrics:
                results.append({
                    'strategy': 'CANSLIM (Technical)',
                    'asset_class': 'stocks',
                    'cagr': metrics.get('CAGR', 0),
                    'total_return': metrics.get('Total Return', 0),
                    'sharpe': metrics.get('Sharpe', 0),
                    'max_dd': metrics.get('Max Drawdown', 0),
                    'win_rate': metrics.get('Win Rate', 0),
                    'profit_factor': metrics.get('Profit Factor', 0),
                    'trades': metrics.get('Trades', 0),
                    'years': metrics.get('Years', 0),
                })
                print(f"  ✅ CANSLIM: CAGR={metrics.get('CAGR', 0):.2%}  PF={metrics.get('Profit Factor', 0):.2f}")
    except Exception as e:
        print(f"  ❌ CANSLIM error: {e}")

    # 4. Generate Report
    if not results:
        print("\n⚠️ No results to report")
        return None
    
    print("\n📊 生成報告...")
    df_results = pd.DataFrame(results)
    df_results = df_results.sort_values('cagr', ascending=False)
    
    # Save CSV
    report_dir = Path("exports/backtest")
    report_dir.mkdir(parents=True, exist_ok=True)
    csv_path = report_dir / "unified_report.csv"
    df_results.to_csv(csv_path, index=False)
    
    # Generate Markdown Report
    md_path = report_dir / "unified_report.md"
    with open(md_path, 'w') as f:
        f.write("# 📊 Unified Backtest Report\n\n")
        f.write(f"**期間**: {START_DATE} → {END_DATE}  ")
        f.write(f"**初始資金**: ${INITIAL_CAPITAL:,.0f}  ")
        f.write(f"**停損**: {STOP_LOSS_PTS} pts | **停利**: {TAKE_PROFIT_PTS} pts  ")
        f.write(f"**生成時間**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        f.write("## 策略比較\n\n")
        f.write("| 策略 | 資產類別 | CAGR | 總報酬 | Sharpe | MaxDD | 勝率 | PF | 交易數 |\n")
        f.write("|------|---------|------|--------|--------|-------|------|----|--------|\n")
        for _, row in df_results.iterrows():
            f.write(f"| {row['strategy']} | {row['asset_class']} | "
                    f"{row['cagr']:.2%} | {row['total_return']:.2%} | "
                    f"{row['sharpe']:.2f} | {row['max_dd']:.2%} | "
                    f"{row['win_rate']:.1%} | {row['profit_factor']:.2f} | "
                    f"{row['trades']:.0f} |\n")

        # Add Live Chip Analysis Section
        f.write("\n## 📈 今日籌碼分析 (Live Chip Analysis)\n\n")
        f.write("> 注意：以下為即時爬蟲資料，用於確認「真實分點」介入情況。\n\n")
        
        try:
            # 設定為 Live Mode 抓取真實資料
            from core.chip_analyzer import chip_analyzer
            chip_analyzer.mode = "live"
            
            # 掃描熱門股 (範例)
            sample_stocks = ['2330', '2454', '2317']
            f.write("| 代號 | 主力淨買超 (張) | 籌碼評分 (0-10) |\n")
            f.write("|------|-----------------|----------------|\n")
            
            for ticker in sample_stocks:
                score = chip_analyzer.get_chip_score(ticker)
                # 重新抓取 NetBuy 數據以便顯示 (因為 get_chip_score 只回傳分數)
                # 這裡我們簡化顯示
                f.write(f"| {ticker} | (需查看 Log) | {score:.1f} |\n")
                
        except Exception as e:
            f.write(f"**籌碼分析失敗**: {e}\n")

        f.write("\n## 結論\n\n")
        if len(df_results) > 0:
            best = df_results.iloc[0]
            f.write(f"**最佳策略**: {best['strategy']} (CAGR={best['cagr']:.2%}, PF={best['profit_factor']:.2f})\n\n")
            f.write(f"**最低風險**: {df_results.loc[df_results['max_dd'].idxmin(), 'strategy']} (MaxDD={df_results['max_dd'].min():.2%})\n\n")
            f.write(f"**最高勝率**: {df_results.loc[df_results['win_rate'].idxmax(), 'strategy']} (WR={df_results['win_rate'].max():.1%})\n")
    
    print(f"  ✅ 報告已儲存: {csv_path}")
    print(f"  ✅ Markdown: {md_path}")
    
    # Print Summary
    print("\n" + "=" * 70)
    print("📊 最終比較:")
    print("=" * 70)
    print(df_results[['strategy', 'cagr', 'profit_factor', 'max_dd', 'win_rate', 'trades']].to_string(index=False))
    
    return df_results

if __name__ == "__main__":
    run_unified_backtest()
