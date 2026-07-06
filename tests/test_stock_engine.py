import numpy as np
from backtest.stock_engine import simulate_stock_trades, calc_stock_costs

def test_odd_lot_day_trading_restriction():
    """驗證：同一交易日內不允許賣出 (當沖限制)"""
    n = 10
    close = np.array([100.0] * n)
    high = np.array([105.0] * n)
    low = np.array([95.0] * n)
    # 所有 K 棒都在同一個交易日
    trading_day = np.array([20260406] * n)
    
    long_signals = np.zeros(n, dtype=np.bool_)
    long_signals[1] = True # 第 2 根買進
    
    short_signals = np.zeros(n, dtype=np.bool_)
    short_signals[3] = True # 第 4 根賣出訊號 (應被忽略)
    
    ent, ext, pos, pnl, reasons, qtys = simulate_stock_trades(
        close, high, low, trading_day, 
        long_signals, short_signals, 
        initial_balance=100000.0, 
        capital_per_trade=10000.0,
        stop_loss_pct=0.03
    )
    
    # 驗證：雖然第 4 根有賣出訊號且觸及止損，但因為同日買進，pos 應維持 1
    assert pos[1] == 1
    assert pos[3] == 1
    assert ext[3] == 0.0 # 沒有執行賣出

def test_stock_costs_minimum_fee():
    """驗證：低額交易時應觸發 20 元低消手續費"""
    # 買進 100 元股票 10 股 = 1000 元
    # 0.05% 手續費 = 0.5 元 < 20 元，應取 20 元
    cost = calc_stock_costs(100.0, 10, is_buy=True)
    assert cost == 20.0

def test_scout_strategy_lifecycle():
    """驗證偵察兵完整生命週期：試單 -> 加碼 -> 出場"""
    close = np.array([100.0, 102.0, 105.0])
    high = np.array([100.0, 102.0, 106.0])
    low = np.array([100.0, 101.0, 104.0])
    trading_day = np.array([20260401, 20260402, 20260403])
    
    long_signals = np.array([True, True, False])
    short_signals = np.array([False, False, True])
    
    ent, ext, pos, pnl, reasons, qtys = simulate_stock_trades(
        close, high, low, trading_day, 
        long_signals, short_signals, 
        initial_balance=100000.0, 
        capital_per_trade=10000.0
    )
    
    assert pos[0] == 1 # 試單中
    assert pos[1] == 2 # 已加碼
    assert pos[2] == 0 # 已平倉
    # 最高點 106.0 * (1 - 0.015) = 104.41
    assert ext[2] == 104.41
    assert np.sum(pnl) > 0

def test_pnl_includes_buy_side_fees():
    """驗證：PnL 必須包含買方手續費 (RULES.md 要求)"""
    close = np.array([100.0, 110.0])
    high = np.array([100.0, 110.0])
    low = np.array([100.0, 100.0])
    trading_day = np.array([20260401, 20260402])
    
    long_signals = np.array([True, False])
    short_signals = np.array([False, False])
    
    ent, ext, pos, pnl, reasons, qtys = simulate_stock_trades(
        close, high, low, trading_day,
        long_signals, short_signals,
        initial_balance=100000.0,
        capital_per_trade=10000.0,
        stop_loss_pct=0.03,
        take_profit_pct=0.05,
        trailing_stop_pct=0.015
    )
    
    # 最後一根強制出場 (reason=7)
    trade_pnl = pnl[pnl != 0]
    if len(trade_pnl) > 0:
        gross = (ext[1] - ent[0]) * qtys[1]
        buy_cost = calc_stock_costs(ent[0], qtys[1], True)
        sell_cost = calc_stock_costs(ext[1], qtys[1], False)
        expected_net = gross - buy_cost - sell_cost
        assert abs(trade_pnl[0] - expected_net) < 0.01, f"PnL {trade_pnl[0]} != expected {expected_net}"
