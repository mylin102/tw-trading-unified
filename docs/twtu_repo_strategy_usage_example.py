from __future__ import annotations

import pandas as pd

from twtu_repo_style_kbar_strategy import KbarFeatureStrategy, PositionSnapshot, SignalAction


def process_symbol(feature_df: pd.DataFrame, symbol: str, account_equity: float) -> None:
    strategy = KbarFeatureStrategy(
        symbol=symbol,
        long_enabled=False,
        short_enabled=True,
        adx_threshold=20,
        require_breakout=True,
    )

    latest = feature_df.iloc[-1]

    # This snapshot should come from your position / order-state service.
    position = PositionSnapshot(
        symbol=symbol,
        side="FLAT",
        qty=0,
        avg_price=0.0,
        bars_held=0,
        stop_loss=None,
        take_profit=None,
    )

    decision = strategy.evaluate(latest, position)
    print(f"[{symbol}] action={decision.action} reason={decision.reason}")

    if decision.action in {SignalAction.BUY, SignalAction.SELL}:
        entry = float(latest["close"])
        stop = float(decision.stop_loss)
        qty = strategy.calc_position_size(
            equity=account_equity,
            entry=entry,
            stop=stop,
            size_mult=decision.size_mult,
        )

        if qty <= 0:
            print(f"[{symbol}] skip order: qty=0")
            return

        order_request = {
            "symbol": symbol,
            "side": "BUY" if decision.action == SignalAction.BUY else "SELL",
            "qty": qty,
            "order_type": "MKT",
            "time_in_force": "IOC",
            "strategy": decision.strategy_name,
            "stop_loss": decision.stop_loss,
            "take_profit": decision.take_profit,
            "reason": decision.reason,
            "meta": decision.meta or {},
        }
        print("submit order request:")
        print(order_request)


if __name__ == "__main__":
    df = pd.read_csv("2026-04-21T19-22_export.csv")
    process_symbol(df, symbol="TXF", account_equity=1_000_000)
