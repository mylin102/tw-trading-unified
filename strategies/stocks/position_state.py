"""
Stock position state persistence.

Separates current position state from the immutable trade event log.
OVERNIGHT_RECOVERY writes to position_state, NOT to the trade ledger.

File format (JSON):
  {
    "ticker": {
      "entry_ts": "2026-04-27 12:39:34",
      "strategy": "scout_strategy",
      "mode": "PAPER",
      "avg_cost": 44.0,
      "qty": 113,
      "realized_pnl": 0.0,
      "recovered_from": "20260427",
      "last_recovered_at": "2026-05-04 00:22:22"
    },
    ...
  }
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def position_state_path(trades_dir: Path, date_str: str, mode_tag: str) -> Path:
    """STOCK_YYYYMMDD_PAPER_positions.json"""
    return trades_dir / f"STOCK_{date_str}_{mode_tag}_positions.json"


def load_position_state(path: Path) -> dict:
    """Load position state JSON. Returns empty dict if file missing/corrupt."""
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Position state load failed: %s path=%s", e, path)
        return {}


def save_position_state(path: Path, state: dict) -> None:
    """Atomically write position state JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.flush()
    tmp.replace(path)


def merge_recovery(
    current_state: dict,
    ticker: str,
    original_entry_ts: str,
    strategy: str,
    mode: str,
    avg_cost: float,
    qty: int,
    realized_pnl: float,
    recovered_from: str,
    recovery_ts: str,
) -> dict:
    """Merge a recovered position into state, preserving original entry fields."""
    existing = current_state.get(ticker, {})
    # Only update if not already set or qty changed
    if ticker not in current_state or existing.get("qty", 0) != qty:
        current_state[ticker] = {
            "entry_ts": existing.get("entry_ts", original_entry_ts),
            "strategy": existing.get("strategy", strategy),
            "mode": mode,
            "avg_cost": existing.get("avg_cost", avg_cost),
            "qty": qty,
            "realized_pnl": existing.get("realized_pnl", realized_pnl),
            "recovered_from": recovered_from,
            "last_recovered_at": recovery_ts,
        }
    return current_state
