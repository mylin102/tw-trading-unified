"""
Strategy Evaluation Trace — structured per-bar decision record.

Each strategy returns a StrategyEval on every bar.
Router collects evals → prints one-line summary → writes JSONL.

No trade = no decision → no trace? Wrong.
No trade IS a decision. Record it.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_log_dir = Path(__file__).resolve().parent.parent / "logs" / "router_trace"
_lock = threading.Lock()


@dataclass
class StrategyEval:
    """Evaluation result for one strategy on one bar."""

    name: str
    enabled: bool
    triggered: bool
    action: Optional[str] = None
    edge_score: Optional[float] = None
    skip_reason: Optional[str] = None
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass
class RouterTrace:
    """Complete trace for one bar — all strategy evals + router decision."""

    ts: str
    regime: str
    bias: str
    selected: Optional[str]
    selected_action: Optional[str]
    strategies: list[dict[str, Any]]


def _ensure_log_dir() -> Path:
    _log_dir.mkdir(parents=True, exist_ok=True)
    return _log_dir


def _today_path() -> Path:
    today = datetime.now().strftime("%Y%m%d")
    return _ensure_log_dir() / f"router_trace_{today}.jsonl"


def write_trace(trace: RouterTrace) -> None:
    """Append one RouterTrace as a JSONL line."""
    with _lock:
        path = _today_path()
        with open(path, "a") as f:
            f.write(json.dumps(asdict(trace), default=str) + "\n")


def print_trace_summary(trace: RouterTrace) -> None:
    """Print a single-line summary to stdout."""
    parts = [f"[RouterTrace] ts={trace.ts} regime={trace.regime} selected={trace.selected or 'None'}"]
    for s in trace.strategies:
        if s["triggered"]:
            status = f"TRADE:{s['action']}"
        elif not s["enabled"]:
            status = f"DISABLED:{s['skip_reason'] or 'NOT_ENABLED'}"
        elif s["skip_reason"]:
            status = f"SKIP:{s['skip_reason']}"
        else:
            status = "NO_SIGNAL"
        edge = s.get("edge_score", "")
        parts.append(f"{s['name']}={status} edge={edge if edge is not None else 'N/A'}")
    print(" | ".join(parts), flush=True)
