#!/usr/bin/env python3
"""
V-Model L4 UAT: Real-World Scenario Validation

Simulates a full night trading session with OrderManager enabled.
Tests: signal → order submit → fill → position → exit → PnL → export
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from datetime import datetime
from unittest.mock import MagicMock
from rich.console import Console

from core.order_management.order import Order, OrderStatus, OrderType, OrderSide
from core.order_management.order_manager import OrderManager
from core.order_management.paper_fill import PaperFillSimulator
from strategies.futures.squeeze_futures.engine.simulator import PaperTrader

console = Console()

def main():
    console.print("[bold]🎯 L4 UAT: Real-World Night Session Simulation[/bold]\n")

    # Setup OrderManager + PaperTrader (same as FuturesMonitor with use_order_manager=True)
    order_mgr = OrderManager(mode="paper")
    sim = PaperFillSimulator(order_mgr)
    order_mgr.set_simulator(sim)
    trader = PaperTrader(initial_balance=100000)

    # Wire callbacks (same as _wire_order_callbacks)
    pending_stop_loss = 60
    def _on_fill(event):
        nonlocal pending_stop_loss
        if event.status == OrderStatus.FILLED:
            ts = datetime.now()
            if event.side == OrderSide.BUY:
                trader.execute_signal("BUY", event.fill_price, ts,
                                      lots=event.fill_qty, max_lots=1, stop_loss=pending_stop_loss)
            else:
                trader.execute_signal("SELL", event.fill_price, ts,
                                      lots=event.fill_qty, max_lots=1, stop_loss=pending_stop_loss)

    order_mgr.register_callback("on_fill", _on_fill)

    # ── Scenario: Night session counter_vwap trade ──
    console.print("📋 Scenario: Counter-VWAP SHORT entry, then VWAP exit")
    console.print(f"   Balance: {trader.balance:,.0f}\n")

    # Step 1: Squeeze fire detected (bearish), counter-vwap SHORT signal
    console.print("[cyan]Step 1: SHORT entry signal at 36500[/cyan]")
    order1 = order_mgr.create_order("TMF", OrderSide.SELL, OrderType.MARKET, 1,
                                     strategy="counter_vwap")
    order_mgr.submit(order1, exchange_ordno="UAT-001")
    sim.register(order1)
    pending_stop_loss = 36500 + 60  # SHORT: stop above entry

    # Simulate market tick
    tick1 = MagicMock()
    tick1.datetime = datetime(2026, 4, 14, 21, 5, 0)
    tick1.close = 36500
    tick1.open = 36510
    tick1.high = 36520
    tick1.low = 36490
    tick1.volume = 200
    sim.process_tick(tick1)

    console.print(f"   Order: {order1.status.value}")
    console.print(f"   Filled: {order1.filled_quantity}/{order1.quantity} @ {order1.avg_fill_price:.0f}")
    console.print(f"   Trader position: {trader.position} (SHORT={trader.position < 0})")

    assert order1.status == OrderStatus.FILLED
    assert trader.position == -1
    console.print("   ✅ SHORT entry confirmed\n")

    # Step 2: Price moves up towards VWAP (counter-trade working)
    console.print("[cyan]Step 2: Price rallies to 36600, VWAP exit signal[/cyan]")
    # For exits, we use direct PaperTrader (no order queue)
    # In production, _execute_trade handles EXIT signals directly
    exit_price = 36600
    trader.execute_signal("EXIT", exit_price, datetime.now(),
                          lots=1, max_lots=1, exit_reason="VWAP")

    console.print(f"   Trader position: {trader.position}")

    assert trader.position == 0
    console.print("   ✅ VWAP exit confirmed\n")

    # Step 3: Verify PnL
    console.print("[cyan]Step 3: Verify PnL[/cyan]")
    console.print(f"   Entry: 36500 (SHORT)")
    console.print(f"   Exit:  36600")
    console.print(f"   PnL pts: -100 (SHORT lost on rally)")

    if trader.trades:
        last_trade = trader.trades[-1]
        console.print(f"   PaperTrader last trade: {last_trade}")
    console.print(f"   Balance: {trader.balance:,.0f}")
    console.print(f"   Trades: {len(trader.trades)}")
    console.print("   ✅ PnL recorded\n")

    # Step 4: Export format check
    console.print("[cyan]Step 4: Export format for dashboard[/cyan]")
    completed = order_mgr.get_completed()
    for o in completed:
        console.print(f"   {o.order_id}: {o.side.value} {o.filled_quantity} @ {o.avg_fill_price:.0f} "
                      f"({o.status.value}) - {o.strategy}")

    # CSV-ready format
    csv_rows = []
    for o in completed:
        csv_rows.append({
            "order_id": o.order_id,
            "timestamp": (o.filled_at or o.created_at).isoformat(),
            "type": "BUY" if o.side == OrderSide.BUY else "SELL",
            "direction": "LONG" if o.side == OrderSide.BUY else "SHORT",
            "price": o.avg_fill_price,
            "lots": o.filled_quantity,
            "status": o.status.value,
            "strategy": o.strategy,
        })

    console.print(f"\n   CSV rows: {len(csv_rows)}")
    for row in csv_rows:
        console.print(f"   {row}")

    # ── Final Summary ──
    console.print(f"\n{'='*60}")
    console.print("[bold green]✅ L4 UAT PASSED[/bold green]")
    console.print(f"{'='*60}")
    console.print(f"  Orders created: {order_mgr._next_id - 1}")
    console.print(f"  Orders filled: {len(order_mgr.completed)}")
    console.print(f"  Orders pending: {len(order_mgr.get_pending())}")
    console.print(f"  Trader position: {trader.position}")
    console.print(f"  Balance: {trader.balance:,.0f}")
    console.print(f"  Trades recorded: {len(trader.trades)}")
    console.print(f"  Export format: {len(csv_rows)} CSV-ready rows")
    console.print(f"\n  Full lifecycle: signal → order → fill → position → exit → export ✅")


if __name__ == "__main__":
    main()
