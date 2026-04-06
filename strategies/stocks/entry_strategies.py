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
    """動能突破：突破今日高點且漲幅 > 2%。"""
    last_5m = state["last_5m"]
    day_open = last_5m.get("day_open", 0)
    if day_open == 0:
        return None
    
    if last_5m["Close"] > day_open * 1.02 and last_5m.get("is_new_high", False):
        return {"action": "BUY", "reason": "MOM_BREAKOUT", "stop_loss": last_5m["Close"] * 0.985}
    return None

def strategy_stock_scout(state, cfg):
    """
    零股偵察兵：
    1. SCOUT: Squeeze Fired 時買入 10 股試單。
    2. SCALE: 獲利 > 1% 且動能持續時回傳加碼訊號。
    """
    last_5m, _last_15m = state["last_5m"], state["last_15m"]
    state["df_5m"]
    
    # 這裡需要讀取該標的的現有部位狀態
    # 假設我們在 state 中注入了 'stage' 資訊
    stage = state.get("scout_stage", "IDLE")
    entry_price = state.get("scout_entry_price", 0)
    curr_price = last_5m["Close"]

    # 階段 1: 尋找試單機會
    if stage == "IDLE":
        if last_5m["fired"] and last_5m["mom_state"] >= 2:
            return {"action": "BUY", "reason": "SCOUT_ENTRY", "qty_mode": "SCOUT", "stop_loss": curr_price * 0.985}

    # 階段 2: 已持倉，檢查是否加碼
    elif stage == "SCOUT":
        profit_pct = (curr_price - entry_price) / entry_price
        # 獲利確認，觸發加碼 (Scale to Main Force)
        if profit_pct >= 0.01 and last_5m["mom_state"] == 3:
            return {"action": "BUY", "reason": "SCOUT_SCALE", "qty_mode": "MAIN", "stop_loss": curr_price * 0.98}
            
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
    }
}
