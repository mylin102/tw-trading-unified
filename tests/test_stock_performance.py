import inspect
import logging

import yaml

from scripts.runners.stock_runner import resolve_loop_interval
from strategies.stocks.monitor import StockMonitor


def test_loop_interval_reads_config_value(tmp_path):
    config = tmp_path / "stocks.yaml"
    config.write_text(yaml.safe_dump({"loop_interval_sec": 45}))
    assert resolve_loop_interval(yaml.safe_load(config.read_text())) == 45


def test_loop_interval_defaults_to_30_seconds():
    assert resolve_loop_interval({}) == 30


def test_loop_interval_clamps_values_below_safety_floor(caplog):
    with caplog.at_level(logging.WARNING):
        assert resolve_loop_interval({"loop_interval_sec": 1}) == 10
    assert "minimum" in caplog.text


def test_production_monitor_source_has_no_per_ticker_debug_log():
    source = inspect.getsource(StockMonitor.run_iteration)
    assert "DEBUG: Before calculate_stock_squeeze" not in source
