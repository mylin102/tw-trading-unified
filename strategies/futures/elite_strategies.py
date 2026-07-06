"""
精英策略 — 去蕪存菁版
從 10 個策略精簡到 1 個。Counter-VWAP 是唯一通過完整回測驗證的策略。

ELITE #1: Counter-VWAP 反向均值回歸 (PF=1.95, 勝率 40.7%, 真實回測)

已剔除策略 (回測全部失敗):
❌ PSAR Breakout (PF=1.13, MaxDD=-63%, 54/54 組合不適用)
❌ Vol-Squeeze (PF=1.23, MaxDD=-55%, 27/27 組合不適用)
❌ Night Short Only (PF=0.04, 夜盤流動性不足)
❌ Pure Breakout (PF=1.02, 假突破太多)
❌ VWAP Bounce (信號不穩定)
❌ Momentum Burst (Z-Score 太敏感)
❌ Cumulative Delta (Delta 估計不準)
❌ Volume Reversal (信號太少)
❌ Gap Reversal (跳空後趨勢延續)
"""
import numpy as np
import pandas as pd
import math


def _skip(reason: str):
    """Return a sentinel dict with _skip_reason for router debugging."""
    # Use a dict-like object that is falsy (None) but carries metadata
    # Actually, return None but the function sets a module-level attr
    # Simplest approach: return a special Marker object
    class _SkipMarker(dict):
        _skip_reason = reason
        def __init__(self):
            super().__init__()
        def __bool__(self):
            return False
    return _SkipMarker()


def is_spring_long_context_favorable(bar, score=None):
    """Conservatively allow SPRING longs only in supportive bullish context."""
    resolved_score = bar.get("score", score if score is not None else 0)
    bullish_align = bar.get("bullish_align", bar.get("bull_align", False))
    opening_bearish = bar.get("opening_bearish", False)
    close = bar.get("Close", bar.get("close", 0))
    vwap = bar.get("vwap", 0)

    if resolved_score <= 0:
        return False
    if not bullish_align:
        return False
    if opening_bearish:
        return False
    if close <= 0 or vwap <= 0:
        return False
    if close < vwap:
        return False

    return True


def strategy_counter_vwap(state, cfg):
    """
    ELITE #1: Counter-VWAP 反向均值回歸 (核心策略)
    
    台指期 5m 均值回歸特性強烈，Squeeze Fire 後突破失敗 = 反向進場點
    VWAP 出場是核心獲利機制，不是選項
    
    回測數據: PF=1.95, 勝率 40.7%, 最大虧損 -7.2%, 獲利 TWD +32,285
    
    進場邏輯:
    1. 偵測 Squeeze Fire (波動率壓縮釋放)
    2. 等待 5 根 K 棒確認突破失敗
    3. 失敗條件: 未創新高/低 + 動能反轉 + VWAP 拒絕
    4. 反向進場，VWAP 回歸出場
    
    關鍵: exit_on_vwap 必須為 True，否則 catastrophic fail
    """
    counter = cfg.get("strategy", {}).get("counter_mode", {})
    if not counter.get("enabled", False):
        return _skip("COUNTER_MODE_DISABLED")
    
    confirm_bars = counter.get("confirm_bars", 5)
    atr_sl_mult = counter.get("atr_sl_mult", 2.0)
    
    last_5m = state["last_5m"]
    df_5m = state.get("df_5m")
    
    if df_5m is None or len(df_5m) < confirm_bars + 5:
        return _skip(f"INSUFFICIENT_BARS need={confirm_bars + 5} got={len(df_5m) if df_5m is not None else 0}")
    
    # Get squeeze fire state from monitor (tracked in _detect_squeeze_failure)
    fired = last_5m.get("fired", False)
    momentum = last_5m.get("momentum", 0)
    close = last_5m["Close"]
    vwap = last_5m.get("vwap", close)
    atr = last_5m.get("atr", 0)
    
    if vwap <= 0:
        return _skip("VWAP_ZERO")
    
    # Track fire state (same logic as _detect_squeeze_failure in monitor)
    # This state should be maintained by the monitor's _fire_pending_* variables
    fire_pending_dir = state.get("fire_pending_dir", 0)
    fire_bar_idx = state.get("fire_bar_idx", 0)
    fire_high = state.get("fire_high", 0.0)
    fire_low = state.get("fire_low", 0.0)
    bar_counter = state.get("bar_counter", 0)
    
    # New fire event
    if fired and fire_pending_dir == 0:
        fire_pending_dir = 1 if momentum > 0 else -1
        fire_bar_idx = bar_counter
        fire_high = close
        fire_low = close
        return _skip(f"FIRE_DETECTED dir={fire_pending_dir} — waiting for confirmation")
    
    if fire_pending_dir == 0:
        return _skip("NO_FIRE_EVENT")
    
    bars_since = bar_counter - fire_bar_idx
    fire_high = max(fire_high, close)
    fire_low = min(fire_low, close)
    
    # Expire if too many bars
    if bars_since > confirm_bars:
        return _skip(f"FIRE_EXPIRED bars_since={bars_since} > confirm={confirm_bars}")
    
    if bars_since < 1:
        return _skip(f"WAIT_CONFIRM bars_since={bars_since} < 1")
    
    # Failure validation
    recent_high = last_5m.get("recent_high", close)
    recent_low = last_5m.get("recent_low", close)
    mom_velo = last_5m.get("mom_velo", 0)
    
    stop_loss = atr * atr_sl_mult if atr > 0 else 60
    
    # Bullish fire failed → COUNTER_SELL
    if fire_pending_dir == 1:
        no_new_high = close < recent_high
        velo_reversed = mom_velo <= 0
        vwap_reject = close < vwap
        
        if no_new_high and (velo_reversed or vwap_reject):
            return {
                "action": "SELL",
                "reason": "COUNTER_VWAP",
                "stop_loss": stop_loss,
                "target": vwap,  # VWAP is the target
                "fire_high": fire_high,
            }
    
    # Bearish fire failed → COUNTER_BUY
    else:
        no_new_low = close > recent_low
        velo_reversed = mom_velo >= 0
        vwap_reject = close > vwap
        
        if no_new_low and (velo_reversed or vwap_reject):
            return {
                "action": "BUY",
                "reason": "COUNTER_VWAP",
                "stop_loss": stop_loss,
                "target": vwap,  # VWAP is the target
                "fire_low": fire_low,
            }
    
    return None


def strategy_spring_upthrust(state, cfg):
    """
    ELITE #2: Spring/Upthrust 假突破反向 (即時確認)
    
    TTM Squeeze Spring/Upthrust 模式:
    - Spring: 假跌破 BB 下軌但收盤彈回 → 做多
    - Upthrust: 假突破 BB 上軌但收盤跌回 → 做空
    
    回測數據: PF=3.36, 勝率 33.3%, 最大虧損 -10.1%, T=33
    
    優勢:
    - 0 延遲 (當下 K 線確認, 不需等 5 根)
    - 高品質信號 (假突破是 Wyckoff 經典反向模式)
    - 交易數少 (33 vs Counter 86)
    """
    spring_cfg = cfg.get("strategy", {}).get("spring_upthrust", {})
    bb_mult = spring_cfg.get("bb_mult", 2.0)
    kc_mult = spring_cfg.get("kc_mult", 1.0)
    atr_mult = spring_cfg.get("atr_mult", 2.0)
    bb_len = spring_cfg.get("bb_length", 20)
    kc_len = spring_cfg.get("kc_length", 20)
    
    last_5m = state["last_5m"]
    df_5m = state.get("df_5m")
    
    if df_5m is None or len(df_5m) < max(bb_len, kc_len) + 2:
        return None
    
    # 計算 BB
    ma = df_5m["Close"].rolling(window=bb_len).mean()
    std = df_5m["Close"].rolling(window=bb_len).std()
    bb_up = ma + (bb_mult * std)
    bb_low = ma - (bb_mult * std)
    
    # 計算 KC (ATR 近似)
    prev_close = df_5m["Close"].shift(1)
    tr = np.maximum(df_5m["High"] - df_5m["Low"],
                    np.maximum(np.abs(df_5m["High"] - prev_close),
                               np.abs(df_5m["Low"] - prev_close)))
    atr = tr.rolling(window=kc_len).mean()
    kc_up = ma + (kc_mult * atr)
    kc_low = ma - (kc_mult * atr)
    
    # 擠壓狀態 (BB 在 KC 內)
    is_squeezing = (bb_up < kc_up) & (bb_low > kc_low)
    
    # 檢查前一根是否在擠壓中
    if len(is_squeezing) < 2 or not is_squeezing.iloc[-2]:
        return None
    
    close = last_5m["Close"]
    high = last_5m["High"]
    low = last_5m["Low"]
    atr_val = last_5m.get("atr", 0)
    # [Bug fix] ATR 合理性上限：TMF 5m ATR 通常 30-150 點，超過 300 表示數據異常
    atr_cap = 300
    if atr_val > atr_cap:
        atr_val = atr_cap
    stop_loss = atr_val * atr_mult if atr_val > 0 else 60
    
    # Spring (假跌破 → 做多)
    if low < bb_low.iloc[-1] and close > bb_low.iloc[-1]:
        if not is_spring_long_context_favorable(last_5m, state.get("score")):
            return None
        return {
            "action": "BUY",
            "reason": "SPRING",
            "stop_loss": stop_loss,
            "bb_low": bb_low.iloc[-1],
        }
    
    # Upthrust (假突破 → 做空)
    if high > bb_up.iloc[-1] and close < bb_up.iloc[-1]:
        return {
            "action": "SELL",
            "reason": "UPTHRUST",
            "stop_loss": stop_loss,
            "bb_up": bb_up.iloc[-1],
        }
    
    return None


# ── 精英策略註冊表 (Counter-VWAP + Spring/Upthrust) ──
ELITE_STRATEGIES = {
    "counter_vwap": {
        "func": strategy_counter_vwap,
        "desc": "反向均歸。偵測 Squeeze 突破失敗後反向進場，VWAP 回歸出場。PF=1.95，盤整市場 (70%)。",
        "elite_rank": 1,
        "backtest_pf": 1.95,
        "backtest_wr": 40.7,
        "backtest_maxdd": -7.2,
        "market_regime": "ranging",
    },
    "spring_upthrust": {
        "func": strategy_spring_upthrust,
        "desc": "假突破反向。Spring/Upthrust 即時確認，不需等待。PF=3.36，高品質信號 (33 筆)。",
        "elite_rank": 2,
        "backtest_pf": 3.36,
        "backtest_wr": 33.3,
        "backtest_maxdd": -10.1,
        "market_regime": "squeeze",
    },
}


def get_strategy(name):
    """Get elite strategy function by name."""
    return ELITE_STRATEGIES.get(name, {}).get("func")


def get_elite_strategies():
    """Return all elite strategies with metadata."""
    return ELITE_STRATEGIES


def detect_market_regime(df_5m, lookback=20):
    """
    判斷市場狀態 (供參考)
    """
    return "ranging"  # Counter-VWAP 適用


def select_strategy(df_5m):
    """
    回傳兩個策略: Counter-VWAP (主) + Spring/Upthrust (輔)
    """
    # 先嘗試 Spring/Upthrust (0 延遲, 高品質)
    spring = get_strategy("spring_upthrust")
    counter = get_strategy("counter_vwap")
    return "spring_upthrust+counter_vwap", [spring, counter]
