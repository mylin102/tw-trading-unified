"""
Test weak_bear_trend strategy — WEAK Regime 空头趋势策略測試

測試重點:
1. 只在 regime=WEAK/CHOP + bias=SHORT 時進場
2. 不追空：必須有反彈接近 VWAP
3. 動能確認：mom_velo < 0 (向下加速)
4. 嚴格止損：1.5 ATR
5. 時間止損：20 分鐘無獲利出場
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timedelta
import sys
import os

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from strategies.plugins.futures.active.weak_bear_trend import WeakBearTrend
from core.strategy_context import PositionView, StrategyContext
from core.signal import Signal

# Helper for testing
class Position:
    def __init__(self, size=0, entry_price=0.0):
        self.size = size
        self.entry_price = entry_price
        self.unrealized_pnl = 0.0
        self.current_stop_loss = None


def create_test_bar(
    close: float,
    vwap: float,
    ema_fast: float,
    ema_slow: float,
    mom_velo: float,
    adx: float,
    volume_spike: float,
    regime: str,
    bias: str,
    atr: float = 50.0,
    high: float = None,
    low: float = None,
    timestamp: datetime = None,
) -> dict:
    """創建測試用 K 棒數據."""
    if high is None:
        high = close + 10
    if low is None:
        low = close - 10
    if timestamp is None:
        timestamp = datetime.now()
    
    return {
        "Close": close,
        "High": high,
        "Low": low,
        "vwap": vwap,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "mom_velo": mom_velo,
        "adx": adx,
        "volume_spike": volume_spike,
        "regime": regime,
        "bias": bias,
        "atr": atr,
        "price_vs_vwap": (close - vwap) / vwap if vwap > 0 else 0,
        "timestamp": timestamp.isoformat(),
    }


def test_weak_bear_entry():
    """測試 1: 標準空頭進場情境."""
    print("\n=== Test 1: 標準空頭進場 ===")
    
    strategy = WeakBearTrend()
    
    # 模擬 WEAK + SHORT + 反彈失敗
    bar = create_test_bar(
        close=22000,
        vwap=22050,
        ema_fast=22080,
        ema_slow=22120,
        mom_velo=-8.0,  # 向下加速
        adx=18.0,  # WEAK regime
        volume_spike=1.1,
        regime="WEAK",
        bias="SHORT",
        atr=50.0,
        high=22020,
        low=21990,
    )
    
    # Mock context
    context = type('obj', (object,), {
        'config': {
            "params": {
                "shadow_mode": False,
                "stop_atr_mult": 1.5,
                "take_profit_atr_mult": 2.0,
            }
        },
        'market': type('obj', (object,), {
            'last_bar': bar,
            'df_5m': None,
        })(),
        'position': PositionView(size=0, entry_price=0, unrealized_pnl=0),
    })()
    
    signal = strategy.on_bar(context)
    
    if signal and signal.action == "SELL":
        print(f"✅ 進場信號：SELL @ {signal.stop_loss=}")
        print(f"   止損：{signal.stop_loss:.0f}, 止盈：{signal.target:.0f}")
        print(f"   信心：{signal.confidence:.2f}")
        return True
    else:
        print(f"❌ 預期 SELL 信號，實際：{signal}")
        return False


def test_regime_block():
    """測試 2: TREND regime 應該阻止進場."""
    print("\n=== Test 2: TREND regime 阻止 ===")
    
    strategy = WeakBearTrend()
    
    bar = create_test_bar(
        close=22000,
        vwap=22050,
        ema_fast=22080,
        ema_slow=22120,
        mom_velo=-8.0,
        adx=18.0,
        volume_spike=1.1,
        regime="TREND",  # 應該是 TREND 而不是 WEAK
        bias="SHORT",
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=type('obj', (object,), {'last_bar': bar, 'df_5m': None})(),
        position=Position(size=0, entry_price=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：TREND regime 不進場")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_bias_block():
    """測試 3: LONG bias 應該阻止進場."""
    print("\n=== Test 3: LONG bias 阻止 ===")
    
    strategy = WeakBearTrend()
    
    bar = create_test_bar(
        close=22000,
        vwap=22050,
        ema_fast=22080,
        ema_slow=22120,
        mom_velo=-8.0,
        adx=18.0,
        volume_spike=1.1,
        regime="WEAK",
        bias="LONG",  # 應該是 SHORT
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=type('obj', (object,), {'last_bar': bar, 'df_5m': None})(),
        position=Position(size=0, entry_price=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：LONG bias 不進場")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_adx_block():
    """測試 4: ADX 過高應該阻止進場."""
    print("\n=== Test 4: ADX 過高阻止 ===")
    
    strategy = WeakBearTrend()
    
    bar = create_test_bar(
        close=22000,
        vwap=22050,
        ema_fast=22080,
        ema_slow=22120,
        mom_velo=-8.0,
        adx=30.0,  # ADX 過高，不是 WEAK regime
        volume_spike=1.1,
        regime="WEAK",
        bias="SHORT",
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=type('obj', (object,), {'last_bar': bar, 'df_5m': None})(),
        position=Position(size=0, entry_price=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：ADX 過高")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_momentum_block():
    """測試 5: 動能不夠向下應該阻止進場."""
    print("\n=== Test 5: 動能不夠向下 ===")
    
    strategy = WeakBearTrend()
    
    bar = create_test_bar(
        close=22000,
        vwap=22050,
        ema_fast=22080,
        ema_slow=22120,
        mom_velo=5.0,  # 正向動能，不是向下
        adx=18.0,
        volume_spike=1.1,
        regime="WEAK",
        bias="SHORT",
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=type('obj', (object,), {'last_bar': bar, 'df_5m': None})(),
        position=Position(size=0, entry_price=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：動能不是向下加速")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_shadow_mode():
    """測試 6: Shadow mode 應該返回 HOLD 而不是 SELL."""
    print("\n=== Test 6: Shadow mode ===")
    
    strategy = WeakBearTrend()
    
    bar = create_test_bar(
        close=22000,
        vwap=22050,
        ema_fast=22080,
        ema_slow=22120,
        mom_velo=-8.0,
        adx=18.0,
        volume_spike=1.1,
        regime="WEAK",
        bias="SHORT",
    )
    
    context = StrategyContext(
        config={
            "params": {
                "shadow_mode": True,  # 開啟 shadow mode
            }
        },
        market=type('obj', (object,), {'last_bar': bar, 'df_5m': None})(),
        position=Position(size=0, entry_price=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal and signal.action == "HOLD" and "SHADOW" in signal.reason:
        print(f"✅ Shadow mode 正確：HOLD, reason={signal.reason}")
        return True
    else:
        print(f"❌ 預期 HOLD (shadow), 實際：{signal}")
        return False


def run_all_tests():
    """執行所有測試."""
    print("=" * 60)
    print("weak_bear_trend 策略測試")
    print("=" * 60)
    
    results = []
    results.append(("標準空頭進場", test_weak_bear_entry()))
    results.append(("TREND regime 阻止", test_regime_block()))
    results.append(("LONG bias 阻止", test_bias_block()))
    results.append(("ADX 過高阻止", test_adx_block()))
    results.append(("動能不夠向下", test_momentum_block()))
    results.append(("Shadow mode", test_shadow_mode()))
    
    print("\n" + "=" * 60)
    print("測試結果總結")
    print("=" * 60)
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")
    
    print(f"\n總計：{passed}/{total} 通過")
    
    if passed == total:
        print("🎉 所有測試通過！")
        return 0
    else:
        print("⚠️  有測試失敗，請檢查邏輯")
        return 1


if __name__ == "__main__":
    exit(run_all_tests())
