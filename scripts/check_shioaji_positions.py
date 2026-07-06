#!/usr/bin/env python3
"""Check actual Shioaji stock + futures positions — read-only, no trading.

Usage: python3 scripts/check_shioaji_positions.py

Uses cross-validated position query (Unit.Common + Unit.Share) for completeness.
"""
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(override=True)

import shioaji as sj
from shioaji.constant import Unit
from rich.console import Console
from collections import defaultdict

console = Console()


def get_positions_reconciled(api):
    """Return {code: total_shares} with cross-validation between Common and Share."""
    common = api.list_positions(account=api.stock_account)
    share = api.list_positions(account=api.stock_account, unit=Unit.Share)
    share_by_code = {p.code: p for p in share}

    result = {}
    warnings = []

    for c in common:
        code = c.code
        expected_min = c.quantity * 1000  # board lot minimum
        actual = share_by_code.get(code)
        actual_qty = actual.quantity if actual else 0

        if actual_qty < expected_min:
            warnings.append(
                f"[WARN] {code}: Common={c.quantity}张 >= {expected_min} > Share={actual_qty}股"
                f" — possible sync delay or unsettled trade"
            )

        # Use Share qty if available, else fallback to Common × 1000
        result[code] = actual_qty if actual_qty >= expected_min else (c.quantity * 1000)

    # Share-only positions (pure odd lots, not in Common)
    for p in share:
        if p.code not in result:
            common_match = next((c for c in common if c.code == p.code), None)
            if common_match:
                # Already handled above but quantity=0 in Common
                result[p.code] = p.quantity if p.quantity > 0 else (common_match.quantity * 1000)
            elif p.quantity > 0:
                result[p.code] = p.quantity

    return result, warnings


def check_positions():
    api_key = os.getenv("SHIOAJI_API_KEY") or os.getenv("SHIOAJI_PERSON_ID")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY") or os.getenv("SHIOAJI_PASSWD")

    if not api_key or not secret_key:
        console.print("[red]❌ SHIOAJI credentials not set[/red]")
        return

    api = sj.Shioaji()
    try:
        api.login(api_key=api_key, secret_key=secret_key, contracts_timeout=10000)
    except Exception as e:
        console.print(f"[red]❌ Login failed: {e}[/red]")
        return

    console.print("[green]✓ Logged in[/green]\n")

    # ── Stock positions (cross-validated) ──
    console.print("[bold]=== STOCK POSITIONS (完整庫存) ===[/bold]")
    try:
        reconciled, warnings = get_positions_reconciled(api)

        if warnings:
            console.print("\n[yellow]⚠️  Cross-validation warnings:[/yellow]")
            for w in warnings:
                console.print(f"  [yellow]{w}[/yellow]")
            console.print()

        # Show unified view
        share_positions = api.list_positions(account=api.stock_account, unit=Unit.Share)
        share_by_code = {p.code: p for p in share_positions}

        if not share_by_code:
            console.print("  [yellow]⚠️ No positions[/yellow]")
        else:
            console.print(
                f"{'Code':<8} {'Qty(股)':>8} {'Cost':>10} {'Last':>10} {'PnL(API)':>10} {'Value':>12}"
            )
            console.print("-" * 60)
            total_val = 0
            total_pnl = 0
            for code in sorted(reconciled.keys()):
                p = share_by_code.get(code)
                if p:
                    val = p.quantity * p.last_price
                    total_val += val
                    total_pnl += p.pnl
                    console.print(
                        f"{p.code:<8} {p.quantity:>8} "
                        f"{p.price:>10.2f} {p.last_price:>10.2f} "
                        f"{p.pnl:>10,.0f} {val:>12,.0f}"
                    )
            console.print("-" * 60)
            console.print(
                f"{'TOTAL':<8} {'':>8} {'':>10} {'':>10} "
                f"{total_pnl:>10,.0f} {total_val:>12,.0f}"
            )

    except Exception as e:
        console.print(f"[red]✗ Stock positions error: {e}[/red]")

    # ── Futures positions ──
    console.print("\n[bold]=== FUTURES POSITIONS ===[/bold]")
    try:
        futs = api.list_positions(account=api.futopt_account)
        if not futs:
            console.print("  [green]✓ No futures positions[/green]")
        else:
            console.print(f"{'Code':<10} {'Qty':>4} {'Price':>10} {'PnL':>12}")
            console.print("-" * 38)
            for p in futs:
                console.print(f"{p.code:<10} {p.quantity:>4} {p.price:>10.2f} {p.pnl:>12,.0f}")
    except Exception as e:
        console.print(f"[yellow]⚠️ Futures error: {e}[/yellow]")

    # ── Pending orders ──
    console.print("\n[bold]=== PENDING ORDERS ===[/bold]")
    try:
        api.update_status()
        trades = api.trades
        pending = [t for t in trades if t.status.status == sj.constant.Status.Submitted]
        if pending:
            for t in pending:
                code = (
                    t.contract.code
                    if hasattr(t, "contract") and hasattr(t.contract, "code")
                    else "?"
                )
                console.print(f"  [yellow]⏳ {code} {t.action} {t.quantity} @ {t.price}[/yellow]")
        else:
            console.print("  [green]✓ None[/green]")
    except Exception as e:
        console.print(f"[yellow]⚠️ Pending orders error: {e}[/yellow]")

    api.logout()


if __name__ == "__main__":
    check_positions()
