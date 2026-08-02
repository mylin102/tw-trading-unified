"""P2 wire: Spread Shadow Bridge — routes gated-accepted ticks into the
spread synchronizer/collector shadow path. SHADOW-ONLY: never influences
decisions, never submits orders, never mutates live state.

Enabled only when explicitly activated (config/env) — default DISABLED so
production behaviour is bit-identical without it.
"""
import os

from core.spread_renko_shadow import SpreadRenkoShadowCollector
from core.spread_synchronizer import SpreadSynchronizer

# Shadow activation: env var or explicit enable. Default OFF.
_ENABLED = os.environ.get("MTS_SPREAD_SHADOW_ENABLED", "").lower() in ("1", "true", "yes")


class SpreadShadowBridge:
    def __init__(self, near_code="TMFH6", far_code="TMFI6", session_id="",
                 telemetry_path=None, enabled=None):
        self.enabled = _ENABLED if enabled is None else bool(enabled)
        self._sync = SpreadSynchronizer(
            near_code=near_code, far_code=far_code, session_id=session_id,
        )
        self._collector = SpreadRenkoShadowCollector(telemetry_path=telemetry_path)
        self._collector.resume_from_disk()
        self.errors = 0

    def on_tick(self, leg, quote):
        """Called from the accepted-tick shadow path (after Session→Quote
        Integrity→Jump gates). Never raises into the trading loop."""
        if not self.enabled:
            return None
        try:
            sample = (self._sync.on_near(quote) if leg == "near"
                      else self._sync.on_far(quote))
            if sample is not None:
                self._collector.record(sample)
                return sample
        except Exception:
            self.errors += 1
        return None


# Module-level default bridge (lazy-init guard against heavy import cost).
_bridge = None


def get_bridge():
    global _bridge
    if _bridge is None:
        _bridge = SpreadShadowBridge()
    return _bridge
