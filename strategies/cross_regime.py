import numpy as np
import time

class RegimeDetector:
    def __init__(self, smoothing=3):
        self.smoothing = smoothing
        self._last_slope = 0.0

    def detect(self, bars, slope_trend=0.8, low_vol_th=5.0):
        if not bars or len(bars) < 20:
            return "UNKNOWN"
        
        closes = np.array([b.get("close", 0) for b in bars[-20:]], dtype=float)
        highs = np.array([b.get("high", 0) for b in bars[-20:]], dtype=float)
        lows = np.array([b.get("low", 0) for b in bars[-20:]], dtype=float)
        
        # Volatility check
        vol = float(np.mean(highs - lows)) if len(highs) > 0 else 0.0
        if vol < low_vol_th:
            return "LOW_VOL"

        # Linear regression for slope and R-squared
        x = np.arange(len(closes))
        try:
            # Simple linear regression: y = mx + c
            A = np.vstack([x, np.ones(len(x))]).T
            m, c = np.linalg.lstsq(A, closes, rcond=None)[0]
            
            # Calculate R-squared (coefficient of determination)
            y_pred = m * x + c
            y_mean = np.mean(closes)
            ss_res = np.sum((closes - y_pred)**2)
            ss_tot = np.sum((closes - y_mean)**2)
            r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
            
            slope = float(m)
        except Exception:
            slope = 0.0
            r_squared = 0.0

        # GSD: Only accept trend if it's statistically significant (R^2 > 0.25)
        # This prevents "noisy" trends where price flips around but happens to have a slope.
        if r_squared < 0.25:
            return "CHOP"

        if slope > slope_trend:
            return "TREND_UP"
        if slope < -slope_trend:
            return "TREND_DOWN"
        return "CHOP"

class TMFLocalDetector:
    def __init__(self):
        pass

    def detect(self, bars):
        if not bars or len(bars) < 20:
            return "UNKNOWN"
        closes = np.array([b.get("close", 0) for b in bars[-20:]], dtype=float)
        highs = np.array([b.get("high", 0) for b in bars[-20:]], dtype=float)
        lows = np.array([b.get("low", 0) for b in bars[-20:]], dtype=float)
        recent_range = float(np.mean(highs[-5:] - lows[-5:])) if len(highs) >= 5 else float(np.mean(highs - lows))
        full_range = float(np.mean(highs - lows)) if len(highs) > 0 else 0.0
        last_close = float(closes[-1])
        recent_high = float(np.max(highs[-5:])) if len(highs) >= 5 else float(np.max(highs))
        recent_low = float(np.min(lows[-5:])) if len(lows) >= 5 else float(np.min(lows))
        if full_range < 5:
            return "STALLED"
        if last_close >= recent_high * 0.999 or last_close <= recent_low * 1.001:
            return "BREAKOUT_READY"
        return "MEAN_REVERT"

class CrossRegimeEngine:
    def __init__(self):
        pass

    def decide(self, tx_regime, tmf_regime, tx_fresh=True, tmf_fresh=True):
        result = {
            "allow_trade": False,
            "orb_weight": 0.0,
            "vwap_weight": 0.0,
            "orb_threshold": 0.60,
            "vwap_threshold": 0.80,
            "tx_regime": tx_regime,
            "tmf_regime": tmf_regime,
            "reason": "DEFAULT_BLOCK",
        }
        # Freshness gating
        if not tx_fresh:
            result.update({"reason": "TX_STALE"})
            return result
        if not tmf_fresh:
            result.update({"reason": "TMF_STALE"})
            return result

        if tx_regime in ("UNKNOWN", "LOW_VOL") or tmf_regime in ("UNKNOWN", "STALLED"):
            result.update({"reason": "NO_EDGE"})
            return result
        if tx_regime in ("TREND_UP", "TREND_DOWN") and tmf_regime == "BREAKOUT_READY":
            result.update({
                "allow_trade": True,
                "orb_weight": 1.0,
                "vwap_weight": 0.2,
                "orb_threshold": 0.50,
                "vwap_threshold": 0.95,
                "reason": "TREND_BREAKOUT",
            })
            return result
        if tx_regime == "CHOP" and tmf_regime == "BREAKOUT_READY":
            result.update({
                "allow_trade": True,
                "orb_weight": 0.4,
                "vwap_weight": 0.8,
                "orb_threshold": 0.72,
                "vwap_threshold": 0.75,
                "reason": "CHOP_FADE_BREAKOUT",
            })
            return result
        if tx_regime == "CHOP" and tmf_regime == "MEAN_REVERT":
            result.update({
                "allow_trade": True,
                "orb_weight": 0.2,
                "vwap_weight": 1.0,
                "orb_threshold": 0.90,
                "vwap_threshold": 0.65,
                "reason": "CHOP_MEAN_REVERT",
            })
            return result
        return result


class TxBarBuilder:
    def __init__(self, timeframe="5min", max_bars=300):
        self.timeframe = timeframe
        self.max_bars = max_bars
        self._current_bar = {"ts": None, "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0}
        self._bars = []
        self.last_tick_time = 0.0

    def on_tick(self, tick):
        try:
            price = float(getattr(tick, 'close', 0.0))
            vol = int(getattr(tick, 'volume', 1) or 1)
            ts = getattr(tick, 'datetime', None)
            if ts is None:
                return
            import pandas as pd
            bucket = pd.Timestamp(ts).floor(self.timeframe)
            bar = self._current_bar
            self.last_tick_time = time.time()
            if bar["ts"] is None or bucket != bar["ts"]:
                if bar["ts"] is not None and bar["open"] > 0:
                    self._bars.append(dict(bar))
                    if len(self._bars) > self.max_bars:
                        self._bars = self._bars[-self.max_bars :]
                self._current_bar = {
                    "ts": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": vol,
                }
                return
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["volume"] += vol
        except Exception:
            return

    def bars(self):
        out = list(self._bars)
        if self._current_bar["ts"] is not None and self._current_bar["open"] > 0:
            out.append(dict(self._current_bar))
        return out
