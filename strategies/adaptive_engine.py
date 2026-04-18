import numpy as np

class AdaptiveEngine:
    def __init__(self):
        self.regime = "UNKNOWN"

    def detect_regime(self, bars):
        """
        bars: list of dict with keys 'close','high','low'
        """
        if not bars or len(bars) < 20:
            return "UNKNOWN"

        closes = np.array([b.get("close", 0) for b in bars[-20:]], dtype=float)
        highs = np.array([b.get("high", 0) for b in bars[-20:]], dtype=float)
        lows = np.array([b.get("low", 0) for b in bars[-20:]], dtype=float)

        # volatility = mean high-low
        volatility = float(np.mean(highs - lows)) if len(highs) > 0 else 0.0

        # simple slope as trend proxy
        x = np.arange(len(closes))
        try:
            slope = float(np.polyfit(x, closes, 1)[0])
        except Exception:
            slope = 0.0

        if volatility < 5:
            self.regime = "LOW_VOL"
        elif abs(slope) > 0.8:
            self.regime = "TREND"
        else:
            self.regime = "MEAN_REVERT"

        return self.regime

    def adjust_threshold(self, base_orb, base_vwap, bars):
        """Return (orb_th, vwap_th) adjusted by recent volatility"""
        if not bars or len(bars) < 5:
            return base_orb, base_vwap
        highs = np.array([b.get("high", 0) for b in bars[-20:]], dtype=float)
        lows = np.array([b.get("low", 0) for b in bars[-20:]], dtype=float)
        volatility = float(np.mean(highs - lows)) if len(highs) > 0 else 0.0

        vol_factor = min(max(volatility / 10.0, 0.5), 2.0)

        orb = float(base_orb) * (1.0 / vol_factor)
        vwap = float(base_vwap) * vol_factor

        return orb, vwap

    def strategy_weight(self):
        if self.regime == "TREND":
            return 1.0, 0.3
        elif self.regime == "MEAN_REVERT":
            return 0.4, 1.0
        elif self.regime == "LOW_VOL":
            return 0.0, 0.0
        else:
            return 0.5, 0.5
