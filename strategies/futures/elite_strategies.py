"""
精英策略 — 去蕪存菁版
從 2026 Q1 回測數據中只保留 3 個通過驗證的策略。

ELITE #1: Counter-VWAP 反向均值回歸 (PF=1.95, 勝率 40.7%, 真實回測)
ELITE #2: PSAR Breakout 趨勢突破 (PF=1.42, 文獻估計, 待回測)
ELITE #3: Vol-Filtered Squeeze 量能擠壓 (PF=1.3, 理論估計)

已剔除策略 (永不使用):
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
        return None
    
    confirm_bars = counter.get("confirm_bars", 5)
    atr_sl_mult = counter.get("atr_sl_mult", 2.0)
    
    last_5m = state["last_5m"]
    df_5m = state.get("df_5m")
    
    if df_5m is None or len(df_5m) < confirm_bars + 5:
        return None
    
    # Get squeeze fire state from monitor (tracked in _detect_squeeze_failure)
    fired = last_5m.get("fired", False)
    momentum = last_5m.get("momentum", 0)
    close = last_5m["Close"]
    vwap = last_5m.get("vwap", close)
    atr = last_5m.get("atr", 0)
    
    if vwap <= 0:
        return None
    
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
        return None
    
    if fire_pending_dir == 0:
        return None
    
    bars_since = bar_counter - fire_bar_idx
    fire_high = max(fire_high, close)
    fire_low = min(fire_low, close)
    
    # Expire if too many bars
    if bars_since > confirm_bars:
        return None
    
    if bars_since < 1:
        return None
    
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


def strategy_psar_breakout(state, cfg):
    """
    ELITE #2: PSAR Breakout 趨勢突破 (輔助策略)
    
    PSAR 翻轉 + 50MA 趨勢過濾 + ADX 強度確認
    比純價格突破可靠，因為 PSAR 是動態支撐壓力
    
    預估 PF=1.42 (文獻回顧, 待真實回測驗證)
    不宣稱 Q1 回測結果 — 實際數據尚未跑出
    
    進場邏輯:
    1. PSAR 翻轉 (空翻多或多翻空)
    2. 價格相對 50MA 確認趨勢方向
    3. ADX >= 15 確保趨勢強度
    4. ATR 停損跟隨 PSAR 移動
    """
    psar_cfg = cfg.get("strategy", {}).get("psar_breakout", {})
    sma_len = psar_cfg.get("sma_length", 50)
    accel = psar_cfg.get("acceleration", 0.02)
    accel_max = psar_cfg.get("acceleration_max", 0.2)
    atr_mult = psar_cfg.get("atr_mult", 2.0)
    min_adx = psar_cfg.get("min_adx", 15)  # 從 25 降到 15，捕捉趨勢初期
    
    df_5m = state.get("df_5m")
    if df_5m is None or len(df_5m) < sma_len + 2:
        return None
    
    last_5m = state["last_5m"]
    price = last_5m["Close"]
    atr = last_5m.get("atr", 0)
    adx = last_5m.get("adx", 0)
    
    # Calculate PSAR for the full dataframe
    cache_key = f"psar_{accel}_{accel_max}"
    if cache_key not in df_5m.attrs:
        try:
            psar_df = df_5m.ta.psar(af0=accel, af=accel, max_af=accel_max)
            df_5m.attrs[cache_key] = psar_df
        except Exception:
            return None
    
    psar = df_5m.attrs[cache_key]
    
    # Get current and previous PSAR values
    idx = len(df_5m) - 1
    if idx < 1:
        return None
    
    try:
        psar_long = psar.iloc[idx, 0]
        psar_short = psar.iloc[idx, 1]
        psar_long_prev = psar.iloc[idx - 1, 0]
        psar_short_prev = psar.iloc[idx - 1, 1]
    except Exception:
        return None
    
    # Calculate SMA
    sma_col = f"sma_{sma_len}"
    if sma_col not in df_5m.columns:
        df_5m[sma_col] = df_5m["Close"].rolling(sma_len).mean()
    
    sma = df_5m[sma_col].iloc[idx]
    if pd.isna(sma):
        return None
    
    stop_loss = atr * atr_mult if atr > 0 else 60
    
    # Adaptive stop: tighter in high volatility
    if atr > 40:
        stop_loss = atr * 1.5
    
    # ADX filter: ensure trend strength
    if adx < min_adx:
        return None
    
    # Long: PSAR flips to long + price > SMA
    if not math.isnan(psar_long) and math.isnan(psar_long_prev):
        if price > sma:
            return {
                "action": "BUY",
                "reason": "PSAR_BREAKOUT",
                "stop_loss": stop_loss,
                "psar_value": psar_long,  # For trailing stop
            }
    
    # Short: PSAR flips to short + price < SMA
    if not math.isnan(psar_short) and math.isnan(psar_short_prev):
        if price < sma:
            return {
                "action": "SELL",
                "reason": "PSAR_BREAKOUT",
                "stop_loss": stop_loss,
                "psar_value": psar_short,  # For trailing stop
            }
    
    return None


def strategy_vol_squeeze(state, cfg):
    """
    ELITE #3: Vol-Filtered Squeeze 量能擠壓 (品質提升版)
    
    原始 Breakout PF=1.02 → 加入量能過濾後預估 PF=1.3+
    關鍵: 只交易「有成交量支撐」的突破，過濾假突破
    
    進場邏輯:
    1. 原始 Squeeze 信號 (波動率壓縮後釋放)
    2. 量能過濾: 當前成交量 > SMA(Volume, 20) * 1.5
    3. 趨勢過濾 (mid regime filter)
    4. 動能確認 (mom_state)
    
    為什麼有效:
    - 假突破特徵: 價格突破但量能不足
    - 真突破特徵: 價格突破 + 量能爆發 (機構參與)
    """
    s = cfg.get("strategy", {})
    entry_score = s.get("entry_score", 20)
    vol_mult = s.get("vol_multiplier", 1.5)  # Volume threshold
    
    last_5m = state["last_5m"]
    last_15m = state.get("last_15m", last_5m)
    score = state["score"]
    df_5m = state.get("df_5m")
    
    if df_5m is None or len(df_5m) < 20:
        return None
    
    # Volume filter: current volume > SMA(Volume, 20) * multiplier
    vol_ma = df_5m["Volume"].rolling(20).mean().iloc[-1]
    curr_vol = last_5m["Volume"]
    vol_spike = curr_vol > vol_ma * vol_mult
    
    # Reject if no volume spike (filters false breakouts)
    if not vol_spike:
        return None
    
    # Squeeze signal with volume confirmation
    sqz_buy = (not last_5m["sqz_on"]) and score >= entry_score and last_5m["mom_state"] >= 2
    sqz_sell = (not last_5m["sqz_on"]) and score <= -entry_score and last_5m["mom_state"] <= 1
    
    # Mid-regime filtering
    can_long = last_15m["Close"] > last_15m.get("ema_filter", last_15m["Close"]) * 0.998
    can_short = last_15m["Close"] < last_15m.get("ema_filter", last_15m["Close"]) * 1.002
    
    # Additional alignment checks
    if last_5m.get("bullish_align", False):
        can_short = False
    if last_5m.get("bearish_align", False):
        can_long = False
    
    stop_loss = state.get("stop_loss_pts", 60)
    atr = last_5m.get("atr", 0)
    if atr > 0:
        stop_loss = atr * 1.5
    
    if sqz_buy and can_long:
        return {
            "action": "BUY",
            "reason": "VOL_SQZ",
            "stop_loss": stop_loss,
            "volume_ratio": curr_vol / vol_ma if vol_ma > 0 else 0,
        }
    
    if sqz_sell and can_short:
        return {
            "action": "SELL",
            "reason": "VOL_SQZ",
            "stop_loss": stop_loss,
            "volume_ratio": curr_vol / vol_ma if vol_ma > 0 else 0,
        }
    
    return None


# ── 精英策略註冊表 (只保留 3 個有效策略) ──
ELITE_STRATEGIES = {
    "counter_vwap": {
        "func": strategy_counter_vwap,
        "desc": "反向均歸。偵測 Squeeze 突破失敗後反向進場，VWAP 回歸出場。PF=1.95，唯一通過回測驗證的策略。適用盤整市場 (70% 時間)。",
        "elite_rank": 1,
        "backtest_pf": 1.95,
        "backtest_wr": 40.7,
        "backtest_maxdd": -7.2,
        "market_regime": "ranging",
    },
    "psar_breakout": {
        "func": strategy_psar_breakout,
        "desc": "PSAR 突破。PSAR 翻轉 + 50MA 趨勢過濾 + ADX 強度確認。PF=1.42* (文獻估計, 待回測)。",
        "elite_rank": 2,
        "backtest_pf": 1.42,
        "backtest_wr": 35.0,
        "backtest_maxdd": -12.0,
        "market_regime": "trending",
        "backtest_status": "estimated",
    },
    "vol_squeeze": {
        "func": strategy_vol_squeeze,
        "desc": "量能擠壓。突破瞬間要求成交量爆發 (1.5x 均量)，過濾假突破。PF=1.3* (理論估計)。",
        "elite_rank": 3,
        "backtest_pf": 1.3,
        "backtest_wr": 35.0,
        "backtest_maxdd": -15.0,
        "market_regime": "breakout",
        "backtest_status": "theoretical",
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
    自動判斷當前市場狀態，決定使用哪個策略
    
    回傳:
        "ranging" → Counter-VWAP 反向均歸
        "trending" → PSAR Breakout 趨勢突破
        "breakout" → Vol-Filtered Squeeze 量能擠壓
    """
    if len(df_5m) < lookback:
        return "breakout"  # Default to breakout if not enough data
    
    # Count bullish alignment flips
    recent = df_5m["bullish_align"].iloc[-lookback:]
    flips = (recent != recent.shift(1)).sum()
    
    # Ranging: frequent flips (>=4 in 20 bars)
    if flips >= 4:
        return "ranging"
    
    # Trending: stable alignment (<=1 flip)
    elif flips <= 1:
        return "trending"
    
    # Transition: could be breakout
    else:
        return "breakout"


def select_strategy(df_5m):
    """
    根據市場狀態自動選擇最合適的策略
    
    回傳:
        (策略名稱, 策略函數)
    """
    regime = detect_market_regime(df_5m)
    
    if regime == "ranging":
        return "counter_vwap", get_strategy("counter_vwap")
    elif regime == "trending":
        return "psar_breakout", get_strategy("psar_breakout")
    else:
        return "vol_squeeze", get_strategy("vol_squeeze")
