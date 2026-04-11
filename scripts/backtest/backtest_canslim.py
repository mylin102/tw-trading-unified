#!/usr/bin/env python3
"""
CANSLIM Backtest — Taiwan Stock Market (Technical + Chip Proxy Version)

Uses pure price/volume data to proxy CANSLIM fundamentals:
  C: 3-month price momentum (proxy for EPS growth)
  A: 6-month trend (proxy for revenue growth)
  N: Proximity to 52-week high
  S: Volume breakout (demand indicator)
  L: Relative strength vs market
  CHIP: Key Broker Branch Net Buy (籌碼分點連買) -> 回測使用 Volume Ratio 代理

Transaction costs (Taiwan): Buy 0.1425%, Sell 0.1425% + 0.3% tax
"""
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime
import warnings
import sys
from pathlib import Path

# Add project root to path to import core modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# We do NOT import chip_analyzer for backtest because it tries to scrape web.
# Instead, we calculate Chip Proxy locally from volume.

warnings.filterwarnings('ignore')

# ==================== Configuration ====================
START_DATE = '2020-01-01'
END_DATE = '2026-04-01'
REBALANCE_FREQ = 'QE'
MAX_STOCKS = 15
STOP_LOSS = 0.08
TAKE_PROFIT = 0.20
FEE_RATE = 0.001425
TAX_RATE = 0.003

UNIVERSE = [
    '2330', '2317', '2454', '2308', '2303', '1303', '2881', '2882', '2884', '2886',
    '2891', '2892', '2880', '5871', '2801', '1402', '1101', '1102', '1216', '1326',
    '2002', '2007', '2324', '2353', '2354', '2357', '2360', '2382', '2395', '2409',
    '2412', '2427', '2448', '2492', '3008', '3021', '3034', '3044', '3045', '3105',
    '3231', '3259', '3311', '3324', '3413', '3443', '3481', '3532', '3533', '3661',
    '3694', '4938', '4943', '6153', '6201', '6223', '6239', '6271', '6415', '6505',
    '6533', '6669', '8070', '8163', '9910', '1525', '1590', '4583', '4768', '2233',
    '2049', '2059', '2207', '2368', '3711', '2379', '1707',
]

# ==================== CANSLIM Technical Screening ====================
def canslim_score(ticker, df, date, market_ret=0):
    """
    CANSLIM score (0-100) using pure technical data.
    """
    # Get data up to the screening date
    mask = df.index <= date
    if mask.sum() < 120:  # Need at least ~6 months
        return 0, {}
    hist = df[mask].iloc[-120:]  # Last ~120 trading days
    close = hist['Close']
    volume = hist['Volume']

    # C: 3-month price momentum (proxy for current EPS growth)
    if len(close) >= 60:
        ret_3m = (close.iloc[-1] / close.iloc[-60]) - 1
    else:
        ret_3m = 0
    c_score = min(max(ret_3m * 100, 0), 40)  # 0-40 points

    # A: 6-month trend (proxy for sustained revenue growth)
    if len(close) >= 120:
        ret_6m = (close.iloc[-1] / close.iloc[0]) - 1
    else:
        ret_6m = ret_3m
    a_score = min(max(ret_6m * 50, 0), 20)  # 0-20 points

    # N: Proximity to 52-week high (new highs preferred)
    if len(close) >= 240:
        high_52w = close.iloc[-240:].max()
    else:
        high_52w = close.max()
    if high_52w > 0:
        n_score = max(0, 20 * (1 - (high_52w - close.iloc[-1]) / high_52w))
    else:
        n_score = 0

    # S: Volume breakout (recent volume vs 20-day average)
    vol_5d = volume.iloc[-5:].mean() if len(volume) >= 5 else volume.mean()
    vol_20d = volume.mean()
    if vol_20d > 0:
        s_score = min(10, 5 * max(0, vol_5d / vol_20d - 1))
    else:
        s_score = 0

    # L: Relative strength vs market (outperformance)
    l_score = min(10, max(0, (ret_3m - market_ret) * 50 + 5))

    # CHIP: Volume Ratio Proxy for Chip Analysis (籌碼分點代理)
    # Logic: High volume relative to average implies Institutional Activity (Chip)
    # 我們用「爆量」來模擬「主力介入」
    vol_now = volume.iloc[-1]
    vol_avg = volume.rolling(20).mean().iloc[-1]
    if vol_avg > 0:
        vol_ratio = vol_now / vol_avg
        # Scoring: 1.5x -> 3pts, 2.0x -> 5pts, 3.0x -> 10pts
        if vol_ratio >= 3.0:
            chip_score = 10.0
        elif vol_ratio >= 2.0:
            chip_score = 5.0
        elif vol_ratio >= 1.5:
            chip_score = 3.0
        else:
            chip_score = 0.0
    else:
        chip_score = 0.0

    # 加權總分 (Chip 佔 10%)
    total = c_score + a_score + n_score + s_score + l_score + chip_score

    return total, {
        'C': round(c_score, 1),
        'A': round(a_score, 1),
        'N': round(n_score, 1),
        'S': round(s_score, 1),
        'L': round(l_score, 1),
        'CHIP': round(chip_score, 1),
    }

# ==================== Backtest Engine ====================
class Position:
    def __init__(self, ticker, entry_price, shares, entry_date, stop_loss_pct=0.08):
        self.ticker = ticker
        self.entry_price = entry_price
        self.shares = shares
        self.entry_date = entry_date
        self.high_water = entry_price
        self.stop_loss_pct = stop_loss_pct  # Dynamic Stop Loss

    def check_exit(self, price):
        # 1. Trailing Stop / Hard Stop Loss
        if price <= self.entry_price * (1 - self.stop_loss_pct):
            return 'STOP_LOSS'
        # 2. Profit Taking Trailing
        if self.high_water >= self.entry_price * (1 + TAKE_PROFIT):
            if price <= self.high_water * 0.95:
                return 'TAKE_PROFIT'
        self.high_water = max(self.high_water, price)
        return None

class Backtester:
    def __init__(self, initial_capital=1_000_000):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions = {}
        self.trades = []
        self.equity = []
        self.trade_num = 0

    def _calc_shares(self, price):
        lot = 1000
        alloc = (self.cash + sum(p.shares * price for p in self.positions.values())) / MAX_STOCKS
        shares = int(alloc / price / lot) * lot
        return max(shares, lot) if shares >= lot else 0

    def _buy(self, ticker, price, date, reason="", stop_loss_pct=0.08):
        shares = self._calc_shares(price)
        if shares <= 0: return
        cost = shares * price
        fee = cost * FEE_RATE
        if cost + fee > self.cash: return
        self.cash -= (cost + fee)
        # Pass stop_loss_pct to Position
        self.positions[ticker] = Position(ticker, price, shares, date, stop_loss_pct)
        self.trade_num += 1
        self.trades.append({'num': self.trade_num, 'ticker': ticker, 'action': 'BUY',
                            'date': date, 'price': round(price, 2), 'shares': shares,
                            'cost': round(cost + fee, 0), 'reason': reason, 'stop_loss': stop_loss_pct})

    def _sell(self, ticker, price, date, reason=""):
        if ticker not in self.positions: return
        pos = self.positions.pop(ticker)
        proceeds = pos.shares * price
        fee = proceeds * FEE_RATE
        tax = proceeds * TAX_RATE
        pnl = proceeds - (pos.shares * pos.entry_price) - fee - tax
        self.cash += (proceeds - fee - tax)
        self.trade_num += 1
        self.trades.append({'num': self.trade_num, 'ticker': ticker, 'action': 'SELL',
                            'date': date, 'price': round(price, 2), 'shares': pos.shares,
                            'pnl': round(pnl, 0), 'pnl_pct': round(pnl / (pos.shares * pos.entry_price) * 100, 2),
                            'holding_days': (date - pos.entry_date).days, 'reason': reason})

    def run(self, price_data):
        """price_data: {ticker: DataFrame with Close column, DatetimeIndex}"""
        all_dates = sorted(set().union(*(df.index for df in price_data.values())))
        raw_rebal = pd.date_range(START_DATE, END_DATE, freq=REBALANCE_FREQ)
        rebal_dates = set(d.date() if hasattr(d, 'date') else d for d in raw_rebal)

        print(f"🚀 CANSLIM Backtest (Technical): {START_DATE} → {END_DATE}")
        print(f"   Capital: ${self.initial_capital:,.0f} | Max stocks: {MAX_STOCKS}")

        # Pre-compute market index return (use 2330 as proxy)
        market_df = price_data.get('2330')

        for date in all_dates:
            d = date.date() if hasattr(date, 'date') else date

            # 1. Stop-loss / take-profit
            for ticker in list(self.positions.keys()):
                if ticker not in price_data: continue
                df = price_data[ticker]
                mask = df.index <= date
                if mask.sum() == 0: continue
                price = df[mask].iloc[-1]['Close']
                signal = self.positions[ticker].check_exit(price)
                if signal:
                    self._sell(ticker, price, date, signal)

            # 2. Quarterly rebalance
            if d in rebal_dates:
                # Calculate market return
                market_ret = 0
                if market_df is not None:
                    m = market_df.index <= date
                    if m.sum() >= 60:
                        mc = market_df[m].iloc[-60]
                        market_ret = (market_df[m].iloc[-1]['Close'] / mc['Close']) - 1

                # Screen stocks using technical CANSLIM
                scores = []
                for t in price_data:
                    score, info = canslim_score(t, price_data[t], date, market_ret)
                    if score > 30:
                        scores.append((t, score, info))
                scores.sort(key=lambda x: x[1], reverse=True)
                selected = [t for t, _, _ in scores[:MAX_STOCKS]]

                if not selected: continue

                # Screen out
                for ticker in list(self.positions.keys()):
                    if ticker not in selected:
                        df = price_data[ticker]
                        mask = df.index <= date
                        if mask.sum() > 0:
                            self._sell(ticker, df[mask].iloc[-1]['Close'], date, "SCREEN_OUT")

                # Screen in
                for t in selected:
                    if t not in self.positions:
                        df = price_data[t]
                        mask = df.index <= date
                        if mask.sum() > 0:
                            # Calculate Chip Score for Dynamic Stop Loss
                            # Re-calculate volume ratio for this bar
                            vol = df[mask]['Volume']
                            vol_avg = vol.rolling(20).mean()
                            if len(vol) > 0 and len(vol_avg) > 0 and vol_avg.iloc[-1] > 0:
                                ratio = vol.iloc[-1] / vol_avg.iloc[-1]
                                if ratio >= 3.0:
                                    sl = 0.10  # Strong Chip
                                elif ratio >= 1.5:
                                    sl = 0.07  # Standard
                                else:
                                    sl = 0.04  # Weak Chip
                            else:
                                sl = 0.08 # Fallback
                            
                            self._buy(t, df[mask].iloc[-1]['Close'], date, f"CANSLIM", stop_loss_pct=sl)

            # 3. Record equity
            port_value = self.cash
            for t, pos in self.positions.items():
                if t in price_data:
                    df = price_data[t]
                    mask = df.index <= date
                    if mask.sum() > 0:
                        port_value += pos.shares * df[mask].iloc[-1]['Close']
            self.equity.append({'date': date, 'equity': port_value})

        self.equity_df = pd.DataFrame(self.equity).set_index('date')
        self.trades_df = pd.DataFrame(self.trades)
        return self._metrics()

    def _metrics(self):
        if len(self.equity_df) < 2: return {}
        eq = self.equity_df['equity']
        total_ret = eq.iloc[-1] / eq.iloc[0] - 1
        years = (eq.index[-1] - eq.index[0]).days / 365.25
        cagr = (1 + total_ret) ** (1/years) - 1 if years > 0 else 0
        daily_ret = eq.pct_change().dropna()
        sharpe = daily_ret.mean() / daily_ret.std() * np.sqrt(252) if daily_ret.std() > 0 else 0
        dd = (eq - eq.cummax()) / eq.cummax()
        max_dd = dd.min()

        sells = pd.DataFrame()
        if len(self.trades_df) > 0 and 'action' in self.trades_df.columns:
            sells = self.trades_df[self.trades_df['action'] == 'SELL']
        wins = sells[sells['pnl'] > 0] if len(sells) > 0 else pd.DataFrame()
        losses = sells[sells['pnl'] <= 0] if len(sells) > 0 else pd.DataFrame()
        wr = len(wins) / len(sells) if len(sells) > 0 else 0
        gp = wins['pnl'].sum() if len(wins) > 0 else 0
        gl = abs(losses['pnl'].sum()) if len(losses) > 0 else 1
        pf = gp / gl

        return {
            'CAGR': cagr, 'Total Return': total_ret, 'Sharpe': sharpe,
            'Max Drawdown': max_dd, 'Win Rate': wr, 'Profit Factor': pf,
            'Trades': len(sells), 'Years': years,
        }

    def report(self):
        m = self._metrics()
        if not m: return
        print("\n" + "=" * 60)
        print("📊 CANSLIM Backtest Results (Technical)")
        print("=" * 60)
        for k in ['CAGR', 'Total Return', 'Max Drawdown']:
            print(f"  {k:<18s} {m[k]:>10.2%}")
        for k in ['Sharpe', 'Profit Factor']:
            print(f"  {k:<18s} {m[k]:>10.2f}")
        wr_val = m.get('Win Rate', 0)
        trades_val = m.get('Trades', 0)
        years_val = m.get('Years', 0)
        print(f"  {'Win Rate':<18s} {wr_val*100:>9.1f}%")
        print(f"  {'Trades':<18s} {trades_val:>10.0f}")
        print(f"  {'Years':<18s} {years_val:>10.0f}")

        print("\n📋 Trades (last 15):")
        print(f"  {'#':>3} {'Ticker':<6} {'Date':<12} {'Action':<5} {'Price':>8} {'Shares':>7} {'PnL':>10} {'Reason':<15}")
        print("  " + "-" * 75)
        for _, t in self.trades_df.tail(15).iterrows():
            pnl_str = f"+{t['pnl']:,.0f}" if t.get('pnl', 0) > 0 else f"{t.get('pnl', 0):,.0f}" if 'pnl' in t else "—"
            print(f"  {t['num']:>3} {t['ticker']:<6} {str(t['date'])[:10]:<12} {t['action']:<5} {t['price']:>8.0f} {t['shares']:>7} {pnl_str:>10} {t.get('reason', ''):<15}")

        print("\n📈 Equity Curve (quarterly):")
        q = self.equity_df.resample('QE').last()
        for d, r in q.iterrows():
            ret = r['equity'] / self.initial_capital - 1
            print(f"  {str(d.date()):<14} ${r['equity']:>12,.0f}  {ret:>+10.2%}")

# ==================== Main ====================
if __name__ == "__main__":
    # 1. Fetch price history
    print(f"📊 Fetching price history ({START_DATE} to {END_DATE})...")
    price_data = {}
    for i, t in enumerate(UNIVERSE):
        try:
            df = yf.Ticker(f"{t}.TW").history(start=START_DATE, end=END_DATE, auto_adjust=False)
            if len(df) > 60:
                price_data[t] = df[['Open', 'High', 'Low', 'Close', 'Volume']]
                if (i + 1) % 20 == 0:
                    print(f"  ... {i+1}/{len(UNIVERSE)}")
        except: pass
    print(f"  ✅ {len(price_data)} stocks loaded")

    if price_data:
        bt = Backtester(1_000_000)
        bt.run(price_data)
        bt.report()
