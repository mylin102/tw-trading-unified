"""
Pluggable entry strategies for StockMonitor.
Focuses on Odd-Lot specific behaviors like Mean Reversion and Arbitrage.
"""

from strategies.stocks.multi_timeframe import analyze_market_condition, should_trade_based_on_tf

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

        # 防禦 B: 大盤濾網 (GSD Relax: Allow SCOUT in BEAR for technical signals)
        market_trend = state.get("market_trend", "BULL")
        market_safe = True # Default to True for SCOUT stage
        if market_trend == "BEAR":
            # In BEAR market, require stronger squeeze signal (mom_state >= 3)
            signal_strength = last_5m["mom_state"] >= 3
        else:
            signal_strength = last_5m["mom_state"] >= 2

        # 防禦 C: MACD 動能確認
        macd_confirmed = last_5m.get("macd_rising", True)

        if last_5m["fired"] and signal_strength and vol_spike and macd_confirmed:
            return {"action": "BUY", "reason": f"SCOUT_CONFIRMED_{market_trend}", "qty_mode": "SCOUT", "stop_loss": curr_price * 0.985}


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


def strategy_it_window_dressing(state, cfg):
    """
    投信作帳波段策略。
    條件：投信連三買 + 多頭排列 + 股價在月線之上。
    邏輯：跟隨法人建倉波段，持股 3-5 天。
    """
    last_5m = state["last_5m"]
    df = state["df_5m"]

    # 投信連三買 (支援 bool 或 rolling count)
    it_buy_flag = last_5m.get("it_buy_3days", False)
    it_buy_count = int(last_5m.get("it_buy_rolling_count", 0) or 0)
    # 多頭排列 (若未提供，從 MA20/MA60 推斷)
    bullish_align = last_5m.get("bullish_align")
    if bullish_align is None:
        bullish_align = (last_5m.get("ma20", 0) > last_5m.get("ma60", 0)) and (last_5m.get("Close", 0) > last_5m.get("ma20", 0))

    # 月線之上（保守預設：若未提供，視為在月線之上當 ma60 < Close）
    above_monthly = last_5m.get("above_monthly")
    if above_monthly is None:
        above_monthly = last_5m.get("Close", 0) > last_5m.get("ma60", 0)

    if (it_buy_flag or it_buy_count >= 2) and bullish_align and above_monthly:
        sl = last_5m["Close"] * (1 - cfg.get("stop_loss_pct", 0.03))
        reason = f"IT_3DAY_BUY_{it_buy_count if it_buy_count>0 else 'flag'}"
        return {"action": "BUY", "reason": reason, "stop_loss": sl}
    return None


def strategy_stock_canslim_breakout(state, cfg):
    """
    CANSLIM 突破策略 (P0 優化版)。
    條件：
    1. 型態：杯中帶把 (Cup with Handle) 或雙底 (Double Bottom)
    2. 突破：帶量突破 Pivot 點 (成交量 ≥ 平均 1.5 倍)
    3. 基本面：EPS 成長、營收成長、ROE > 15%
    4. 大盤方向：大盤非空頭
    """
    last_5m = state["last_5m"]
    df = state["df_5m"]

    # 1. 型態確認
    pattern = last_5m.get("pattern", "")
    if pattern not in ["cup_with_handle", "double_bottom"]:
        return None

    # 2. 突破確認
    pivot_price = last_5m.get("pivot_price", 0)
    if pivot_price <= 0:
        return None

    # 成交量確認 (帶量突破)
    vol_avg = df["Volume"].rolling(20).mean().iloc[-1]
    if vol_avg <= 0 or last_5m["Volume"] < vol_avg * 1.5:
        return None

    # 價格突破確認
    if last_5m["Close"] < pivot_price:
        return None

    # 3. 基本面過濾 (如果數據可用)
    if cfg.get("stocks", {}).get("canslim", {}).get("fundamental_filter", True):
        eps_growth = last_5m.get("eps_growth", 0)
        revenue_growth = last_5m.get("revenue_growth", 0)
        roe = last_5m.get("roe", 0)

        if eps_growth < 0.2 or revenue_growth < 0.15 or roe < 15:
            return None

    # 4. 大盤方向過濾 (Market Direction - M)
    if cfg.get("stocks", {}).get("canslim", {}).get("market_direction_filter", True):
        if state.get("market_trend") == "BEAR":
            return None

    return {
        "action": "BUY", 
        "reason": f"CANSLIM_BREAKOUT_PIVOT_{pivot_price}", 
        "qty_mode": "MAIN",
        "stop_loss": pivot_price * 0.93  # 突破失敗止損設在 pivot 下方 7%
    }

def strategy_stock_mean_reversion_enhanced(state, cfg):
    """
    增強版均值回歸：結合多時間框架過濾器。
    1. 基本條件：價格跌破布林下軌
    2. 多時間框架過濾：檢查15分/60分趨勢一致性
    3. 市場狀態過濾：排除空頭市場
    """
    # 1. 基本均值回歸條件
    df = state["df_5m"]
    if len(df) < 20:
        return None
    
    last = df.iloc[-1]
    if last["Close"] >= last.get("bb_lower", 0):
        return None
    
    # 2. 多時間框架分析
    market_analysis = analyze_market_condition(df)
    if not market_analysis:
        return None
    
    # 3. 檢查是否應該交易
    should_trade, _ = should_trade_based_on_tf(df)
    if not should_trade:
        return None
    
    # 4. 計算停損
    stop_loss = last["Close"] * 0.97
    
    return {
        "action": "BUY",
        "reason": f"BB_LOWER_BOUNCE_ENHANCED (market_state: {market_analysis['market_state']['primary_trend']})",
        "stop_loss": stop_loss,
        "metadata": {
            "multi_timeframe_used": True,
            "market_regime": market_analysis['market_state']['market_regime'],
            "primary_trend": market_analysis['market_state']['primary_trend'],
            "filters_passed": market_analysis['trading_recommendation'].get('filters_passed', 0),
            "total_filters": market_analysis['trading_recommendation'].get('total_filters', 0)
        }
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
    "mean_reversion_enhanced": {
        "func": strategy_stock_mean_reversion_enhanced,
        "desc": "增強版均值回歸。結合多時間框架過濾器，避免逆勢交易，提高勝率。"
    },
    # "technical_analysis_enhanced": {
    #     "func": strategy_technical_analysis_enhanced,
    #     "desc": "台灣市場增強版技術分析。整合均線系統、支撐壓力、成交量確認、法人指標。"
    # },
    # fakeout_reversal: 已移除 — 期貨 PF=0.85, 台股 PnL=-3,593, 雙向皆虧損 (2026-04-08 回測)
}