import pandas as pd


def test_legacy_trade_ledger_backfills_name_from_indicator_snapshot(tmp_path, monkeypatch):
    import strategies.stocks.monitor as monitor_module
    from strategies.stocks.monitor import StockMonitor

    indicator_dir = tmp_path / "market_data"
    indicator_dir.mkdir()
    pd.DataFrame([{"name": "貝爾威勒"}]).to_csv(
        indicator_dir / "STOCK_7861_20260916_indicators.csv", index=False
    )
    monkeypatch.setattr(monitor_module, "MKT_LOGS", indicator_dir)

    ledger = tmp_path / "STOCK_20260916_PAPER_trades.csv"
    pd.DataFrame([{
        "timestamp": "2026-09-16 09:12:15",
        "ticker": "7861",
        "action": "SELL",
        "price": 1180.0,
    }]).to_csv(ledger, index=False)

    monitor = StockMonitor.__new__(StockMonitor)
    monitor.ledger_path = ledger
    monitor.api = None
    monitor._ensure_trade_ledger_schema()

    repaired = pd.read_csv(ledger)
    assert repaired.loc[0, "name"] == "貝爾威勒"
