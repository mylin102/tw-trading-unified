#!/usr/bin/env python3
"""Check actual Shioaji stock positions — read-only, no trading."""
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(override=True)

import shioaji as sj
from rich.console import Console

console = Console()

def check_positions():
    """Connect to Shioaji and print all current stock positions."""
    api_key = os.getenv("SHIOAJI_API_KEY") or os.getenv("SHIOAJI_PERSON_ID")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY") or os.getenv("SHIOAJI_PASSWD")
    person_id = os.getenv("SHIOAJI_PERSON_ID", api_key)

    if not api_key or not secret_key:
        console.print("[red]❌ SHIOAJI credentials not set[/red]")
        return

    api = sj.Shioaji()
    try:
        api.login(api_key=api_key, secret_key=secret_key, contracts_timeout=10000)
    except Exception as e:
        console.print(f"[red]❌ Login failed: {e}[/red]")
        return

    console.print(f"[green]✓ Logged in as {person_id}[/green]")

    # Check stock positions
    console.print("\n[bold red]=== STOCK POSITIONS (持倉) ===[/bold red]")
    try:
        stock_contracts = api.Contracts.Stocks
        positions = []

        for contract in stock_contracts:
            try:
                snap = api.snapshots([contract])
                if snap and len(snap) > 0:
                    s = snap[0]
                    # Check if we hold shares: odd_lot_shares > 0
                    odd_shares = getattr(s, 'odd_lot_shares', 0) or 0
                    if odd_shares > 0:
                        positions.append({
                            'ticker': contract.code,
                            'name': getattr(contract, 'name', ''),
                            'qty': odd_shares,
                            'price': s.close or 0,
                        })
            except Exception:
                continue

        if not positions:
            console.print("[yellow]  ⚠️ No odd-lot stock positions found[/yellow]")
            console.print("[dim]  (Note: This only checks odd-lot positions. Use Shioaji app for full account view.)[/dim]")
        else:
            console.print(f"{'Ticker':<8} {'Name':<15} {'Qty':>6} {'Price':>8} {'Value':>10}")
            console.print("-" * 50)
            total = 0
            for p in positions:
                val = p['qty'] * p['price']
                total += val
                console.print(f"{p['ticker']:<8} {p['name']:<15} {p['qty']:>6} {p['price']:>8.2f} {val:>10,.0f}")
            console.print(f"{'TOTAL':<8} {'':<15} {'':>6} {'':>8} {total:>10,.0f}")
            console.print(f"\n[bold yellow]⚠️ Settlement T+2 = 4/15 (Wed) — need ~${total:,.0f}[/bold yellow]")

    except Exception as e:
        console.print(f"[yellow]⚠️ Could not query stock positions: {e}[/yellow]")
        console.print("[dim]  Please check your Shioaji app directly for positions.[/dim]")

    # Check pending stock orders
    console.print("\n[bold]=== PENDING ORDERS (委託單) ===[/bold]")
    try:
        api.update_status()
        trades = api.trades
        pending = []
        for t in trades:
            try:
                code = t.contract.code if hasattr(t, 'contract') and hasattr(t.contract, 'code') else '?'
                status = t.status.status if hasattr(t, 'status') else '?'
                if status == sj.constant.Status.Submitted:
                    pending.append(t)
            except Exception:
                continue

        if pending:
            for t in pending:
                code = t.contract.code
                action = t.action
                qty = t.quantity
                price = t.price
                console.print(f"[yellow]  ⏳ {code} {action} {qty} @ {price}[/yellow]")
        else:
            console.print("[green]  ✓ No pending orders[/green]")

    except Exception as e:
        console.print(f"[yellow]⚠️ Could not query pending orders: {e}[/yellow]")

    console.print("\n[bold red]⚠️  IMPORTANT: This only checks odd-lot data.[/bold red]")
    console.print("[bold red]   The 4 positions from today's bug may not show up here.[/bold red]")
    console.print("[bold red]   Please verify in the Shioaji mobile app directly.[/bold red]")

if __name__ == "__main__":
    check_positions()
