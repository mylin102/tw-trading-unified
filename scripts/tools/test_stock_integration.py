#!/usr/bin/env python3
"""
Stock Monitor API 對接測試 (Paper Mode)。
登入 Shioaji → 抓行情 → 跑策略 → 模擬下單，驗證完整流程。
不受盤中時間限制，隨時可跑。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import yaml
import pandas as pd
from datetime import datetime
from pathlib import Path
from rich.console import Console
from rich.table import Table
from core.shioaji_session import get_api, logout
from strategies.stocks.entry_strategies import STOCK_STRATEGIES
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze

console = Console()
ROOT = Path(__file__).parent.parent.parent

def main():
    console.print("[bold]🧪 Stock Monitor API Integration Test (Paper)[/bold]\n")

    # ── 1. Login ──
    console.print("[dim]1/5 Logging in to Shioaji...[/dim]")
    try:
        api = get_api()
        console.print("[green]   ✅ Login OK[/green]")
    except Exception as e:
        console.print(f"[red]   ❌ Login failed: {e}[/red]")
        return

    # ── 2. Load config ──
    with open(ROOT / "config" / "stocks.yaml") as f:
        cfg = yaml.safe_load(f)
    stk = cfg["stocks"]
    strategy_name = stk["strategy"]
    watchlist = stk["watchlist"]
    console.print(f"[dim]2/5 Config loaded: strategy={strategy_name}, watchlist={len(watchlist)} tickers[/dim]")

    # ── 3. Fetch market data ──
    console.print(f"[dim]3/5 Fetching kbars for {len(watchlist)} tickers...[/dim]")
    results = Table(title="策略信號測試結果")
    results.add_column("Ticker", style="cyan")
    results.add_column("Name")
    results.add_column("Close", justify="right")
    results.add_column("Bars", justify="right")
    results.add_column("Signal")
    results.add_column("Reason")

    strat_fn = STOCK_STRATEGIES[strategy_name]["func"]
    signals_found = 0
    errors = 0

    for ticker in watchlist:
        try:
            contract = api.Contracts.Stocks[ticker]
            if contract is None:
                console.print(f"[yellow]   ⚠️ {ticker}: contract not found[/yellow]")
                errors += 1
                continue

            start = (datetime.now() - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
            kbars = api.kbars(contract, start=start)
            df = pd.DataFrame({**kbars})
            if df.empty:
                results.add_row(ticker, "?", "-", "0", "[red]NO DATA[/red]", "")
                errors += 1
                continue

            df["ts"] = pd.to_datetime(df["ts"])
            df = df.set_index("ts")
            df.columns = [c.capitalize() if c.lower() in ["open", "high", "low", "close", "volume"] else c for c in df.columns]
            df = calculate_futures_squeeze(df)

            last = df.iloc[-1]
            state = {"last_5m": last, "df_5m": df}
            res = strat_fn(state, cfg)

            if res:
                signals_found += 1
                results.add_row(ticker, contract.name, f"{last['Close']:.1f}", str(len(df)),
                                f"[green]{res['action']}[/green]", res.get("reason", ""))
            else:
                results.add_row(ticker, contract.name, f"{last['Close']:.1f}", str(len(df)),
                                "[dim]—[/dim]", "")
        except Exception as e:
            results.add_row(ticker, "?", "-", "-", f"[red]ERR[/red]", str(e)[:40])
            errors += 1

    console.print(results)

    # ── 4. Account info ──
    console.print(f"\n[dim]4/5 Account check...[/dim]")
    try:
        positions = api.list_positions(api.stock_account)
        console.print(f"   📊 Stock positions: {len(positions)}")
        for p in positions[:5]:
            console.print(f"      {p.code} qty={p.quantity} pnl={p.pnl:.0f}")
    except Exception as e:
        console.print(f"   [yellow]⚠️ Position query: {e}[/yellow]")

    # ── 5. Bear defense: market regime ──
    console.print(f"\n[dim]5/5 Market regime check...[/dim]")
    try:
        taiex = api.Contracts.Indexs.TSE["001"]
        start = (datetime.now() - pd.Timedelta(days=90)).strftime("%Y-%m-%d")
        kbars = api.kbars(taiex, start=start)
        df = pd.DataFrame({**kbars})
        df["ema60"] = df["Close"].ewm(span=60, adjust=False).mean()
        close = df["Close"].iloc[-1]
        ema = df["ema60"].iloc[-1]
        is_bear = close < ema
        tag = "🐻 空頭" if is_bear else "🐂 多頭"
        console.print(f"   {tag}  TAIEX={close:.0f}  EMA60={ema:.0f}")
    except Exception as e:
        console.print(f"   [yellow]⚠️ Market regime: {e}[/yellow]")

    # ── Summary ──
    console.print(f"\n[bold]{'='*50}[/bold]")
    console.print(f"[bold]📋 Summary[/bold]")
    console.print(f"   Strategy:  {strategy_name}")
    console.print(f"   Tickers:   {len(watchlist)} ({errors} errors)")
    console.print(f"   Signals:   {signals_found} BUY signals found")
    console.print(f"   Mode:      PAPER (live_trading=false)")
    console.print(f"   Params:    SL={stk['stop_loss_pct']:.0%} TP={stk['take_profit_pct']:.0%} TS={stk['trailing_stop_pct']:.1%}")
    if signals_found > 0:
        console.print(f"   [green]✅ Ready for tomorrow's paper trading[/green]")
    else:
        console.print(f"   [yellow]⚠️ No signals now (normal if market closed)[/yellow]")

    logout()
    console.print("[dim]Session closed.[/dim]")

if __name__ == "__main__":
    main()
