"""
Test weak_bear_trend strategy — 簡單单元测试
"""
import sys
import os
from datetime import datetime

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from strategies.plugins.futures.active.weak_bear_trend import WeakBearTrend


def create_mock_context(bar, config=None, position_size=0):
    """創建 mock context 對象."""
    if config is None:
        config = {"params": {"shadow_mode": False}}
    
    return type('obj', (object,), {
        'config': config,
        'market': type('obj', (object,), {
            'last_bar': bar,
            'df_5m': None,
        })(),
        'position': type('obj', (object,), {
            'size': position_size,
            'entry_price': 0,
            'unrealized_pnl': 0,
        })(),
        'bar_counter': 100,
    })()


def create_test_bar(**kwargs):
    """創建測試用 K 棒數據."""
    defaults = {
        "Close": 22000,
        "High": 22020,
        "Low": 21990,
        "vwap": 22050,
        "ema_fast": 22080,
        "ema_slow": 22120,
        "mom_velo": -8.0,
        "adx": 18.0,
        "volume_spike": 1.1,
        "regime": "WEAK",
        "bias": "SHORT",
        "atr": 50.0,
        "price_vs_vwap": -0.0023,
        "timestamp": datetime.now().isoformat(),
    }
    defaults.update(kwargs)
    return defaults


def test_1_standard_entry():
    """測試 1: 標準空頭進場情境."""
    print("\n=== Test 1: 標準空頭進場 ===")
    
    strategy = WeakBearTrend()
    
    # 創建一個簡單的 df_5m mock，模擬過去 5 bars 有高點接近 VWAP
    class SimpleDF:
        def __init__(self):
            self._data = {'High': [22100, 22090, 22080, 22070, 22060]}
        
        def __len__(self):
            return 5
        
        @property
        def columns(self):
            return ['High']
        
        def __getitem__(self, key):
            class Series:
                def __init__(self, data):
                    self._data = data
                def iloc(self, idx):
                    if isinstance(idx, slice):
                        return self._data[idx]
                    return self._data[idx]
            return Series(self._data)
    
    bar = create_test_bar()
    context = create_mock_context(bar)
    context.market.df_5m = SimpleDF()  # 添加 df_5m
    
    signal = strategy.on_bar(context)
    
    if signal and signal.action == "SELL":
        print(f"✅ 進場信號：SELL")
        print(f"   止損：{signal.stop_loss:.0f}, 止盈：{signal.target:.0f}")
        print(f"   信心：{signal.confidence:.2f}")
        return True
    else:
        print(f"❌ 預期 SELL 信號，實際：{signal}")
        return False


def test_2_regime_block():
    """測試 2: TREND regime 應該阻止進場."""
    print("\n=== Test 2: TREND regime 阻止 ===")
    
    strategy = WeakBearTrend()
    bar = create_test_bar(regime="TREND")
    context = create_mock_context(bar)
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：TREND regime 不進場")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_3_bias_block():
    """測試 3: LONG bias 應該阻止進場."""
    print("\n=== Test 3: LONG bias 阻止 ===")
    
    strategy = WeakBearTrend()
    bar = create_test_bar(bias="LONG")
    context = create_mock_context(bar)
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：LONG bias 不進場")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_4_adx_block():
    """測試 4: ADX 過高應該阻止進場."""
    print("\n=== Test 4: ADX 過高阻止 ===")
    
    strategy = WeakBearTrend()
    bar = create_test_bar(adx=30.0)
    context = create_mock_context(bar)
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：ADX 過高")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_5_momentum_block():
    """測試 5: 動能不夠向下應該阻止進場."""
    print("\n=== Test 5: 動能不夠向下 ===")
    
    strategy = WeakBearTrend()
    bar = create_test_bar(mom_velo=5.0)
    context = create_mock_context(bar)
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：動能不是向下加速")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_6_shadow_mode():
    """測試 6: Shadow mode 應該返回 HOLD."""
    print("\n=== Test 6: Shadow mode ===")
    
    strategy = WeakBearTrend()
    bar = create_test_bar()
    context = create_mock_context(bar, config={"params": {"shadow_mode": True}})
    
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
    print("weak_bear_trend 策略单元测试")
    print("=" * 60)
    
    results = []
    results.append(("標準空頭進場", test_1_standard_entry()))
    results.append(("TREND regime 阻止", test_2_regime_block()))
    results.append(("LONG bias 阻止", test_3_bias_block()))
    results.append(("ADX 過高阻止", test_4_adx_block()))
    results.append(("動能不夠向下", test_5_momentum_block()))
    results.append(("Shadow mode", test_6_shadow_mode()))
    
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
