"""
ETF Regime Consumer — read market regime from etf_regime.json into trading decisions.

Provides threshold/size modifiers for ORB, VWAP, Scout/Scale strategies.
Does NOT generate trade signals — only adjusts existing strategy parameters.

Usage:
    from core.etf_regime_consumer import get_regime_adjustments
    adj = get_regime_adjustments()
    orb_threshold *= adj["orb_mult"]
    vwap_threshold *= adj["vwap_mult"]
    size_mult = adj["size_mult"]
"""

from __future__ import annotations

import logging
from typing import Any

from core.external_feature_provider import get_external_feature_provider

logger = logging.getLogger(__name__)

REGIME_KEYS = {"RISK_ON", "RISK_OFF", "DEFENSIVE", "CHOP"}

# ── Threshold / Size adjustments per regime ──
# These are small modifiers (±0.03~0.05, 0.5x~1.2x) as per spec:
#   "ETF regime 是環境濾鏡，不是進場訊號"
REGIME_ADJUSTMENTS: dict[str, dict[str, float]] = {
    "RISK_ON": {
        "orb_threshold_mult": 0.97,   # easier to break out
        "orb_size_mult": 1.2,          # larger scout/scale
        "vwap_threshold_mult": 1.03,   # wider VWAP bands
        "vwap_size_mult": 1.0,
        "size_mult": 1.2,              # overall position size
        "allow_scale": 1.0,            # full scale allowed
        "scout_only": 0.0,
    },
    "CHOP": {
        "orb_threshold_mult": 1.0,
        "orb_size_mult": 1.0,
        "vwap_threshold_mult": 0.97,   # tighter VWAP bands (mean reversion friendly)
        "vwap_size_mult": 1.1,
        "size_mult": 1.0,
        "allow_scale": 0.0,            # scout only in chop
        "scout_only": 1.0,
    },
    "DEFENSIVE": {
        "orb_threshold_mult": 1.05,    # harder to break out
        "orb_size_mult": 0.5,
        "vwap_threshold_mult": 0.95,   # tighter bands
        "vwap_size_mult": 1.1,
        "size_mult": 0.7,
        "allow_scale": 0.0,
        "scout_only": 1.0,
    },
    "RISK_OFF": {
        "orb_threshold_mult": 1.05,
        "orb_size_mult": 0.5,
        "vwap_threshold_mult": 1.0,
        "vwap_size_mult": 0.5,
        "size_mult": 0.5,
        "allow_scale": 0.0,
        "scout_only": 1.0,
    },
}

DEFAULT_ADJUSTMENTS = {
    "orb_threshold_mult": 1.0,
    "orb_size_mult": 1.0,
    "vwap_threshold_mult": 1.0,
    "vwap_size_mult": 1.0,
    "size_mult": 1.0,
    "allow_scale": 1.0,
    "scout_only": 0.0,
}


def fetch_regime_data() -> dict[str, Any]:
    """Fetch ETF regime from the external feature provider.

    Returns raw regime payload. Never raises — falls back to CHOP/0.0 confidence.
    """
    try:
        provider = get_external_feature_provider()
        return provider.get_regime()
    except Exception as exc:
        logger.error("[Regime] failed to get provider: %s", exc)
        return {"regime": "CHOP", "confidence": 0.0, "degraded": True}


def get_regime_adjustments(regime_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute threshold/size adjustments from ETF regime data.

    Args:
        regime_data: Raw regime payload (from fetch_regime_data or cached).
            If None, fetches fresh data.

    Returns:
        Dict with orb_threshold_mult, orb_size_mult, vwap_threshold_mult,
        vwap_size_mult, size_mult, allow_scale, scout_only, plus metadata.
    """
    if regime_data is None:
        regime_data = fetch_regime_data()

    regime = str(regime_data.get("regime", "CHOP"))
    confidence = float(regime_data.get("confidence", 0.0))
    degraded = bool(regime_data.get("degraded", False))

    if regime not in REGIME_KEYS:
        logger.warning("[Regime] unknown regime '%s', defaulting to CHOP", regime)
        regime = "CHOP"

    adj = dict(REGIME_ADJUSTMENTS.get(regime, DEFAULT_ADJUSTMENTS))

    # Degrade: if stale/degraded, pull adjustments toward neutral
    if degraded or confidence < 0.3:
        for key in ("orb_size_mult", "vwap_size_mult", "size_mult"):
            adj[key] = 1.0 + (adj[key] - 1.0) * 0.5  # halve the adjustment
        adj["allow_scale"] = min(adj["allow_scale"], 0.5)
        logger.info(
            "[Regime] degraded (conf=%.2f degraded=%s), adjustments halved toward neutral",
            confidence, degraded,
        )

    adj["regime"] = regime
    adj["confidence"] = confidence
    adj["degraded"] = degraded

    logger.debug(
        "[Regime] adjustments: regime=%s conf=%.2f orb_th=%.2f orb_sz=%.2f "
        "vwap_th=%.2f vwap_sz=%.2f size=%.2f scale=%s scout=%s",
        regime, confidence,
        adj["orb_threshold_mult"], adj["orb_size_mult"],
        adj["vwap_threshold_mult"], adj["vwap_size_mult"],
        adj["size_mult"], adj["allow_scale"], adj["scout_only"],
    )
    return adj
