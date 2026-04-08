"""
Phase 3: Backtest Scout Strategy with parameter sweep.
Scout: SCOUT (10 lots trial) → SCALE (if profit > 1% and mom_state==3)
Uses full two-stage lifecycle (stock_engine.py supports this via positions 1→2).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta
import yaml
from itertools import product

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from strategies.options.options_engine.engine.indicators import calculate_stock_squeeze
from strategies.stocks.entry_strategies import STOCK_STRATEGIES

DATA_DIR = ROOT / "data" / "taifex_raw"
CONFIG_PATH = ROOT / "config" / "stocks.yaml"


def load_and_prepare(ticker):
    file_path = DATA_DIR / f"STOCK_{ticker}_5m.csv"
    if not file_path.exists():
        return None
    df = pd.read_csv(file_path)
    if df.empty or len(df) < 50:
        return None

    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if cl in ("ts", "timestamp", "date"):
            col_map[c] = "timestamp"
        elif cl in ("open", "high", "low", "close", "volume"):
            col_map[c] = c.capitalize()
    df = df.rename(columns=col_map)

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp").sort_index()

    df = calculate_stock_squeeze(df)

    for col in ["macd_hist", "k_val", "d_val", "adx", "bb_lower", "bb_mid", "bb_upper",
                "momentum", "mom_state", "vwap", "ema_fast", "ema_slow", "ema_macro"]:
        if col in df.columns:
            df[col] = df[col].fillna(0)
    for col in ["macd_rising", "sqz_on", "fired", "bullish_align"]:
        if col in df.columns:
            df[col] = df[col].fillna(False)

    return df


def scout_lifecycle(close, high, low, trading_day, long_sig,
                     initial_balance, capital_per_trade, sl, tp, ts_pct):
    """
    Pure numpy scout lifecycle: SCOUT (10 lots) → SCALE (to capital_per_trade) → EXIT
    Position states: 0=flat, 1=scout(10 lots), 2=scaled(full)
    """
    n = len(close)
    if n == 0 or np.sum(long_sig) == 0:
        return None

    position = 0  # 0=flat, 1=scout, 2=scaled
    qty_odd = 0
    qty_round = 0
    entry_price_avg = 0.0
    entry_day_odd = 0
    entry_day_round = 0
    max_price = 0.0
    entry_buy_cost = 0.0

    total_pnl = 0.0
    n_trades = 0
    n_wins = 0
    gross_profit = 0.0
    gross_loss = 0.0
    max_dd = 0.0
    cumulative_pnl = 0.0

    for i in range(n):
        curr_day = trading_day[i]

        if long_sig[i]:
            if position == 0:
                # SCOUT: buy 10 lots
                qty_odd = 10
                position = 1
                entry_price_avg = close[i]
                entry_day_odd = curr_day
                max_price = close[i]
                amount = close[i] * qty_odd
                entry_buy_cost = max(20.0, amount * 0.0005)

            elif position == 1:
                # SCALE: add round lots
                qty_round = int(capital_per_trade // close[i])
                if qty_round > 0:
                    position = 2
                    entry_day_round = curr_day
                    total_qty = qty_odd + qty_round
                    entry_price_avg = (entry_price_avg * qty_odd + close[i] * qty_round) / total_qty
                    entry_buy_cost += max(20.0, close[i] * qty_round * 0.0005)

        elif position > 0:
            exit_price = 0.0
            if high[i] > max_price:
                max_price = high[i]

            can_sell_odd = (curr_day != entry_day_odd)
            can_sell_round = True  # Round lots can sell same day

            if position == 1 and can_sell_odd:
                trailing = max_price * (1 - ts_pct)
                hard_stop = entry_price_avg * (1 - sl)
                tp_price = entry_price_avg * (1 + tp)

                if low[i] <= trailing or low[i] <= hard_stop:
                    exit_price = max(trailing, hard_stop) if low[i] <= trailing and low[i] <= hard_stop else \
                                 (trailing if low[i] <= trailing else hard_stop)
                elif high[i] >= tp_price:
                    exit_price = tp_price
            elif position == 2:
                trailing = max_price * (1 - ts_pct)
                hard_stop = entry_price_avg * (1 - sl)
                tp_price = entry_price_avg * (1 + tp)

                if low[i] <= trailing or low[i] <= hard_stop:
                    exit_price = max(trailing, hard_stop) if low[i] <= trailing and low[i] <= hard_stop else \
                                 (trailing if low[i] <= trailing else hard_stop)
                elif high[i] >= tp_price:
                    exit_price = tp_price

            if i == n - 1 and exit_price == 0.0:
                exit_price = close[i]

            if exit_price > 0:
                total_qty = qty_odd + qty_round
                trade_pnl = (exit_price - entry_price_avg) * total_qty
                sell_cost = max(20.0, exit_price * total_qty * 0.0035)
                net_pnl = trade_pnl - entry_buy_cost - sell_cost

                total_pnl += net_pnl
                cumulative_pnl += net_pnl
                if net_pnl > 0:
                    n_wins += 1
                    gross_profit += net_pnl
                else:
                    gross_loss += abs(net_pnl)
                n_trades += 1

                if cumulative_pnl < max_dd:
                    max_dd = cumulative_pnl

                position = 0
                qty_odd = 0
                qty_round = 0
                entry_price_avg = 0.0
                entry_buy_cost = 0.0

    if n_trades == 0:
        return None

    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    wr = n_wins / n_trades * 100

    return {
        "total_pnl": round(total_pnl, 0),
        "win_rate": round(wr, 1),
        "total_trades": n_trades,
        "max_drawdown": round(max_dd, 0),
        "profit_factor": round(pf, 2),
    }


def gen_scout_signals(df):
    """Generate long signals for scout strategy."""
    n = len(df)
    long_sig = np.zeros(n, dtype=np.bool_)

    for i in range(20, n):
        state = {
            "last_5m": df.iloc[i],
            "df_5m": df.iloc[:i + 1],
            "scout_stage": "IDLE",
            "scout_entry_price": 0.0,
            "market_trend": "BULL",
            "is_bear_market": False,
        }
        res_sig = STOCK_STRATEGIES["scout_strategy"]["func"](state, {})
        if res_sig and res_sig["action"] == "BUY":
            long_sig[i] = True

    return long_sig


def main():
    cfg = yaml.safe_load(open(CONFIG_PATH))
    stk_cfg = cfg.get("stocks", {})
    watchlist = stk_cfg.get("watchlist", [])

    # ══════════════════════════════════════════════
    # PRE-COMPUTE
    # ══════════════════════════════════════════════
    print("Loading indicators...")
    ticker_data = {}
    for ticker in watchlist:
        df = load_and_prepare(ticker)
        if df is not None:
            close = df["Close"].values.astype(np.float64)
            high = df["High"].values.astype(np.float64)
            low = df["Low"].values.astype(np.float64)

            day_codes = {}
            day_counter = 0
            trading_day = np.zeros(len(df), dtype=np.int64)
            for i, ts in enumerate(df.index):
                d = ts.date() if hasattr(ts, 'date') else pd.Timestamp(ts).date()
                if d not in day_codes:
                    day_codes[d] = day_counter
                    day_counter += 1
                trading_day[i] = day_codes[d]

            long_sig = gen_scout_signals(df)

            ticker_data[ticker] = {
                "close": close, "high": high, "low": low,
                "trading_day": trading_day, "long_sig": long_sig,
                "data_len": len(df),
            }
    print(f"  Loaded {len(ticker_data)}/{len(watchlist)} tickers")
    total_signals = sum(d["long_sig"].sum() for d in ticker_data.values())
    print(f"  Total scout signals: {total_signals}")

    # ══════════════════════════════════════════════
    # PARAMETER SWEEP
    # ══════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("Scout Strategy Parameter Sweep")
    print("=" * 80)

    sl_range = [0.01, 0.015, 0.02, 0.03]
    tp_range = [0.01, 0.02, 0.05, 0.10]
    ts_range = [0.005, 0.01, 0.015, 0.02]

    sweep_results = []
    combo_count = len(sl_range) * len(tp_range) * len(ts_range)
    for idx, (sl, tp, ts) in enumerate(product(sl_range, tp_range, ts_range), 1):
        total_pnl = 0
        total_trades = 0
        total_dd = 0.0
        count = 0

        for ticker, data in ticker_data.items():
            res = scout_lifecycle(
                data["close"], data["high"], data["low"], data["trading_day"],
                data["long_sig"], 100000.0, 20000.0, sl, tp, ts,
            )
            if res and res["total_trades"] > 0:
                total_pnl += res["total_pnl"]
                total_trades += res["total_trades"]
                total_dd += res["max_drawdown"]
                count += 1

        avg_dd = total_dd / count if count > 0 else 0
        sweep_results.append({
            "sl_pct": f"{sl*100:.1f}%",
            "tp_pct": f"{tp*100:.1f}%",
            "ts_pct": f"{ts*100:.2f}%",
            "total_pnl": round(total_pnl, 0),
            "total_trades": total_trades,
            "avg_pnl_per_trade": round(total_pnl / max(1, total_trades), 0),
            "avg_max_dd": round(avg_dd, 0),
        })

        if idx % 16 == 0:
            print(f"  [{idx}/{combo_count}] done...")

    df_sweep = pd.DataFrame(sweep_results).sort_values("total_pnl", ascending=False)
    print("\nTop 10 Parameter Combinations:")
    print(df_sweep.head(10).to_string(index=False))

    best = df_sweep.iloc[0] if not df_sweep.empty else None
    if best is not None:
        best_sl = float(best["sl_pct"].replace("%", "")) / 100
        best_tp = float(best["tp_pct"].replace("%", "")) / 100
        best_ts = float(best["ts_pct"].replace("%", "")) / 100
        print(f"\n🏆 Best Scout: SL={best['sl_pct']} TP={best['tp_pct']} TS={best['ts_pct']} "
              f"PnL={best['total_pnl']:+.0f} T={best['total_trades']} DD={best['avg_max_dd']:.0f}")

    # Per-ticker breakdown with best params
    print("\n" + "=" * 80)
    print("Per-Ticker Breakdown (Best Params)")
    print("=" * 80)
    for ticker, data in sorted(ticker_data.items()):
        res = scout_lifecycle(
            data["close"], data["high"], data["low"], data["trading_day"],
            data["long_sig"], 100000.0, 20000.0, best_sl, best_tp, best_ts,
        )
        if res and res["total_trades"] > 0:
            print(f"  {ticker}: PnL={res['total_pnl']:+.0f} PF={res['profit_factor']:.2f} "
                  f"WR={res['win_rate']:.0f}% T={res['total_trades']} DD={res['max_drawdown']:.0f}")
        else:
            print(f"  {ticker}: NO TRADES")

    # Save
    out_path = ROOT / "exports" / "scout_backtest.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df_sweep.to_csv(out_path, index=False)
    print(f"\n📊 Results saved to {out_path}")


if __name__ == "__main__":
    main()
