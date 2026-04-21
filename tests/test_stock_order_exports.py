import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import yaml

from strategies.stocks.monitor import StockMonitor


class _DummyStocks(dict):
    def __getitem__(self, key):
        return SimpleNamespace(code=key)


class _DummyApi:
    def __init__(self):
        self.Contracts = SimpleNamespace(Stocks=_DummyStocks())

    def snapshots(self, contracts):
        return [SimpleNamespace(close=100.0)]

    def list_trades(self):
        return []


def test_stock_monitor_orders_file_name_is_mode_scoped(tmp_path):
    config_path = tmp_path / "stocks.yaml"
    config_path.write_text(
        yaml.safe_dump({"stocks": {"watchlist": ["2330"], "strategy": "scout_strategy"}}, allow_unicode=True),
        encoding="utf-8",
    )

    monitor = StockMonitor(api=None, config_path=str(config_path), dry_run=True)

    assert monitor.orders_path.name.endswith(f"_{monitor.mode_tag}_orders.json")


def test_stock_monitor_live_orders_fall_back_to_ledger(tmp_path):
    monitor = StockMonitor.__new__(StockMonitor)
    monitor.api = _DummyApi()
    monitor.watchlist = ["2330"]
    monitor.mode_tag = "LIVE"
    monitor.pending_orders = {}
    monitor.positions = {}
    monitor.strat_name = "scout_strategy"
    monitor.ledger_path = tmp_path / "STOCK_20260421_LIVE_trades.csv"
    monitor.orders_path = tmp_path / "STOCK_20260421_LIVE_orders.json"

    pd.DataFrame(
        [
            {
                "timestamp": "2026-04-21 09:15:00",
                "ticker": 2330,
                "strategy": "scout_strategy",
                "mode": "LIVE",
                "action": "BUY",
                "price": 880.0,
                "entry_price": 880.0,
                "qty": 1,
                "reason": "TEST",
                "pnl_gross": 0.0,
                "fees": 0.0,
                "pnl_cash": 0.0,
            }
        ]
    ).to_csv(monitor.ledger_path, index=False)

    monitor._save_orders_file()

    orders = json.loads(monitor.orders_path.read_text(encoding="utf-8"))
    assert len(orders) == 1
    assert orders[0]["ticker"] == "2330"
    assert orders[0]["status"] == "FILLED"
    assert orders[0]["mode"] == "LIVE"
