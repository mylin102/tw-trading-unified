"""
Runtime Replay System — Observability & Post-Mortem Intelligence.
Saves unified artifacts (bars + decisions) for offline replay and analysis.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Mapping

logger = logging.getLogger(__name__)

_REPLAY_ROOT = Path(__file__).resolve().parent.parent / "artifacts" / "replay"
_lock = threading.Lock()

@dataclass
class ReplaySnapshot:
    """Unified snapshot of a single bar and the resulting strategy decisions."""
    ts: str
    bar: dict[str, Any]
    regime: str
    bias: str
    candidates: list[str]
    winner: Optional[str]
    winner_action: Optional[str]
    strategies: dict[str, dict[str, Any]]
    system_status: str

class ReplayEngine:
    """Manages recording and retrieval of trading session replays."""

    def __init__(self, root_dir: Path = _REPLAY_ROOT):
        self.root_dir = root_dir
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def record_bar_decision(
        self,
        bar: Mapping[str, Any],
        regime: str,
        bias: str,
        candidates: list[str],
        winner: Optional[str],
        winner_action: Optional[str],
        strategy_evals: list[dict[str, Any]],
        system_status: str = "TRADING"
    ) -> None:
        """Saves a complete snapshot for the current bar."""
        ts = bar.get("timestamp") or bar.get("ts") or datetime.now().isoformat()
        if hasattr(ts, "strftime"):
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
            day_str = ts.strftime("%Y-%m-%d")
        else:
            ts_str = str(ts)
            # Try to extract date part
            day_str = ts_str.split(' ')[0] if ' ' in ts_str else datetime.now().strftime("%Y-%m-%d")

        # Organize into dict for easy lookup
        strategies_dict = {
            s["name"]: {
                "triggered": s.get("triggered", False),
                "action": s.get("action"),
                "skip_reason": s.get("skip_reason"),
                "edge_score": s.get("edge_score"),
                "notes": s.get("notes", {})
            }
            for s in strategy_evals
        }

        snapshot = ReplaySnapshot(
            ts=ts_str,
            bar=dict(bar),
            regime=regime,
            bias=bias,
            candidates=candidates,
            winner=winner,
            winner_action=winner_action,
            strategies=strategies_dict,
            system_status=system_status
        )

        self._save_snapshot(day_str, snapshot)

    def _save_snapshot(self, day_str: str, snapshot: ReplaySnapshot) -> None:
        """Internal: writes snapshot to day-partitioned JSONL."""
        day_dir = self.root_dir / day_str
        day_dir.mkdir(parents=True, exist_ok=True)
        
        replay_file = day_dir / "session_replay.jsonl"
        
        with _lock:
            try:
                with open(replay_file, "a") as f:
                    f.write(json.dumps(asdict(snapshot), default=str) + "\n")
            except Exception as e:
                logger.error(f"Failed to record replay snapshot: {e}")

# Global instance
replay_engine = ReplayEngine()
