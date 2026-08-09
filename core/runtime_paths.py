"""Single authority for the persistent runtime data root.

Both the trading engine and the dashboard resolve runtime data (logs, exports,
data) through this module so a deploy-directory change can never split the
ledgers again (2026-08-05 incident: dashboard read <repo>/logs while the
engine wrote <runtime>/logs).

Resolution order:
  1. $TRADING_RUNTIME_DIR  (set by PM2 ecosystem.config.js env for both apps)
  2. repo root fallback    (dev/backtest on Air4: identical to old behaviour)
"""
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def runtime_root() -> str:
    return os.environ.get("TRADING_RUNTIME_DIR") or _REPO_ROOT


def runtime_path(*parts) -> str:
    return os.path.join(runtime_root(), *parts)


def runtime_logs(*parts) -> str:
    """Runtime log namespace: <runtime_root>/logs[/...].

    Both the trading engine and the dashboard resolve ALL log writes
    (market_data CSVs, backups, stocks, anomalous quotes, MTS events)
    through this — never into the release source tree. Production
    always sets TRADING_RUNTIME_DIR (PM2 env); dev/paper falls back to
    <repo>/logs explicitly."""
    return os.path.join(runtime_root(), "logs", *parts)
