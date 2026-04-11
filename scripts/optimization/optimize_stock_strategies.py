"""
Phase 2.5: Optimize mean_reversion + test fakeout_reversal.
Pure numpy backtest (no numba) to avoid cache issues.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta
import yaml
from itertools import product

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from strategies.options.options_engine.engine.indicators import calculate_stock_squeeze

DATA_DIR = ROOT / "data" / "taifex_raw"
CONFIG_PATH = ROOT / "config" / "stocks.yaml"


def load_and_prepare(ticker):
    """Load CSV, calculate indicators."""
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


def run_backtest_numpy(close, high, low, trading_day, long_sig, short_sig,
                        initial_balance, capital_per_trade, sl, tp, ts_pct):
    """Pure numpy backtest. Returns (total_pnl, win_rate, n_trades, max_dd, pf)."""
    n = len(close)
    if n == 0 or np.sum(long_sig) == 0:
        return None

    position = 0
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
    cumulative_pnl = 0.0
    max_dd = 0.0
    gross_profit = 0.0
    gross_loss = 0.0

    for i in range(n):
        curr_day = trading_day[i]

        if long_sig[i] and position == 0:
            qty_odd = 10
            position = 1
            entry_price_avg = close[i]
            entry_day_odd = curr_day
            max_price = close[i]
            amount = close[i] * qty_odd
            entry_buy_cost = max(20.0, amount * 0.0005)

        elif position > 0:
            exit_price = 0.0

            if high[i] > max_price:
                max_price = high[i]

            can_sell_odd = (curr_day != entry_day_odd)

            if can_sell_odd:
                trailing_price = max_price * (1 - ts_pct)
                hard_stop_price = entry_price_avg * (1 - sl)

                if low[i] <= trailing_price or low[i] <= hard_stop_price:
                    if low[i] <= trailing_price and low[i] <= hard_stop_price:
                        if trailing_price >= hard_stop_price:
                            exit_price = trailing_price
                        else:
                            exit_price = hard_stop_price
                    elif low[i] <= trailing_price:
                        exit_price = trailing_price
                    else:
                        exit_price = hard_stop_price
                elif high[i] >= entry_price_avg * (1 + tp):
                    exit_price = entry_price_avg * (1 + tp)
                elif short_sig[i]:
                    exit_price = close[i]

            if i == n - 1 and exit_price == 0.0:
                exit_price = close[i]

            if exit_price > 0:
                total_qty = qty_odd
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


def gen_mean_reversion_signals(close, bb_lower):
    """Generate long signals: Close < BB lower."""
    n = len(close)
    long_sig = np.zeros(n, dtype=np.bool_)
    for i in range(n):
        if bb_lower[i] > 0 and close[i] < bb_lower[i]:
            long_sig[i] = True
    return long_sig


def gen_fakeout_signals(df):
    """Squeeze fire failure counter-trade."""
    n = len(df)
    long_sig = np.zeros(n, dtype=np.bool_)
    for i in range(5, n):
        last = df.iloc[i]
        prev = df.iloc[i - 1]
        vwap = last.get("vwap", last["Close"])
        close = last["Close"]
        momentum = last.get("momentum", 0)
        prev_momentum = prev.get("momentum", 0)
        fired = last.get("fired", False)
        was_squeezing = prev.get("sqz_on", False)
        is_release = fired and was_squeezing

        if not is_release:
            continue

        if momentum > 0 and prev_momentum <= 0 and close < vwap:
            long_sig[i] = True
        elif momentum < 0 and prev_momentum >= 0 and close > vwap:
            long_sig[i] = True

    return long_sig


def prepare_arrays(df):
    """Extract numpy arrays from DataFrame for backtest."""
    n = len(df)
    close = df["Close"].values.astype(np.float64)
    high = df["High"].values.astype(np.float64)
    low = df["Low"].values.astype(np.float64)
    bb_lower = df["bb_lower"].values.astype(np.float64) if "bb_lower" in df.columns else np.zeros(n)

    day_codes = {}
    day_counter = 0
    trading_day = np.zeros(n, dtype=np.int64)
    for i, ts in enumerate(df.index):
        d = ts.date() if hasattr(ts, 'date') else pd.Timestamp(ts).date()
        if d not in day_codes:
            day_codes[d] = day_counter
            day_counter += 1
        trading_day[i] = day_codes[d]

    return close, high, low, trading_day, bb_lower


def main():
    cfg = yaml.safe_load(open(CONFIG_PATH))
    stk_cfg = cfg.get("stocks", {})
    watchlist = stk_cfg.get("watchlist", [])

    # ══════════════════════════════════════════════
    # PRE-COMPUTE
    # ══════════════════════════════════════════════
    print("Loading and computing indicators...")
    ticker_data = {}
    for ticker in watchlist:
        df = load_and_prepare(ticker)
        if df is not None:
            close, high, low, trading_day, bb_lower = prepare_arrays(df)
            ls_mr = gen_mean_reversion_signals(close, bb_lower)
            ls_fo = gen_fakeout_signals(df)
            ticker_data[ticker] = {
                "df": df, "close": close, "high": high, "low": low,
                "trading_day": trading_day,
                "mean_reversion": ls_mr,
                "fakeout": ls_fo,
            }
    print(f"  Loaded {len(ticker_data)}/{len(watchlist)} tickers")

    # ══════════════════════════════════════════════
    # PART 1: Sweep mean_reversion parameters
    # ══════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("PART 1: mean_reversion Parameter Sweep (64 combos)")
    print("=" * 80)

    sl_range = [0.015, 0.02, 0.03, 0.05]
    tp_range = [0.05, 0.10, 0.15, 0.20]
    ts_range = [0.005, 0.01, 0.015, 0.02]

    sweep_results = []
    combo_count = len(sl_range) * len(tp_range) * len(ts_range)
    for idx, (sl, tp, ts) in enumerate(product(sl_range, tp_range, ts_range), 1):
        total_pnl = 0
        total_trades = 0
        total_dd = 0.0
        count = 0

        for ticker, data in ticker_data.items():
            ls = data["mean_reversion"]
            short_sig = np.zeros(len(ls), dtype=np.bool_)
            res = run_backtest_numpy(
                data["close"], data["high"], data["low"], data["trading_day"],
                ls, short_sig, 100000.0, 20000.0, sl, tp, ts,
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
    best_sl = float(best["sl_pct"].replace("%", "")) / 100 if best is not None else 0.03
    best_tp = float(best["tp_pct"].replace("%", "")) / 100 if best is not None else 0.10
    best_ts = float(best["ts_pct"].replace("%", "")) / 100 if best is not None else 0.015

    if best is not None:
        print(f"\n🏆 Best: SL={best['sl_pct']} TP={best['tp_pct']} TS={best['ts_pct']} "
              f"PnL={best['total_pnl']:+.0f} T={best['total_trades']}")

    # ══════════════════════════════════════════════
    # PART 2: fakeout_reversal on all tickers
    # ══════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("PART 2: fakeout_reversal Strategy (all tickers)")
    print("=" * 80)

    fakeout_results = []
    for ticker, data in ticker_data.items():
        ls = data["fakeout"]
        short_sig = np.zeros(len(ls), dtype=np.bool_)
        res = run_backtest_numpy(
            data["close"], data["high"], data["low"], data["trading_day"],
            ls, short_sig, 100000.0, 20000.0, best_sl, best_tp, best_ts,
        )
        if res and res["total_trades"] > 0:
            fakeout_results.append({"ticker": ticker, **res})
            print(f"  {ticker}: PnL={res['total_pnl']:+.0f} PF={res['profit_factor']:.2f} "
                  f"WR={res['win_rate']:.0f}% T={res['total_trades']} DD={res['max_drawdown']:.0f}")
        elif res:
            print(f"  {ticker}: NO TRADES")

    df_fakeout = pd.DataFrame(fakeout_results)
    if not df_fakeout.empty:
        total = df_fakeout["total_pnl"].sum()
        avg_pf = df_fakeout["profit_factor"].mean()
        avg_wr = df_fakeout["win_rate"].mean()
        total_t = df_fakeout["total_trades"].sum()
        print(f"\n📊 fakeout_reversal TOTAL: PnL={total:+.0f} PF={avg_pf:.2f} WR={avg_wr:.0f}% T={total_t}")
        print("\nPer-ticker results:")
        print(df_fakeout.sort_values("total_pnl", ascending=False).to_string(index=False))

    # ══════════════════════════════════════════════
    # PART 3: All strategies with best params
    # ══════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("PART 3: All Strategies with Optimized Params")
    print(f"  SL={best_sl*100:.1f}% TP={best_tp*100:.0f}% TS={best_ts*100:.2f}%")
    print("=" * 80)

    all_results = []
    for strat_name in ["mean_reversion", "fakeout_reversal"]:
        total_pnl = 0
        total_trades = 0
        pf_sum = 0
        dd_sum = 0
        wr_sum = 0
        count = 0

        for ticker, data in ticker_data.items():
            if strat_name == "mean_reversion":
                ls = data["mean_reversion"]
            else:
                ls = data["fakeout"]

            short_sig = np.zeros(len(ls), dtype=np.bool_)
            res = run_backtest_numpy(
                data["close"], data["high"], data["low"], data["trading_day"],
                ls, short_sig, 100000.0, 20000.0, best_sl, best_tp, best_ts,
            )
            if res and res["total_trades"] > 0:
                total_pnl += res["total_pnl"]
                total_trades += res["total_trades"]
                pf_sum += res["profit_factor"]
                dd_sum += res["max_drawdown"]
                wr_sum += res["win_rate"]
                count += 1

        if count > 0:
            all_results.append({
                "strategy": strat_name,
                "total_pnl": round(total_pnl, 0),
                "total_trades": total_trades,
                "avg_pf": round(pf_sum / count, 2),
                "avg_wr": round(wr_sum / count, 1),
                "avg_dd": round(dd_sum / count, 0),
            })

    df_all = pd.DataFrame(all_results).sort_values("total_pnl", ascending=False)
    print(df_all.to_string(index=False))

    # Save
    out_path = ROOT / "exports" / "stock_optimization_results.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df_all.to_csv(out_path, index=False)
    print(f"\n📊 Results saved to {out_path}")


if __name__ == "__main__":
    main()
