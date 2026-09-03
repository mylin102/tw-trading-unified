"""
Market Gate — regime-based gating for stock entry.

Uses skew signal from futures system to answer 'should I be trading right now?'
Replaces unconditional strategy scanning with market-aware decision making.

FAIL-CLOSED by default: if skew signal is missing, stale, or unreadable,
the gate returns BLOCK_LONG unless an explicit fallback_regime from StockMonitor
(e.g., IX0001 / TXF benchmark trend) is supplied. Safety over convenience.

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
SKEW_SIGNAL_PATH = Path(__file__).parent.parent.parent / "data" / "skew_signal.json"
SKEW_MAX_AGE_SECS = 120  # 2 min — if skew data is older, gate is closed

REGIME_GATE_MAP = {
    "BULL":      "ALLOW_LONG",
    "STRONG":    "ALLOW_LONG",
    "WEAK":      "ALLOW_LONG",
    "CHOP":      "ALLOW_LONG",
    "NORMAL":    "ALLOW_LONG",
    "BEAR":      "BLOCK_LONG",
    "CRASH":     "BLOCK_LONG",
    "UNKNOWN":   "BLOCK_LONG",   # fail-closed: no signal file and no fallback
    "STALE":     "BLOCK_LONG",   # fail-closed: signal too old and no fallback
}

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

def get_market_regime(fallback_regime: Optional[str] = None) -> str:
    """Resolve current market regime from skew signal or fallback benchmark.

    Priority:
      1. Fresh skew signal from futures/options system
      2. fallback_regime from StockMonitor (IX0001/TXF benchmark EMA)
      3. UNKNOWN if signal file is missing and no fallback provided
      4. STALE if signal file exists but timestamp is too old and no fallback
    """
    data = _read_skew_signal()
    if data is None:
        if fallback_regime:
            return fallback_regime
        return "UNKNOWN"
    if not _skew_is_fresh(data):
        if fallback_regime:
            return fallback_regime
        return "STALE"
    return data.get("regime", fallback_regime or "UNKNOWN")

def get_gate(fallback_regime: Optional[str] = None) -> str:
    """Get the current gate state: ALLOW_LONG | REDUCE_SIZE | BLOCK_LONG."""
    return get_gate_result(fallback_regime=fallback_regime).gate

def get_gate_result(fallback_regime: Optional[str] = None) -> GateResult:
    """Return structured gate result with reason for audit / logging."""
    global _WARNED_AT
    regime = get_market_regime(fallback_regime=fallback_regime)
    gate = REGIME_GATE_MAP.get(regime, "BLOCK_LONG")
    allowed = gate in ("ALLOW_LONG", "REDUCE_SIZE")

    reason = f"regime={regime} gate={gate}"
    if regime == "UNKNOWN":
        reason = "skew_signal.json missing — fail-closed gate BLOCK_LONG"
        if time.time() - _WARNED_AT > _WARN_COOLDOWN:
            logger.warning("MarketGate: %s", reason)
            _WARNED_AT = time.time()
    elif regime == "STALE":
        reason = f"skew_signal.json too old (>={SKEW_MAX_AGE_SECS}s) — gate BLOCK_LONG"
        if time.time() - _WARNED_AT > _WARN_COOLDOWN:
            logger.warning("MarketGate: %s", reason)
            _WARNED_AT = time.time()

    return GateResult(allowed=allowed, regime=regime, gate=gate, reason=reason)

def get_size_multiplier(fallback_regime: Optional[str] = None) -> float:
    """Position size multiplier based on gate state."""
    gate = get_gate(fallback_regime=fallback_regime)
    if gate == "ALLOW_LONG":
        return 1.0
    elif gate == "REDUCE_SIZE":
        return 0.5
    else:
        return 0.0

def strategy_allowed(strategy_name: str, regime: Optional[str] = None, fallback_regime: Optional[str] = None) -> bool:
    """Check if a specific strategy is allowed in the current regime."""
    if regime is None:
        regime = get_market_regime(fallback_regime=fallback_regime)
    if regime in {"BEAR", "CRASH"}:
        return False
    return True
