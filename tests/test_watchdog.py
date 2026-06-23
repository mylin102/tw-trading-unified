import time
import pandas as pd
import pytest
from core.shioaji_session import SystemReadiness, get_shared_system_status
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
    m._last_real_tmf_tick_at = time.time() - 2

    # Should not raise
    m._check_futures_contract_staleness()


def test_watchdog_light_recovery(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("{}")
    m = make_monitor(str(cfg))
    m.STALE_WARN_SECS = 1
    m.STALE_CRITICAL_SECS = 10
    # Simulate stale beyond warn
    m.last_tick_at = time.time() - 2
    m._last_real_tmf_tick_at = time.time() - 2

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
    monkeypatch.setattr("strategies.futures.monitor.is_taifex_futures_market_open", lambda: True)
    # Ensure deque empty before
    m._tick_bars_deque.clear()
    m._check_futures_contract_staleness()
    assert called["rolled"] is True
    assert called["refreshed"] is True
    # [Refactored] Recovery kline no longer directly injects into _tick_bars_deque.
    # Instead it saves raw data to CSV and updates last_tick_at. Data now flows
    # through the canonical bar pipeline via _periodic_backfill_bars(). Verify
    # that last_tick_at was updated (so we don't immediately re-enter recovery).
    assert m.last_tick_at > time.time() - 5  # Should have been refreshed


def test_watchdog_marks_runtime_degraded_on_stale(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("{}")
    m = make_monitor(str(cfg))
    m.STALE_WARN_SECS = 1
    m.STALE_CRITICAL_SECS = 10
    m.last_tick_at = time.time() - 2
    monkeypatch.setattr("core.shioaji_session._system_status_path", lambda: tmp_path / "runtime_status.json")
    m._check_contract_rollover = lambda: None
    m.client.get_kline = lambda ticker, interval="5m": None

    m._check_futures_contract_staleness()

    assert get_shared_system_status() == SystemReadiness.DEGRADED


def test_refresh_runtime_status_requires_fresh_tmf_for_trading(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("{}")
    m = make_monitor(str(cfg))
    m.STALE_WARN_SECS = 5
    m.is_trading_ready = True
    monkeypatch.setattr("core.shioaji_session._system_status_path", lambda: tmp_path / "runtime_status.json")

    # 2026-06-23 Gemini CLI: Set both last_tick_at and _last_real_tmf_tick_at to trigger staleness check correctly
    m.last_tick_at = time.time() - 10
    m._last_real_tmf_tick_at = time.time() - 10
    m._refresh_runtime_status()
    assert get_shared_system_status() == SystemReadiness.DEGRADED

    m.last_tick_at = time.time()
    m._last_real_tmf_tick_at = time.time()
    m._refresh_runtime_status()
    assert get_shared_system_status() == SystemReadiness.TRADING


def test_tmf_feed_age_falls_back_when_feed_health_reports_infinity(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("{}")
    m = make_monitor(str(cfg))
    m.feed_health = type("FeedHealth", (), {"age": lambda self, symbol: float("inf")})()
    m._last_real_tmf_tick_at = time.time() - 2

    age = m._tmf_feed_age_secs()

    assert 0 <= age < 5


def test_watchdog_uses_local_tmf_timer_during_startup_gap(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("{}")
    m = make_monitor(str(cfg))
    m.STALE_WARN_SECS = 5
    m.STALE_CRITICAL_SECS = 10
    m.feed_health = type("FeedHealth", (), {"age": lambda self, symbol: float("inf")})()
    m._last_real_tmf_tick_at = time.time() - 2
    m._check_contract_rollover = lambda: None
    m.client.get_kline = lambda ticker, interval="5m": None
    monkeypatch.setattr("strategies.futures.monitor.is_taifex_futures_market_open", lambda: True)

    m._check_futures_contract_staleness()


def test_watchdog_critical_exit(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("{}")
    m = make_monitor(str(cfg))
    m.STALE_WARN_SECS = 1
    m.STALE_CRITICAL_SECS = 5
    # Simulate critical stale
    m.last_tick_at = time.time() - 6
    m._last_real_tmf_tick_at = time.time() - 6

    # api.quote.unsubscribe should exist but we expect RuntimeError
    class API2:
        class Quote:
            def unsubscribe(self, *args, **kwargs):
                return True
        quote = Quote()
    m.api = API2()
    monkeypatch.setattr("strategies.futures.monitor.is_taifex_futures_market_open", lambda: True)

    with pytest.raises(RuntimeError):
        m._check_futures_contract_staleness()


def test_watchdog_does_not_exit_during_scheduled_recess(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("{}")
    m = make_monitor(str(cfg))
    m.STALE_WARN_SECS = 1
    m.STALE_CRITICAL_SECS = 5
    m._last_real_tmf_tick_at = time.time() - 60

    monkeypatch.setattr("strategies.futures.monitor.is_taifex_futures_market_open", lambda: False)

    m._check_futures_contract_staleness()
