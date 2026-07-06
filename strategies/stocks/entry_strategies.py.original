"""
Pluggable entry strategies for StockMonitor.
Focuses on Odd-Lot specific behaviors like Mean Reversion and Arbitrage.
"""

def strategy_stock_mean_reversion(state, cfg):
    """均值回歸：跌破布林下軌買入，回升至中軌賣出。"""
    df = state["df_5m"]
    if len(df) < 20:
        return None
    
    last = df.iloc[-1]
    # 假設指標已在 scanner 中算出
    if last["Close"] < last.get("bb_lower", 0):
        return {"action": "BUY", "reason": "BB_LOWER_BOUNCE", "stop_loss": last["Close"] * 0.97}
    return None

def strategy_stock_arbitrage(state, cfg):
    """整零價差監控：當零股折價超過 0.5% 時買入。"""
    last_5m = state["last_5m"]
    price_odd = last_5m["Close"]
    price_round = last_5m.get("price_round", 0) # 需要從 snapshot 注入
    
    if price_round > 0 and price_odd < price_round * 0.995:
        return {"action": "BUY", "reason": "ODD_DISCOUNT", "stop_loss": price_odd * 0.98}
    return None

def strategy_stock_momentum(state, cfg):
    """
    動能突破（帶量版）：突破今日高點且漲幅 > 2%。
    第一招：當前成交量必須 ≥ 過去 20 bars 平均量的 2 倍。
    """
    last_5m = state["last_5m"]
    df = state["df_5m"]
    day_open = last_5m.get("day_open", 0)
    if day_open == 0 or len(df) < 20:
        return None

    # 帶量確認
    vol_avg = df["Volume"].iloc[-21:-1].mean()
    if vol_avg <= 0 or last_5m["Volume"] < vol_avg * 2:
        return None

    if last_5m["Close"] > day_open * 1.02 and last_5m.get("is_new_high", False):
        return {"action": "BUY", "reason": "MOM_BREAKOUT_VOL", "stop_loss": last_5m["Close"] * 0.985}
    return None
def strategy_stock_scout(state, cfg):
    """
    零股偵察兵 (防禦加強版)：
    1. SCOUT: Squeeze Fired + 成交量放大 1.5 倍 + 大盤非空頭。
    2. SCALE: 獲利 > 1% 且動能持續。
    """
    last_5m = state["last_5m"]
    df = state["df_5m"]

    stage = state.get("scout_stage", "IDLE")
    entry_price = state.get("scout_entry_price", 0)
    curr_price = last_5m["Close"]

    # 階段 1: 尋找試單機會
    if stage == "IDLE":
        # 防禦 A: 成交量確認 (Volume Confirmation)
        vol_avg = df["Volume"].rolling(20).mean().iloc[-1]
        vol_spike = last_5m["Volume"] > (vol_avg * 1.5)

        # 防禦 B: 大盤濾網
        market_safe = state.get("market_trend", "BULL") != "BEAR"

        # 防禦 C: MACD 動能確認 (新增)
        macd_confirmed = last_5m.get("macd_rising", True)

        if last_5m["fired"] and last_5m["mom_state"] >= 2 and vol_spike and market_safe and macd_confirmed:
            return {"action": "BUY", "reason": "SCOUT_CONFIRMED", "qty_mode": "SCOUT", "stop_loss": curr_price * 0.985}


    # 階段 2: 已持倉，檢查是否加碼
    elif stage == "SCOUT":
        profit_pct = (curr_price - entry_price) / entry_price
        # 獲利確認，觸發加碼 (Scale to Main Force)
        if profit_pct >= 0.01 and last_5m["mom_state"] == 3:
            return {"action": "BUY", "reason": "SCOUT_SCALE", "qty_mode": "MAIN", "stop_loss": curr_price * 0.98}
            
    return None

def strategy_kd_mean_reversion(state, cfg):
    """
    KD 超賣均值回歸 + ADX 趨勢過濾。
    研究顯示 RSI/KD 超賣 + 趨勢過濾器的均值回歸策略勝率可達 70%+。
    進場：K<20 超賣 + ADX<30 (非強趨勢，適合回歸) + 收在 EMA200 之上 (長期多頭)
    出場：K>70 超買區
    """
    last = state["last_5m"]
    k_val = last.get("k_val", 50)
    adx = last.get("adx", 0)
    ema_200_up = last.get("ema_200_up", False)

    if k_val < 20 and adx < 30 and ema_200_up:
        sl = last["Close"] * (1 - cfg.get("stop_loss_pct", 0.03))
        return {"action": "BUY", "reason": "KD_OVERSOLD_REVERT", "stop_loss": sl}
    return None


def strategy_bb_bounce(state, cfg):
    """
    布林帶下軌反彈 + MACD 動能確認。
    價格觸及 BB 下軌時，若 MACD histogram 開始翻正（動能回升），進場做多。
    比純 BB 策略多了動能確認，減少在下跌趨勢中接刀。
    """
    last = state["last_5m"]
    df = state["df_5m"]
    if len(df) < 3:
        return None

    bb_lower = last.get("bb_lower", 0)
    macd_hist = last.get("macd_hist", 0)
    macd_prev = df["macd_hist"].iloc[-2] if "macd_hist" in df.columns else 0

    # 價格在 BB 下軌附近 (1% 以內) + MACD histogram 由負轉正
    if bb_lower > 0 and last["Close"] <= bb_lower * 1.01 and macd_hist > macd_prev and macd_prev < 0:
        sl = last["Close"] * (1 - cfg.get("stop_loss_pct", 0.03))
        return {"action": "BUY", "reason": "BB_LOWER_MACD_CROSS", "stop_loss": sl}
    return None


def strategy_ema_pullback(state, cfg):
    """
    EMA 回踩策略：趨勢中的回調買入。
    條件：多頭排列 (bullish_align) + 價格回踩到 EMA slow 附近 + KD 未超買 + ADX>20 確認趨勢存在。
    這是經典的「趨勢回調」策略，在確認的上升趨勢中買回調。
    """
    last = state["last_5m"]
    ema_slow = last.get("ema_slow", 0)
    if ema_slow <= 0:
        return None

    bullish = last.get("bullish_align", False)
    k_val = last.get("k_val", 50)
    adx = last.get("adx", 0)
    close = last["Close"]

    # 多頭排列 + 回踩 EMA slow (在 0.5% 範圍內) + KD 不在超買 + ADX 確認趨勢
    if bullish and adx > 20 and k_val < 70 and abs(close - ema_slow) / ema_slow < 0.005:
        sl = close * (1 - cfg.get("stop_loss_pct", 0.03))
        return {"action": "BUY", "reason": "EMA_PULLBACK", "stop_loss": sl}
    return None


def strategy_fakeout_reversal(state, cfg):
    """
    假突破反向操作（Squeeze Fire Failure Counter-Trade）。
    邏輯：壓縮釋放後價格未能延續方向，反向操作。
    - Bullish fire (sqz_on→False + momentum>0) 但價格 < VWAP → 做空
    - Bearish fire (sqz_on→False + momentum<0) 但價格 > VWAP → 做多

    類似期貨 Counter-VWAP 策略，但適用於股票。
    """
    last = state["last_5m"]
    df = state["df_5m"]
    if len(df) < 5:
        return None

    prev = df.iloc[-2]
    vwap = last.get("vwap", last["Close"])
    close = last["Close"]
    momentum = last.get("momentum", 0)
    prev_momentum = prev.get("momentum", 0)

    # 偵測 fire: 壓縮剛釋放
    fired = last.get("fired", False)
    was_squeezing = prev.get("sqz_on", False)
    is_release = fired and was_squeezing

    if not is_release:
        return None

    # Bullish fire 但價格在 VWAP 下方 → 反向做多 (bullish fire failure)
    if momentum > 0 and prev_momentum <= 0 and close < vwap:
        sl = close * (1 - cfg.get("stop_loss_pct", 0.03))
        return {"action": "BUY", "reason": "FAKEOUT_BULL_FAILURE", "stop_loss": sl}

    # Bearish fire 但價格在 VWAP 上方 → 反向做多 (bearish fire failure)
    if momentum < 0 and prev_momentum >= 0 and close > vwap:
        sl = close * (1 - cfg.get("stop_loss_pct", 0.03))
        return {"action": "BUY", "reason": "FAKEOUT_BEAR_FAILURE", "stop_loss": sl}

    return None


def strategy_it_window_dressing(state, cfg):
    """
    投信作帳波段策略。
    邏輯：投信連續 3 天買超 + 多頭排列 (Close > MA20 > MA60)。
    這是一個典型的籌碼面濾網搭配趨勢面進場的策略。
    """
    last_5m = state["last_5m"]
    df_5m = state["df_5m"]

    if len(df_5m) < 60:  # 確保有足夠均線計算空間
        return None

    # 1. 均線過濾 (均線通常在 scanner 中預先算出)
    ma20 = last_5m.get("ma20", 0)
    ma60 = last_5m.get("ma60", 0)
    close = last_5m["Close"] if "Close" in last_5m else last_5m.get("close", 0)

    if not (close > ma20 > ma60):
        return None

    # 2. 籌碼過濾 (動能代理)
    # 優化：不再要求絕對連三根，改為過去 5 根中有 2 根符合機構買盤特徵
    it_hits = last_5m.get("it_buy_rolling_count", 0)

    if it_hits < 2:
        return None

    sl = close * (1 - cfg.get("stop_loss_pct", 0.05)) # 波段策略給予較大空間 5%
    return {
        "action": "BUY",
        "reason": "IT_3DAY_BUY_BULLISH_ALIGN",
        "stop_loss": sl
    }


def strategy_stock_canslim_breakout(state, cfg):
    """
    CANSLIM 突破策略：
    1. 形態確認：Scanner 已標註為 CUP_WITH_HANDLE 且有 pivot_price。
    2. 價格突破：當前價格 > pivot_price。
    3. 成交量爆發：當前成交量 > 20日均量 * 1.4 倍。
    4. 大盤過濾：TMF 指標顯示非強空頭 (由 state 傳入)。
    """
    last_5m = state["last_5m"]
    df = state["df_5m"]
    pivot_price = state.get("pivot", 0.0)
    
    if pivot_price <= 0:
        return None

    # 1. 價格突破檢查
    if last_5m["Close"] <= pivot_price:
        return None

    # 2. 成交量噴發檢查 (GSD: Volume Confirmation)
    vol_avg = df["Volume"].rolling(20).mean().iloc[-1]
    vol_mult = cfg.get("stocks", {}).get("canslim", {}).get("volume_breakout_mult", 1.4)
    if last_5m["Volume"] < vol_avg * vol_mult:
        return None

    # 3. 大盤方向過濾 (Market Direction - M)
    if cfg.get("stocks", {}).get("canslim", {}).get("market_direction_filter", True):
        if state.get("market_trend") == "BEAR":
            return None

    return {
        "action": "BUY", 
        "reason": f"CANSLIM_BREAKOUT_PIVOT_{pivot_price}", 
        "qty_mode": "MAIN",
        "stop_loss": pivot_price * 0.93  # 突破失敗止損設在 pivot 下方 7%
    }

STOCK_STRATEGIES = {
    "canslim_breakout": {
        "func": strategy_stock_canslim_breakout,
        "desc": "CANSLIM 突破。杯中帶把/雙底型態，帶量突破 Pivot 點時進場。"
    },
    "it_window_dressing": {
        "func": strategy_it_window_dressing,
        "desc": "投信作帳波段。投信連三買 + 多頭排列，跟隨法人建倉波段。"
    },
    "mean_reversion": {
        "func": strategy_stock_mean_reversion,
        "desc": "均值回歸。跌破布林下軌時買入，捕捉超跌反彈。"
    },
    "arbitrage_lite": {
        "func": strategy_stock_arbitrage,
        "desc": "整零套利。當零股價格低於整股 0.8% 以上且整股趨勢穩定時進場。"
    },
    "momentum_breakout": {
        "func": strategy_stock_momentum,
        "desc": "動能突破。追蹤當日強勢股，突破高點且漲幅 > 2% 時跟進。"
    },
    "scout_strategy": {
        "func": strategy_stock_scout,
        "desc": "零股偵察兵。先以極小量零股試單，獲利 > 1% 確認趨勢後再大額加碼整股。"
    },
    "kd_mean_reversion": {
        "func": strategy_kd_mean_reversion,
        "desc": "KD超賣均值回歸。K<20超賣+ADX<30非強趨勢+EMA200多頭，捕捉超跌反彈。"
    },
    "bb_bounce": {
        "func": strategy_bb_bounce,
        "desc": "布林下軌反彈。價格觸及BB下軌+MACD histogram翻正確認動能回升。"
    },
    "ema_pullback": {
        "func": strategy_ema_pullback,
        "desc": "EMA回踩策略。多頭排列中回踩EMA slow，趨勢回調買入。"
    },
    # fakeout_reversal: 已移除 — 期貨 PF=0.85, 台股 PnL=-3,593, 雙向皆虧損 (2026-04-08 回測)
}
