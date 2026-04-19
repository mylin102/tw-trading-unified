
import pytest
from datetime import datetime
from strategies.futures.squeeze_futures.engine.simulator import PaperTrader

def test_margin_enforcement_buy():
    """測試 BUY 訊號的保證金攔截"""
    # 初始資金 100,000，每口保證金 60,000
    trader = PaperTrader(initial_balance=100000, margin_per_lot=60000)
    
    # 第一口應該成功 (60,000 <= 100,000)
    r1 = trader.execute_signal("BUY", 20000, datetime.now(), lots=1, max_lots=2)
    assert "Entry LONG" in r1
    assert trader.position == 1
    
    # 第二口應該失敗 (120,000 > 100,000)
    r2 = trader.execute_signal("BUY", 20010, datetime.now(), lots=1, max_lots=2)
    assert "Insufficient Margin" in r2
    assert trader.position == 1

def test_margin_enforcement_sell():
    """測試 SELL 訊號的保證金攔截"""
    # 初始資金 50,000，每口保證金 40,000
    trader = PaperTrader(initial_balance=50000, margin_per_lot=40000)
    
    # 第一口應該成功
    r1 = trader.execute_signal("SELL", 20000, datetime.now(), lots=1, max_lots=1)
    assert "Entry SHORT" in r1
    assert trader.position == -1
    
    # 資金不足以開第二口 (雖然 max_lots 是 2)
    r2 = trader.execute_signal("SELL", 19990, datetime.now(), lots=1, max_lots=2)
    assert "Insufficient Margin" in r2
    assert trader.position == -1

def test_margin_after_loss():
    """測試虧損後保證金不足導致無法再進場"""
    # 初始 40,000，保證金 40,000
    trader = PaperTrader(initial_balance=40000, margin_per_lot=40000, point_value=10, fee_per_side=20)
    
    # 進場
    trader.execute_signal("BUY", 20000, datetime.now(), lots=1, max_lots=1)
    # 虧損出場 (20000 -> 19900 = -100 pts = -1000 TWD)
    trader.execute_signal("EXIT", 19900, datetime.now())
    
    assert trader.balance < 40000
    
    # 再次進場應該被攔截
    r2 = trader.execute_signal("BUY", 19900, datetime.now(), lots=1, max_lots=1)
    assert "Insufficient Margin" in r2
    assert trader.position == 0


def test_margin_reversal_short_to_long():
    """反轉 SHORT→LONG 時，保證金只需計算新倉 1 口，不應將舊倉計入
    Bug: required_margin = (abs(-1)+1)*margin = 2*margin，導致合法反轉被誤攔截"""
    # 資金剛好能支付 1 口保證金但不夠 2 口
    trader = PaperTrader(initial_balance=50000, margin_per_lot=40000)

    r1 = trader.execute_signal("SELL", 20000, datetime.now(), lots=1, max_lots=1)
    assert "Entry SHORT" in r1
    assert trader.position == -1

    # 反轉：平 SHORT 後只需 1 口保證金 (40000 <= 50000)，應該成功
    r2 = trader.execute_signal("BUY", 20010, datetime.now(), lots=1, max_lots=1)
    assert "Insufficient Margin" not in r2, (
        "Reversal SHORT→LONG incorrectly rejected: margin check must not double-count existing position"
    )
    assert trader.position == 1


def test_margin_reversal_long_to_short():
    """反轉 LONG→SHORT 時，保證金只需計算新倉 1 口，不應將舊倉計入"""
    trader = PaperTrader(initial_balance=50000, margin_per_lot=40000)

    r1 = trader.execute_signal("BUY", 20000, datetime.now(), lots=1, max_lots=1)
    assert "Entry LONG" in r1
    assert trader.position == 1

    # 反轉：平 LONG 後只需 1 口保證金 (40000 <= 50000)，應該成功
    r2 = trader.execute_signal("SELL", 19990, datetime.now(), lots=1, max_lots=1)
    assert "Insufficient Margin" not in r2, (
        "Reversal LONG→SHORT incorrectly rejected: margin check must not double-count existing position"
    )
    assert trader.position == -1
