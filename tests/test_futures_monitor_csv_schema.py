from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from core.date_utils import get_session_date_str
from strategies.futures.monitor import FuturesMonitor


def test_ensure_indicator_schema_repairs_unnamed_timestamp_column(tmp_path):
    csv_path = tmp_path / "TMF_20260420_PAPER_indicators.csv"
    pd.DataFrame(
        [{"timestamp": "2026-04-20 11:45:00", "close": 37637.0}]
    ).set_index("timestamp").to_csv(csv_path)

    monitor = FuturesMonitor.__new__(FuturesMonitor)

    monitor._ensure_indicator_schema(csv_path, ["timestamp", "close", "score"])

    repaired = pd.read_csv(csv_path)
    assert "timestamp" in repaired.columns
    assert not any(str(col).startswith("Unnamed") for col in repaired.columns)
    assert "score" in repaired.columns


def test_backfill_night_gaps_persists_timestamp_header(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    market_dir = Path("logs/market_data")
    market_dir.mkdir(parents=True)

    date_str = get_session_date_str(datetime.now())
    csv_path = market_dir / f"TMF_{date_str}_PAPER_indicators.csv"
    pd.DataFrame(
        [
            {
                "timestamp": "2026-04-20 11:40:00",
                "Open": 1,
                "High": 1,
                "Low": 1,
                "Close": 1,
                "Volume": 1,
            }
        ]
    ).to_csv(csv_path, index=False)

    api_df = pd.DataFrame(
        [
            {
                "Open": 2,
                "High": 2,
                "Low": 2,
                "Close": 2,
                "Volume": 2,
            }
        ],
        index=pd.to_datetime([datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=5)]),
    )

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.dry_run = False
    monitor.api = object()
    monitor.live_trading = False
    monitor.ticker = "TMF"

    monitor._backfill_night_gaps(api_df)

    header = csv_path.read_text().splitlines()[0]
    assert header.startswith("timestamp,")


def test_backfill_night_gaps_repairs_corrupt_csv_and_rebuilds_from_api(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    market_dir = Path("logs/market_data")
    market_dir.mkdir(parents=True)

    date_str = get_session_date_str(datetime.now())
    csv_path = market_dir / f"TMF_{date_str}_PAPER_indicators.csv"
    pd.DataFrame(
        [
            {
                "timestamp": "2026-04-20 11:45:00",
                "Close": 10,
                "High": 10,
                "Low": 10,
                "Open": 10,
                "Volume": 10,
            }
        ]
    ).set_index("timestamp").to_csv(csv_path)

    api_df = pd.DataFrame(
        [
            {
                "Open": 11,
                "High": 12,
                "Low": 10,
                "Close": 11,
                "Volume": 20,
            },
            {
                "Open": 12,
                "High": 13,
                "Low": 11,
                "Close": 12,
                "Volume": 25,
            },
        ],
        index=pd.to_datetime([
            "2026-04-20 11:45:00",
            "2026-04-20 11:50:00",
        ]),
    )

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.dry_run = False
    monitor.api = object()
    monitor.live_trading = False
    monitor.ticker = "TMF"

    monitor._backfill_night_gaps(api_df)

    repaired = pd.read_csv(csv_path)
    assert "timestamp" in repaired.columns
    assert not any(str(col).startswith("Unnamed") for col in repaired.columns)
    assert repaired["timestamp"].iloc[-1] == "2026-04-20 11:50:00"
