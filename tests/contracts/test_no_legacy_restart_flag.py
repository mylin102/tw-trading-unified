"""P0 contract: production code cannot use the legacy restart-flag control plane."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_SOURCES = (
    "main.py",
    "ui/dashboard.py",
    "scripts/runners/stock_runner.py",
    "strategies/futures/monitor.py",
    "strategies/options/live_options_squeeze_monitor.py",
    "strategies/stocks/monitor.py",
    "scripts/sync/sync_external_watchlist.py",
    "scripts/sync/sync_watchlist.py",
)


def test_production_code_has_no_legacy_restart_flag_control_plane():
    for relative_path in PRODUCTION_SOURCES:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative_path)
        strings = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        names = [node.id for node in ast.walk(tree) if isinstance(node, ast.Name)]
        assert ".restart" not in strings, relative_path
        assert "RESTART_FLAG" not in names, relative_path
        assert "trigger_restart" not in source, relative_path
