"""RED/GREEN tests: Python release-tree write migration.

The trading engine (strategies/futures/monitor.py) must write ALL
runtime data (market_data CSVs, backups, stocks, anomalous quotes, MTS
events, orders exports) under the runtime namespace
(core.runtime_paths.runtime_logs / runtime_path) — NEVER into the
release source tree. The dashboard already reads the runtime namespace,
so repo-relative writes recreate the 2026-08-05 split incident class.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_MONITOR = Path(__file__).resolve().parents[2] / "strategies" / "futures" \
    / "monitor.py"


def test_runtime_logs_uses_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    from core.runtime_paths import runtime_logs
    assert runtime_logs() == str(tmp_path / "logs")
    assert runtime_logs("market_data") == \
        str(tmp_path / "logs" / "market_data")


def test_runtime_logs_fallback_repo_logs(monkeypatch):
    # PAPER/dev fallback (no TRADING_RUNTIME_DIR) keeps the repo logs —
    # explicit, tested behaviour (production always sets the env via PM2)
    monkeypatch.delenv("TRADING_RUNTIME_DIR", raising=False)
    from core.runtime_paths import runtime_logs, _REPO_ROOT
    assert runtime_logs().startswith(
        os.path.join(_REPO_ROOT, "logs") + os.sep)


def test_monitor_no_release_tree_log_paths():
    # source-level regression: every identified release-tree path must be
    # gone from the trading engine (migration inventory complete)
    src = _MONITOR.read_text(encoding="utf-8")
    forbidden = [
        'os.path.join(os.getcwd(), "logs")',      # CWD logs (line 1850)
        '"logs", "market_data"',                  # os.path.join repo-root logs
        '"logs/market_data"',                     # Path(f"logs/...") reads
        '"logs/stocks"',                          # stock trade dir (3080)
        '"logs/backups/trade_records"',           # backup dir (3152)
        '_dir = "logs"',                          # MTS event log (4784)
        '"exports/trades"',                       # orders export dir (3387)
    ]
    for pat in forbidden:
        assert pat not in src, \
            f"release-tree write path still present in monitor.py: {pat}"
    # the repo-root-derived market_data dir must be gone too
    assert 'os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs"' \
        not in src, "repo-root logs/ dir derivation still present"
