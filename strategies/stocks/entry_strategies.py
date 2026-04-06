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


STOCK_STRATEGIES = {
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
}
