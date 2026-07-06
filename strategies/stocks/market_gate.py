"""
Market Gate — regime-based gating for stock entry.

Uses skew signal from futures system to answer 'should I be trading right now?'
Replaces unconditional strategy scanning with market-aware decision making.

FAIL-CLOSED by default: if skew signal is missing, stale, or unreadable,
the gate returns BLOCK_LONG. Safety over convenience.

Gate output:
  ALLOW_LONG  — normal operation, all strategies eligible
  REDUCE_SIZE — cut position size to 50%, only high-conviction entries
  BLOCK_LONG  — no new long entries, only manage existing positions
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


# ── Config ──────────────────────────────────────────────────────────
SKEW_SIGNAL_PATH = Path(__file__).parent.parent / "data" / "skew_signal.json"
SKEW_MAX_AGE_SECS = 120  # 2 min — if skew data is older, gate is closed

# UNKNOWN = signal file missing completely
# STALE   = signal file exists but too old
# Both map to BLOCK_LONG below.
REGIME_GATE_MAP = {
    "BULL":      "ALLOW_LONG",
    "STRONG":    "ALLOW_LONG",
    "WEAK":      "ALLOW_LONG",
    "CHOP":      "ALLOW_LONG",
    "BEAR":      "BLOCK_LONG",
    "CRASH":     "BLOCK_LONG",
    "UNKNOWN":   "BLOCK_LONG",   # fail-closed: no signal file
    "STALE":     "BLOCK_LONG",   # fail-closed: signal too old
}

# Throttle warning log to once per N seconds
_WARNED_AT: float = 0
_WARN_COOLDOWN = 300  # 5 min


@dataclass
class GateResult:
    """Structured gate result with reason for audit / logging."""
    allowed: bool
    regime: str
    gate: str
    reason: str


def _read_skew_signal() -> Optional[dict]:
    """Read latest skew signal from disk."""
    try:
        if not SKEW_SIGNAL_PATH.exists():
            return None
        with open(SKEW_SIGNAL_PATH, "r") as f:
            data = json.load(f)
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _skew_is_fresh(data: dict) -> bool:
    """Check if skew signal is recent enough to trust."""
    ts = data.get("timestamp", 0)
    return (time.time() - ts) < SKEW_MAX_AGE_SECS


def get_market_regime() -> str:
    """Resolve current market regime from skew signal.

    Priority:
      1. Fresh skew signal from futures system
      2. UNKNOWN if signal file is missing or all attempts fail
      3. STALE  if signal file exists but timestamp is too old
    """
    data = _read_skew_signal()
    if data is None:
        return "UNKNOWN"
    if not _skew_is_fresh(data):
        return "STALE"
    return data.get("regime", "UNKNOWN")


def get_gate() -> str:
    """Get the current gate state: ALLOW_LONG | REDUCE_SIZE | BLOCK_LONG.

    DEPRECATED: prefer get_gate_result() for audit trail.
    """
    return get_gate_result().gate


def get_gate_result() -> GateResult:
    """Return structured gate result with reason for audit / logging.

    Logs a warning once per WARN_COOLDOWN when skew signal is missing or stale.
    """
    global _WARNED_AT
    regime = get_market_regime()
    gate = REGIME_GATE_MAP.get(regime, "BLOCK_LONG")
    allowed = gate in ("ALLOW_LONG", "REDUCE_SIZE")

    reason = f"regime={regime} gate={gate}"
    if regime == "UNKNOWN":
        reason = "skew_signal.json missing or unreadable — gate BLOCK_LONG"
        if time.time() - _WARNED_AT > _WARN_COOLDOWN:
            logger.warning("MarketGate: %s", reason)
            _WARNED_AT = time.time()
    elif regime == "STALE":
        reason = f"skew_signal.json too old (>={SKEW_MAX_AGE_SECS}s) — gate BLOCK_LONG"
        if time.time() - _WARNED_AT > _WARN_COOLDOWN:
            logger.warning("MarketGate: %s", reason)
            _WARNED_AT = time.time()

    return GateResult(allowed=allowed, regime=regime, gate=gate, reason=reason)


def get_size_multiplier() -> float:
    """Position size multiplier based on gate state."""
    gate = get_gate()
    if gate == "ALLOW_LONG":
        return 1.0
    elif gate == "REDUCE_SIZE":
        return 0.5
    else:
        return 0.0


def strategy_allowed(strategy_name: str, regime: Optional[str] = None) -> bool:
    """Check if a specific strategy is allowed in the current regime.

    This is a secondary filter — the primary is get_gate_result().allowed.
    """
    if regime is None:
        regime = get_market_regime()
    if regime in {"BEAR", "CRASH"}:
        return False
    return True
