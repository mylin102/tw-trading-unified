import os
from unittest.mock import MagicMock, patch

import pytest
import yaml

from strategies.stocks.monitor import StockMonitor


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "stocks.yaml"
    path.write_text(yaml.safe_dump({
        "live_trading": False,
        "stocks": {"watchlist": ["2330"], "strategy": "scout_strategy"},
    }))
    return path


def make_monitor(config_file):
    return StockMonitor(MagicMock(), str(config_file), dry_run=True)


def bump_mtime(path):
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))


def test_watchlist_reload_applies_on_next_reload(config_file):
    monitor = make_monitor(config_file)
    monitor._loop_state = {"active_watchlist": ["2330"]}
    config_file.write_text(yaml.safe_dump({
        "live_trading": False,
        "stocks": {"watchlist": ["2454", "3017"], "strategy": "scout_strategy"},
    }))
    bump_mtime(config_file)

    monitor._reload_config_if_changed()

    assert monitor.watchlist == ["2454", "3017"]
    assert monitor._loop_state["active_watchlist"] is None


def test_unchanged_mtime_does_not_reload(config_file):
    monitor = make_monitor(config_file)
    with patch("strategies.stocks.monitor.yaml.safe_load", wraps=yaml.safe_load) as load:
        monitor._reload_config_if_changed()
    load.assert_not_called()


@pytest.mark.parametrize("replacement", ["stocks: [", None])
def test_failed_reload_keeps_old_config_and_live_semantics(config_file, replacement):
    monitor = make_monitor(config_file)
    old_cfg = monitor.cfg
    old_watchlist = monitor.watchlist
    if replacement is None:
        config_file.unlink()
    else:
        config_file.write_text(replacement)
    bump_mtime(config_file) if config_file.exists() else None

    monitor._reload_config_if_changed()

    assert monitor.cfg == old_cfg
    assert monitor.watchlist == old_watchlist
    assert monitor.live_trading is False
