import time
import pandas as pd
import pytest
from strategies.futures.monitor import FuturesMonitor


def make_monitor(tmp_config_path):
    # Create a monitor in dry_run to avoid heavy init, then flip to non-dry for testing
    dummy_api = type("A", (), {})()
    m = FuturesMonitor(api=dummy_api, config_path=tmp_config_path, dry_run=True)
    # Call setup to initialize deques and warmup path (dry_run will short-circuit after init)
    m.setup()
    assert hasattr(m, '_tick_bars_deque')
    m.dry_run = False
    # Provide a minimal api object required by watchdog (quote.unsubscribe)
    class API:
        class Quote:
            def unsubscribe(self, *args, **kwargs):
                return True
        quote = Quote()
    m.api = API()
    return m


def test_watchdog_no_action(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("{}")
    m = make_monitor(str(cfg))
    m.STALE_WARN_SECS = 5
    m.STALE_CRITICAL_SECS = 10
    m.last_tick_at = time.time() - 2

    # Should not raise
    m._check_futures_contract_staleness()


def test_watchdog_light_recovery(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("{}")
    m = make_monitor(str(cfg))
    m.STALE_WARN_SECS = 1
    m.STALE_CRITICAL_SECS = 10
    # Simulate stale beyond warn
    m.last_tick_at = time.time() - 2

    # Monkeypatch rollover and client.get_kline to return a small dataframe
    called = {"rolled": False, "refreshed": False}

    def fake_rollover():
        called["rolled"] = True

    m._check_contract_rollover = fake_rollover

    def fake_get_kline(ticker, interval="5m"):
        idx = pd.date_range(end=pd.Timestamp.now(), periods=5, freq='5T')
        df = pd.DataFrame({
            "Open": [100, 101, 102, 103, 104],
            "High": [101, 102, 103, 104, 105],
            "Low": [99, 100, 101, 102, 103],
            "Close": [100, 101, 102, 103, 104],
            "Volume": [10, 10, 10, 10, 10],
        }, index=idx)
        called["refreshed"] = True
        return df

    m.client.get_kline = fake_get_kline
    # Ensure deque empty before
    m._tick_bars_deque.clear()
    m._check_futures_contract_staleness()
    assert called["rolled"] is True
    assert called["refreshed"] is True
    assert len(m._tick_bars_deque) > 0


def test_watchdog_critical_exit(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("{}")
    m = make_monitor(str(cfg))
    m.STALE_WARN_SECS = 1
    m.STALE_CRITICAL_SECS = 5
    # Simulate critical stale
    m.last_tick_at = time.time() - 6

    # api.quote.unsubscribe should exist but we expect RuntimeError
    class API2:
        class Quote:
            def unsubscribe(self, *args, **kwargs):
                return True
        quote = Quote()
    m.api = API2()

    with pytest.raises(RuntimeError):
        m._check_futures_contract_staleness()
