"""RED/GREEN: exhaustive release-tree write migration (P0 decisions).

ALL PM2-started production writers/readers must resolve through
core.runtime_paths (runtime_logs / runtime_path) — the single runtime
authority. The 14-site inventory:

  core/health_evidence_exporter.py   exports/market_data (+soak)
  core/dynamics_capture.py           logs/ticks/dynamics (default)
  core/market_data_runtime.py        logs/ticks/dynamics (default pass)
  core/strategy_eval.py              <repo>/logs/router_trace
  strategies/futures/monitor.py      logs/market_data/TMF_trades.csv
  strategies/futures/monitor.py      exports/trades/live/diagnostics
  strategies/options/...squeeze...   CWD/strategies/options/logs/<sub>
  strategies/options/...squeeze...   logs/market_data ×3 + options_watchdog
  strategies/futures/mts/soak...     data/telemetry/shadow-soak
  strategies/futures/mts/policy_j... exports/telemetry/policy_j
  ui/dashboard.py                    logs/... reads (6 + options)

Legacy squeeze_futures strategies + one-off scripts are NOT in the PM2
scope: the manifest proves the PM2 entries never START them.
"""

import os
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_MONITOR = _REPO / "strategies" / "futures" / "monitor.py"

# per-file forbidden release-tree path literals (exact evidence anchors)
_FORBIDDEN = {
    "core/health_evidence_exporter.py": ['"exports/market_data"'],
    "core/dynamics_capture.py": ['"logs/ticks/dynamics"'],
    "core/market_data_runtime.py": ['"logs/ticks/dynamics"'],
    "core/strategy_eval.py": ['"router_trace"'],
    "strategies/futures/monitor.py": [
        'Path("logs/market_data/TMF_trades.csv")',
        'Path("exports/trades/live/diagnostics")',
    ],
    "strategies/options/live_options_squeeze_monitor.py": [
        "os.getcwd()",
        '"logs/market_data"',
        '"logs/options_watchdog"',
    ],
    "strategies/futures/mts/soak_collector.py": [
        '"data/telemetry/shadow-soak"',
    ],
    "strategies/plugins/futures/active/tmf_spread.py": [
        '"data/telemetry/shadow-soak"',
    ],
    "strategies/futures/mts/policy_j_telemetry_writer.py": [
        '"policy_j"',
    ],
    "ui/dashboard.py": [
        '"logs/router_trace"',
        '"logs/market_data"',
        '"logs/mts_spread_events.jsonl"',
        '"logs/stocks"',
        '"logs/backups/trade_records"',
    ],
}


# ── runtime_logs() authority semantics ─────────────────────────────────────

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
    assert runtime_logs() == os.path.join(_REPO_ROOT, "logs")
    assert runtime_logs("stocks") == \
        os.path.join(_REPO_ROOT, "logs", "stocks")


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
    assert 'os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs"' \
        not in src, "repo-root logs/ dir derivation still present"


# ── P0 exhaustive inventory (all PM2-started production sources) ───────────

@pytest.mark.parametrize("rel,patterns", sorted(_FORBIDDEN.items()))
def test_no_release_tree_paths_in_production_sources(rel, patterns):
    src = (_REPO / rel).read_text(encoding="utf-8")
    for pat in patterns:
        assert pat not in src, \
            f"{rel} still contains release-tree path: {pat}"


def test_pm2_entries_never_start_legacy_strategies_or_scripts():
    """Manifest: the PM2 apps (trading-system=main.py, dashboard=
    ui/dashboard.py, stock-runner=scripts/runners/stock_runner.py) never
    START the legacy squeeze_futures strategies or one-off scripts.
    The only squeeze_futures references in the PM2 entries are dashboard
    get_point_value CONSTANT lookups (never strategy execution)."""
    dash = (_REPO / "ui" / "dashboard.py").read_text(encoding="utf-8")
    sq_imports = re.findall(
        r"(?:import|from)\s+strategies\.futures\.squeeze_futures\.[^\s(]+",
        dash)
    for imp in sq_imports:
        assert "engine.constants" in imp, \
            f"dashboard imports a non-constants squeeze_futures module: {imp}"
    for entry in ("main.py", "ui/dashboard.py",
                  "scripts/runners/stock_runner.py"):
        src = (_REPO / entry).read_text(encoding="utf-8")
        for banned in ("import scripts.", "from scripts.",
                       "generate_adaptive_dataset",
                       "run_portfolio_sweep"):
            assert banned not in src, \
                f"{entry} references a one-off script: {banned}"
    for legacy in ("strategies/futures/squeeze_futures/engine/strategy",
                   "strategies/futures/squeeze_futures/engine/live",
                   "tmf_strategy", "TmfSpreadStrategy"):
        assert legacy not in (_REPO / "main.py").read_text(encoding="utf-8")
