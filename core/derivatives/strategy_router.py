"""
Strategy Regime Router — maps (skew_regime, underlying_trend) to strategy category.

Six categories from ref_250516.md Section 3:
1. Bullish (看漲) — Long Call, Bull Call Spread, Bull Put Spread
2. Bearish (看跌) — Long Put, Bear Put Spread, Bear Call Spread
3. Income/Yield (收益/收權) — CSP, Covered Call, Wheel
4. Hedging/Protection (保護/持倉) — Protective Put, Collar
5. Neutral/Range (震盪/區間) — Iron Condor, Butterfly
6. Volatility/Time (波動率/時間) — Straddle, Strangle, Calendar

Output: a recommendation category + suggested strategies.

Mapping Table (from ref_250516.md Section 3 + practical adjustments):
- LEFT_SKEW + trend=DOWN → BEARISH
- LEFT_SKEW + trend=NEUTRAL → HEDGING
- RIGHT_SKEW + trend=UP → BULLISH
- RIGHT_SKEW + trend=NEUTRAL → INCOME (sell puts into euphoria)
- PARALLEL + any trend → VOLATILITY (straddle/strangle for big move)
- NEUTRAL + any trend → RANGE (iron condor / butterfly)
- LEFT_SKEW + trend=UP → NEUTRAL (conflicting signals)
- RIGHT_SKEW + trend=DOWN → NEUTRAL (conflicting signals)
- UNKNOWN or None → UNKNOWN
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

STRATEGY_CATEGORIES = {
    "BULLISH": "看漲 — Long Call, Bull Call Spread, Bull Put Spread",
    "BEARISH": "看跌 — Long Put, Bear Put Spread, Bear Call Spread",
    "INCOME": "收益/收權 — Cash-Secured Put, Covered Call, Wheel",
    "HEDGING": "保護/持倉 — Protective Put, Collar",
    "RANGE": "震盪/區間 — Iron Condor, Butterfly, Jade Lizard",
    "VOLATILITY": "波動率/時間 — Straddle, Strangle, Calendar Spread",
    "UNKNOWN": "不確定 — 數據不足或信號衝突",
}


def route_strategy(
    skew_regime: Optional[dict],
    trend: str = "NEUTRAL",
) -> dict:
    """Route (skew_regime, trend) to a recommended strategy category.

    Parameters
    ----------
    skew_regime: dict from SkewRegime.to_dict(), or None
    trend: "UP" | "DOWN" | "NEUTRAL" — from underlying futures trend

    Returns
    -------
    dict with keys:
        category: str — one of STRATEGY_CATEGORIES keys
        description: str — Chinese description
        confidence: float — how well the input maps to this category
        reason: str — human-readable explanation
        suggested_strategies: list[str] — example strategies
    """
    shape = "UNKNOWN"
    confidence = 0.0
    if skew_regime is not None:
        shape = skew_regime.get("shape", "UNKNOWN")
        confidence = skew_regime.get("confidence", 0.0)

    trend_upper = trend.upper() if trend else "NEUTRAL"
    shape_upper = shape.upper()

    # ---- Decision matrix ----
    category = "UNKNOWN"
    reason = ""
    suggested = []

    if shape_upper == "LEFT_SKEW":
        if trend_upper == "DOWN":
            category = "BEARISH"
            reason = "左偏 IV (恐慌) + 下跌趨勢 → 強烈看空信號"
            suggested = ["Bear Put Spread", "Long Put"]
        elif trend_upper == "NEUTRAL":
            category = "HEDGING"
            reason = "左偏 IV (恐慌) + 橫盤 → 市場預期下跌風險，宜避險"
            suggested = ["Protective Put", "Collar"]
        else:  # UP
            category = "UNKNOWN"
            reason = "左偏 IV (恐慌) + 上漲趨勢 → 信號衝突，不建議進場"
            suggested = []

    elif shape_upper == "RIGHT_SKEW":
        if trend_upper == "UP":
            category = "BULLISH"
            reason = "右偏 IV (樂觀) + 上漲趨勢 → 強烈看漲信號"
            suggested = ["Bull Call Spread", "Long Call"]
        elif trend_upper == "NEUTRAL":
            category = "INCOME"
            reason = "右偏 IV (樂觀) + 橫盤 → 市場預期上漲，可賣 Put 收租"
            suggested = ["Cash-Secured Put (CSP)", "Wheel Strategy"]
        else:  # DOWN
            category = "UNKNOWN"
            reason = "右偏 IV (樂觀) + 下跌趨勢 → 信號衝突，不建議進場"
            suggested = []

    elif shape_upper == "PARALLEL":
        category = "VOLATILITY"
        reason = "IV 全面上升 → 市場預期大波動，適合做多波動率"
        suggested = ["Straddle", "Strangle", "Long Calendar Spread"]

    elif shape_upper == "NEUTRAL":
        category = "RANGE"
        reason = "IV 無異常 + 無明顯趨勢 → 適合區間策略"
        suggested = ["Iron Condor", "Iron Butterfly", "Short Strangle"]
    else:
        category = "UNKNOWN"
        reason = "IV 數據不足或無效"
        suggested = []

    # Cap confidence by category quality
    if category == "UNKNOWN":
        effective_confidence = 0.0
    elif category in ("BEARISH", "BULLISH"):
        # Directional alignment: highest confidence
        effective_confidence = min(confidence * 1.1, 1.0)
    else:
        effective_confidence = confidence

    return {
        "category": category,
        "description": STRATEGY_CATEGORIES.get(category, ""),
        "confidence": round(effective_confidence, 4),
        "reason": reason,
        "suggested_strategies": suggested,
        "input": {
            "skew_shape": shape_upper,
            "trend": trend_upper,
        },
    }
